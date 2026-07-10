"""Controlled Sprint 0 bootstrap for Burgos ordinance demo data.

The corpus below uses official BOP Burgos announcement PDF URLs only. The
bootstrap deliberately imports the live official PDFs through the same import
service used by ordinance import jobs, then marks successful chunks as approved
for demo retrieval while keeping legal-review notes explicit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib import parse as urlparse

from sqlalchemy import select, text as sql_text
from sqlalchemy.orm import Session, selectinload

from app.assistant import models as _assistant_models  # noqa: F401 - register mappers
from app.db.session import SessionLocal
from app.documents import models as _document_models  # noqa: F401 - register mappers
from app.municipalities.models import Municipality
from app.organizations import models as _organization_models  # noqa: F401 - register mappers
from app.ordinances.embeddings import embed_text, vector_similarity
from app.ordinances.import_service import run_import_job
from app.ordinances.models import (
    OfficialLegalSource,
    Ordinance,
    OrdinanceImportItem,
    OrdinanceImportJob,
    OrdinanceLegalChunk,
)
from app.ordinances.seed import ensure_initial_official_legal_sources
from app.projects import models as _project_models  # noqa: F401 - register mappers
from app.rbac import models as _rbac_models  # noqa: F401 - register mappers
from app.requirements import models as _requirement_models  # noqa: F401 - register mappers
from app.telegram import models as _telegram_models  # noqa: F401 - register mappers
from app.users import models as _user_models  # noqa: F401 - register mappers

BURGOS_PROVINCE = "Burgos"
BURGOS_AUTONOMOUS_COMMUNITY = "Castilla y León"
BOP_BURGOS_DOMAIN = "bopbur.diputaciondeburgos.es"
DEMO_REVIEW_NOTE = (
    "Aprobada técnicamente para recuperación en la demo Sprint 0 Burgos desde "
    "fuente oficial BOP Burgos. Requiere validación jurídica humana antes de "
    "uso oficial."
)


@dataclass(frozen=True)
class DemoOrdinanceSource:
    municipality_name: str
    title: str
    topic: str
    subtopic: str
    bulletin_number: str
    publication_date_label: str
    source_url: str


DEMO_ORDINANCE_SOURCES: tuple[DemoOrdinanceSource, ...] = (
    DemoOrdinanceSource(
        municipality_name="Hoyales de Roa",
        title=(
            "Modificación parcial de la ordenanza fiscal reguladora de la tasa "
            "por recogida de basuras domiciliarias o residuos sólidos urbanos"
        ),
        topic="residuos",
        subtopic="tasa de basuras",
        bulletin_number="BOPBUR-2025-04362",
        publication_date_label="BOP Burgos núm. 177, 19/09/2025",
        source_url=(
            "http://bopbur.diputaciondeburgos.es/sites/default/files/private/"
            "publicado/bopbur-2025-177/bopbur-2025-177-anuncio-202504362.pdf"
        ),
    ),
    DemoOrdinanceSource(
        municipality_name="Condado de Treviño",
        title=(
            "Modificación de la ordenanza fiscal reguladora de la tasa por "
            "suministro domiciliario de agua potable en Zurbitu"
        ),
        topic="agua",
        subtopic="suministro domiciliario",
        bulletin_number="BOPBUR-2025-01057",
        publication_date_label="BOP Burgos núm. 44, 05/03/2025",
        source_url=(
            "http://bopbur.diputaciondeburgos.es/sites/default/files/private/"
            "publicado/bopbur-2025-044/bopbur-2025-044-anuncio-202501057.pdf"
        ),
    ),
    DemoOrdinanceSource(
        municipality_name="Quintanar de la Sierra",
        title=(
            "Modificación de la ordenanza fiscal reguladora de las tasas de los "
            "servicios de abastecimiento de agua y alcantarillado y recogida de basuras"
        ),
        topic="agua y residuos",
        subtopic="abastecimiento, alcantarillado y basuras",
        bulletin_number="BOPBUR-2024-06870",
        publication_date_label="BOP Burgos núm. 7, 13/01/2025",
        source_url=(
            "http://bopbur.diputaciondeburgos.es/sites/default/files/private/"
            "publicado/bopbur-2025-007/bopbur-2025-007-anuncio-202406870.pdf"
        ),
    ),
)


def bootstrap_burgos_demo_ordinances(db: Session) -> dict:
    """Import and approve the controlled Burgos Sprint 0 demo corpus.

    The function is idempotent by source URL. Existing imported ordinances are
    re-approved for demo retrieval and missing/disabled embeddings are refreshed.
    """

    _ensure_text_embedding_column(db)
    ensure_initial_official_legal_sources(db)
    official_source = _get_bop_burgos_source(db)

    imported: list[dict] = []
    for source in DEMO_ORDINANCE_SOURCES:
        _validate_official_source_url(source.source_url)
        municipality = _get_or_create_municipality(db, source.municipality_name)
        ordinance = _find_existing_ordinance(db, source.source_url)
        if ordinance is not None and not ordinance.legal_chunks:
            _discard_incomplete_ordinance(db, ordinance)
            ordinance = None
        item: OrdinanceImportItem | None = None
        if ordinance is None:
            job = _create_import_job(db, source, municipality, official_source)
            run_import_job(job.id, db=db)
            db.refresh(job)
            item = _first_item_for_job(db, job.id)
            if item is None or item.ordinance is None:
                imported.append(
                    {
                        "municipality": source.municipality_name,
                        "source_url": source.source_url,
                        "status": "failed",
                        "error": item.error_message if item else "No import item created",
                    }
                )
                continue
            ordinance = item.ordinance
        else:
            item = _find_import_item_for_ordinance(db, ordinance.id)

        _approve_for_demo(db, ordinance, item, source)
        imported.append(_summarize_ordinance(ordinance, source, item))

    db.commit()
    return {
        "official_source_domain": official_source.domain,
        "imported": imported,
        "metrics": _corpus_metrics(db),
        "smoke_searches": _smoke_searches(db),
    }


def _ensure_text_embedding_column(db: Session) -> None:
    """Keep existing dev DBs aligned with the current SQLAlchemy model.

    An earlier local migration attempt converted embeddings to pgvector when the
    extension existed, but the application stores JSON string vectors and scores
    them in Python. Convert back to text so import jobs can write embeddings.
    """

    column_type = db.execute(
        sql_text(
            """
            SELECT udt_name
            FROM information_schema.columns
            WHERE table_name = 'ordinance_legal_chunks'
              AND column_name = 'embedding'
            """
        )
    ).scalar()
    if column_type == "vector":
        db.execute(
            sql_text(
                """
                ALTER TABLE ordinance_legal_chunks
                ALTER COLUMN embedding TYPE text
                USING embedding::text
                """
            )
        )
        db.commit()


def _get_bop_burgos_source(db: Session) -> OfficialLegalSource:
    source = db.scalar(
        select(OfficialLegalSource).where(
            OfficialLegalSource.domain == BOP_BURGOS_DOMAIN,
            OfficialLegalSource.status == "active",
        )
    )
    if source is None:
        raise RuntimeError("BOP Burgos official source is not active in the database")
    return source


def _validate_official_source_url(url: str) -> None:
    host = (urlparse.urlparse(url).hostname or "").lower()
    if host != BOP_BURGOS_DOMAIN:
        raise ValueError(f"Demo source is not BOP Burgos: {url}")


def _get_or_create_municipality(db: Session, name: str) -> Municipality:
    municipality = db.scalar(
        select(Municipality).where(
            Municipality.name == name,
            Municipality.province == BURGOS_PROVINCE,
            Municipality.autonomous_community == BURGOS_AUTONOMOUS_COMMUNITY,
        )
    )
    if municipality is not None:
        return municipality
    municipality = Municipality(
        name=name,
        province=BURGOS_PROVINCE,
        autonomous_community=BURGOS_AUTONOMOUS_COMMUNITY,
        rural_urban_profile="rural",
        administrative_notes="Alta automática para demo Sprint 0 Burgos.",
    )
    db.add(municipality)
    db.flush()
    return municipality


def _find_existing_ordinance(db: Session, source_url: str) -> Ordinance | None:
    return db.scalar(
        select(Ordinance)
        .options(selectinload(Ordinance.legal_chunks), selectinload(Ordinance.municipality))
        .where(Ordinance.source_url == source_url)
    )


def _discard_incomplete_ordinance(db: Session, ordinance: Ordinance) -> None:
    db.delete(ordinance)
    db.commit()


def _create_import_job(
    db: Session,
    source: DemoOrdinanceSource,
    municipality: Municipality,
    official_source: OfficialLegalSource,
) -> OrdinanceImportJob:
    job = OrdinanceImportJob(
        title=f"Demo Sprint 0 Burgos - {source.municipality_name} - {source.topic}",
        description=(
            "Importación controlada de ordenanza real para demo del MVP Burgos "
            f"desde {source.publication_date_label}."
        ),
        topic=source.topic,
        subtopic=source.subtopic,
        search_query=None,
        municipality_ids_json=json.dumps([municipality.id]),
        official_source_ids_json=json.dumps([official_source.id]),
        source_urls_json=json.dumps(
            [
                {
                    "url": source.source_url,
                    "municipality_id": municipality.id,
                    "official_source_id": official_source.id,
                    "title": source.title,
                }
            ],
            ensure_ascii=False,
        ),
        review_criteria=(
            "Fuente BOP Burgos oficial, municipio esperado, texto extraíble, "
            "título de ordenanza y materia útil para demo."
        ),
        status="draft",
    )
    db.add(job)
    db.commit()
    return job


def _first_item_for_job(db: Session, job_id: int) -> OrdinanceImportItem | None:
    return db.scalar(
        select(OrdinanceImportItem)
        .options(
            selectinload(OrdinanceImportItem.ordinance).selectinload(
                Ordinance.legal_chunks
            ),
            selectinload(OrdinanceImportItem.ordinance).selectinload(
                Ordinance.municipality
            ),
        )
        .where(OrdinanceImportItem.job_id == job_id)
        .order_by(OrdinanceImportItem.id)
    )


def _find_import_item_for_ordinance(
    db: Session,
    ordinance_id: int,
) -> OrdinanceImportItem | None:
    return db.scalar(
        select(OrdinanceImportItem)
        .options(selectinload(OrdinanceImportItem.ordinance))
        .where(OrdinanceImportItem.ordinance_id == ordinance_id)
        .order_by(OrdinanceImportItem.id.desc())
    )


def _approve_for_demo(
    db: Session,
    ordinance: Ordinance,
    item: OrdinanceImportItem | None,
    source: DemoOrdinanceSource,
) -> None:
    ordinance.title = source.title
    ordinance.topic = source.topic
    ordinance.subtopic = source.subtopic
    ordinance.ordinance_type = "tax_ordinance" if "tasa" in source.title.lower() else "ordinance"
    ordinance.official_bulletin = "Boletín Oficial de la Provincia de Burgos"
    ordinance.bulletin_number = source.bulletin_number
    ordinance.status = "active"
    ordinance.curation_status = "approved"
    ordinance.extraction_status = "extracted"
    ordinance.legal_review_notes = DEMO_REVIEW_NOTE
    ordinance.notes = source.publication_date_label
    if item is not None:
        item.status = "approved"
        item.error_message = None
    now = datetime.now(UTC)
    for chunk in ordinance.legal_chunks:
        chunk.review_status = "approved"
        if chunk.embedding_status != "ready" or not chunk.embedding:
            embedding, model, status = embed_text(chunk.text)
            chunk.embedding = embedding
            chunk.embedding_model = model
            chunk.embedding_status = status
            chunk.embedded_at = now if status == "ready" else None
    db.flush()


def _summarize_ordinance(
    ordinance: Ordinance,
    source: DemoOrdinanceSource,
    item: OrdinanceImportItem | None,
) -> dict:
    ready_chunks = [
        chunk
        for chunk in ordinance.legal_chunks
        if chunk.review_status == "approved" and chunk.embedding_status == "ready"
    ]
    return {
        "municipality": ordinance.municipality.name,
        "ordinance_id": ordinance.id,
        "import_item_id": item.id if item else None,
        "status": item.status if item else "existing",
        "curation_status": ordinance.curation_status,
        "title": ordinance.title,
        "topic": ordinance.topic,
        "bulletin_number": source.bulletin_number,
        "source_url": source.source_url,
        "approved_ready_chunks": len(ready_chunks),
    }


def _corpus_metrics(db: Session) -> dict:
    ordinances = list(
        db.scalars(
            select(Ordinance)
            .join(Ordinance.municipality)
            .where(
                Municipality.province == BURGOS_PROVINCE,
                Ordinance.source_url.in_([source.source_url for source in DEMO_ORDINANCE_SOURCES]),
            )
        )
    )
    chunks = list(
        db.scalars(
            select(OrdinanceLegalChunk)
            .join(OrdinanceLegalChunk.ordinance)
            .where(Ordinance.source_url.in_([source.source_url for source in DEMO_ORDINANCE_SOURCES]))
        )
    )
    return {
        "demo_ordinances": len(ordinances),
        "approved_ordinances": sum(1 for ordinance in ordinances if ordinance.curation_status == "approved"),
        "chunks": len(chunks),
        "approved_ready_chunks": sum(
            1
            for chunk in chunks
            if chunk.review_status == "approved" and chunk.embedding_status == "ready"
        ),
    }


def _smoke_searches(db: Session) -> list[dict]:
    searches = [
        ("recogida de basuras residuos sólidos", "residuos"),
        ("suministro domiciliario de agua potable", "agua"),
    ]
    return [_smoke_search(db, query, expected_topic) for query, expected_topic in searches]


def _smoke_search(db: Session, query_text: str, expected_topic: str) -> dict:
    query_vector, _, status = embed_text(query_text)
    if status != "ready" or query_vector is None:
        return {"query": query_text, "results": 0, "top": None}
    query = (
        select(OrdinanceLegalChunk)
        .join(OrdinanceLegalChunk.ordinance)
        .join(Ordinance.municipality)
        .where(
            Municipality.province == BURGOS_PROVINCE,
            Ordinance.curation_status == "approved",
            OrdinanceLegalChunk.review_status == "approved",
            OrdinanceLegalChunk.embedding_status == "ready",
        )
        .options(
            selectinload(OrdinanceLegalChunk.ordinance).selectinload(
                Ordinance.municipality
            )
        )
        .limit(500)
    )
    scored = [
        (vector_similarity(query_vector, chunk.embedding), chunk)
        for chunk in db.scalars(query)
    ]
    scored = [(score, chunk) for score, chunk in scored if score > 0]
    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[0][1] if scored else None
    return {
        "query": query_text,
        "expected_topic": expected_topic,
        "results": len(scored),
        "top": None
        if top is None
        else {
            "ordinance_id": top.ordinance_id,
            "municipality": top.ordinance.municipality.name,
            "title": top.ordinance.title,
            "topic": top.ordinance.topic,
            "source_url": top.source_url or top.ordinance.source_url,
        },
    }


def main() -> None:
    db = SessionLocal()
    try:
        summary = bootstrap_burgos_demo_ordinances(db)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
