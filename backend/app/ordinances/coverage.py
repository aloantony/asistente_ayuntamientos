"""Coverage reporting helpers for imported municipal ordinances."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from urllib import parse as urlparse

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.municipalities.models import Municipality
from app.ordinances.embeddings import EmbeddingsUnavailableError, embed_text
from app.ordinances.models import Ordinance, OrdinanceImportItem, OrdinanceLegalChunk

CASTILLA_LEON_AUTONOMOUS_COMMUNITY = "Castilla y León"
CASTILLA_LEON_PROVINCES = (
    "Ávila",
    "Burgos",
    "León",
    "Palencia",
    "Salamanca",
    "Segovia",
    "Soria",
    "Valladolid",
    "Zamora",
)

CASTILLA_LEON_BOP_DOMAINS: dict[str, tuple[str, ...]] = {
    "Ávila": ("diputacionavila.es",),
    "Burgos": ("bopbur.diputaciondeburgos.es",),
    "León": ("bop.dipuleon.es",),
    "Palencia": ("diputaciondepalencia.es",),
    "Salamanca": ("diputaciondesalamanca.gob.es",),
    "Segovia": ("dipsegovia.es",),
    "Soria": ("bop.dipsoria.es",),
    "Valladolid": ("diputaciondevalladolid.es",),
    "Zamora": ("diputaciondezamora.es",),
}


def build_castilla_leon_coverage_report(db: Session) -> dict:
    """Return an aggregate coverage report for all Castilla y León provinces."""

    province_reports = [
        build_province_coverage_report(db, province)
        for province in CASTILLA_LEON_PROVINCES
    ]
    province_summaries = [
        {
            "province": report["province"],
            "municipalities_total": report["municipalities_total"],
            "municipalities_with_approved_ordinances": report[
                "municipalities_with_approved_ordinances"
            ],
            "municipalities_ready_for_assistant": report[
                "municipalities_ready_for_assistant"
            ],
            "ordinances_total": report["ordinances_total"],
            "ordinances_approved": report["ordinances_approved"],
            "chunks_total": report["chunks_total"],
            "chunks_ready": report["chunks_ready"],
            "chunks_approved": report["chunks_approved"],
            "chunks_failed": report["chunks_failed"],
            "import_failures_total": report["import_failures_total"],
        }
        for report in province_reports
    ]
    return {
        "autonomous_community": CASTILLA_LEON_AUTONOMOUS_COMMUNITY,
        "provinces_total": len(CASTILLA_LEON_PROVINCES),
        "provinces_with_municipalities": sum(
            1 for report in province_reports if report["municipalities_total"] > 0
        ),
        "provinces_ready_for_assistant": sum(
            1
            for report in province_reports
            if report["municipalities_ready_for_assistant"] > 0
        ),
        "municipalities_total": sum(
            report["municipalities_total"] for report in province_reports
        ),
        "municipalities_with_approved_ordinances": sum(
            report["municipalities_with_approved_ordinances"]
            for report in province_reports
        ),
        "municipalities_ready_for_assistant": sum(
            report["municipalities_ready_for_assistant"] for report in province_reports
        ),
        "ordinances_total": sum(report["ordinances_total"] for report in province_reports),
        "ordinances_approved": sum(
            report["ordinances_approved"] for report in province_reports
        ),
        "chunks_total": sum(report["chunks_total"] for report in province_reports),
        "chunks_ready": sum(report["chunks_ready"] for report in province_reports),
        "chunks_approved": sum(report["chunks_approved"] for report in province_reports),
        "chunks_failed": sum(report["chunks_failed"] for report in province_reports),
        "import_failures_total": sum(
            report["import_failures_total"] for report in province_reports
        ),
        "provinces": province_summaries,
    }


def build_province_coverage_report(db: Session, province: str) -> dict:
    """Return local coverage metrics for one Castilla y León province."""

    canonical_province = canonical_castilla_leon_province(province)
    rows = db.execute(
        select(
            func.min(Municipality.id).label("id"),
            Municipality.name,
            func.count(func.distinct(Ordinance.id)).label("ordinances_total"),
            func.count(func.distinct(Ordinance.id))
            .filter(Ordinance.curation_status == "approved")
            .label("ordinances_approved"),
            func.count(OrdinanceLegalChunk.id).label("chunks_total"),
            func.count(OrdinanceLegalChunk.id)
            .filter(OrdinanceLegalChunk.embedding_status == "ready")
            .label("chunks_ready"),
            func.count(OrdinanceLegalChunk.id)
            .filter(OrdinanceLegalChunk.review_status == "approved")
            .label("chunks_approved"),
            func.count(OrdinanceLegalChunk.id)
            .filter(OrdinanceLegalChunk.embedding_status == "failed")
            .label("chunks_failed"),
        )
        .outerjoin(Ordinance, Ordinance.municipality_id == Municipality.id)
        .outerjoin(OrdinanceLegalChunk, OrdinanceLegalChunk.ordinance_id == Ordinance.id)
        .where(_province_filter(canonical_province), _castilla_leon_filter())
        .group_by(Municipality.name)
        .order_by(Municipality.name)
    ).all()

    municipalities = [
        {
            "municipality_id": row.id,
            "municipality_name": row.name,
            "ordinances_total": row.ordinances_total,
            "ordinances_approved": row.ordinances_approved,
            "chunks_total": row.chunks_total,
            "chunks_ready": row.chunks_ready,
            "chunks_approved": row.chunks_approved,
            "chunks_failed": row.chunks_failed,
            "ready_for_assistant": row.ordinances_approved > 0
            and row.chunks_ready > 0
            and row.chunks_approved > 0,
        }
        for row in rows
    ]
    import_failures = _province_import_failures(db, canonical_province)
    return {
        "province": canonical_province,
        "municipalities_total": len(municipalities),
        "municipalities_with_approved_ordinances": sum(
            1 for row in municipalities if row["ordinances_approved"] > 0
        ),
        "municipalities_ready_for_assistant": sum(
            1 for row in municipalities if row["ready_for_assistant"]
        ),
        "ordinances_total": sum(row["ordinances_total"] for row in municipalities),
        "ordinances_approved": sum(row["ordinances_approved"] for row in municipalities),
        "chunks_total": sum(row["chunks_total"] for row in municipalities),
        "chunks_ready": sum(row["chunks_ready"] for row in municipalities),
        "chunks_approved": sum(row["chunks_approved"] for row in municipalities),
        "chunks_failed": sum(row["chunks_failed"] for row in municipalities),
        "import_failures_total": len(import_failures),
        "import_failures": import_failures,
        "municipalities": municipalities,
    }


def retry_failed_province_embeddings(db: Session, province: str) -> dict:
    """Retry failed embeddings for legal chunks in one province."""

    canonical_province = canonical_castilla_leon_province(province)
    chunks = list(
        db.scalars(
            select(OrdinanceLegalChunk)
            .join(OrdinanceLegalChunk.ordinance)
            .join(Ordinance.municipality)
            .options(
                selectinload(OrdinanceLegalChunk.ordinance).selectinload(
                    Ordinance.municipality
                )
            )
            .where(
                _province_filter(canonical_province),
                _castilla_leon_filter(),
                OrdinanceLegalChunk.embedding_status == "failed",
            )
            .order_by(OrdinanceLegalChunk.id)
        )
    )
    retried = 0
    restored = 0
    still_failed: list[dict] = []
    for chunk in chunks:
        retried += 1
        try:
            embedding, embedding_model, embedding_status = embed_text(chunk.text)
        except EmbeddingsUnavailableError:
            embedding = None
            embedding_model = None
            embedding_status = "failed"
        chunk.embedding = embedding
        if embedding_model:
            chunk.embedding_model = embedding_model
        chunk.embedding_status = embedding_status
        chunk.embedded_at = datetime.now(UTC) if embedding_status == "ready" else None
        if embedding_status == "ready":
            restored += 1
            continue
        still_failed.append(_summarize_failed_chunk(chunk))
    db.commit()
    return {
        "province": canonical_province,
        "retried": retried,
        "restored": restored,
        "failed": len(still_failed),
        "still_failed": still_failed,
    }


def canonical_castilla_leon_province(value: str) -> str:
    slug = _slugify(value)
    province = _PROVINCE_BY_SLUG.get(slug)
    if province is None:
        raise ValueError(f"Unsupported Castilla y León province: {value}")
    return province


def _province_filter(province: str):
    aliases = {_without_accents(province), province}
    return or_(*(Municipality.province.ilike(alias) for alias in aliases))


def _castilla_leon_filter():
    return Municipality.autonomous_community.ilike(CASTILLA_LEON_AUTONOMOUS_COMMUNITY)


def _province_import_failures(db: Session, province: str) -> list[dict]:
    domains = CASTILLA_LEON_BOP_DOMAINS.get(province, ())
    items = db.scalars(
        select(OrdinanceImportItem)
        .options(
            selectinload(OrdinanceImportItem.municipality),
            selectinload(OrdinanceImportItem.official_source),
        )
        .where(
            OrdinanceImportItem.status == "failed",
            ~select(Ordinance.id)
            .where(Ordinance.source_url == OrdinanceImportItem.source_url)
            .exists(),
        )
        .order_by(OrdinanceImportItem.updated_at.desc(), OrdinanceImportItem.id.desc())
    )
    failures = []
    for item in items:
        if not _item_belongs_to_province(item, province, domains):
            continue
        failures.append(
            {
                "item_id": item.id,
                "job_id": item.job_id,
                "municipality_id": item.municipality_id,
                "municipality_name": item.municipality.name if item.municipality else None,
                "source_url": item.source_url,
                "error_message": item.error_message,
                "requires_manual_review": _requires_manual_review(item.error_message),
            }
        )
    return failures


def _item_belongs_to_province(
    item: OrdinanceImportItem,
    province: str,
    domains: tuple[str, ...],
) -> bool:
    if (
        item.municipality
        and item.municipality.autonomous_community.lower()
        == CASTILLA_LEON_AUTONOMOUS_COMMUNITY.lower()
        and _slugify(item.municipality.province) == _slugify(province)
    ):
        return True
    host = (urlparse.urlparse(item.source_url).hostname or "").lower()
    if _host_matches_domains(host, domains):
        return True
    source_domain = item.official_source.domain.lower() if item.official_source else ""
    return bool(source_domain and _host_matches_domains(source_domain, domains))


def _host_matches_domains(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _summarize_failed_chunk(chunk: OrdinanceLegalChunk) -> dict:
    return {
        "chunk_id": chunk.id,
        "ordinance_id": chunk.ordinance_id,
        "municipality_name": chunk.ordinance.municipality.name,
        "citation": chunk.citation,
        "embedding_status": chunk.embedding_status,
    }


def _requires_manual_review(error_message: str | None) -> bool:
    if not error_message:
        return False
    normalized = error_message.lower()
    return any(
        marker in normalized
        for marker in ("ocr", "revisión manual", "texto suficiente")
    )


def _slugify(value: str) -> str:
    return _without_accents(value).lower().replace(" ", "-").replace("_", "-")


def _without_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip())
    return "".join(character for character in normalized if not unicodedata.combining(character))


_PROVINCE_BY_SLUG = {
    _slugify(province): province for province in CASTILLA_LEON_PROVINCES
}
