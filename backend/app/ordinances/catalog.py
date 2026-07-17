"""Exact, deterministic inventory over the internal ordinance corpus.

Semantic retrieval answers questions about ordinance contents.  This module
serves a different purpose: count and enumerate every eligible ordinance in a
stable corpus snapshot without depending on embeddings or relevance scores.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from app.core.config import settings
from app.municipalities.models import Municipality
from app.ordinances.models import (
    Ordinance,
    OrdinanceImportItem,
    OrdinanceLegalChunk,
)
from app.ordinances.search import DEFINITIVELY_INACTIVE_STATUSES
from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session, selectinload

MAX_CATALOG_PAGE_SIZE = 100
MAX_CATALOG_CURSOR_CHARS = 4096
CORPUS_SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class OrdinanceCorpusFilters:
    autonomous_community: str | None = None
    province: str | None = None
    municipality_id: int | None = None
    municipality_name: str | None = None
    population_gte: int | None = None
    population_lt: int | None = None
    include_pending: bool = False
    include_inactive: bool = False


@dataclass(frozen=True)
class OrdinanceCatalogOptions(OrdinanceCorpusFilters):
    limit: int = 20
    after_id: int | None = None
    snapshot_id: str | None = None


class InvalidOrdinanceCatalogCursor(ValueError):
    """Raised when a catalogue cursor is malformed, forged or obsolete."""


def build_ordinance_corpus_manifest(
    db: Session,
    *,
    embedding_model: str,
    filters: OrdinanceCorpusFilters,
) -> dict[str, Any]:
    """Return exact denominators for one filtered internal-corpus snapshot."""

    _validate_filters(filters)
    snapshot_id = _corpus_snapshot_id(db, filters)
    population = _population_coverage(db, filters)
    ordinance_counts = _ordinance_counts(db, filters)
    chunk_counts = _chunk_counts(db, filters, embedding_model)
    import_counts = _import_counts(db, filters)
    by_province = _province_coverage(db, filters, embedding_model)

    catalog_ordinances = ordinance_counts["catalog_ordinances"]
    population_filter_applied = (
        filters.population_gte is not None or filters.population_lt is not None
    )
    population_coverage_complete = (
        not population_filter_applied
        or population["municipalities_without_population"] == 0
    )
    municipality_identity_complete = population["municipalities_without_ine_code"] == 0
    final_snapshot_id = _corpus_snapshot_id(db, filters)
    if final_snapshot_id != snapshot_id:
        raise ValueError(
            "El corpus cambió mientras se preparaba el manifiesto; vuelve a intentarlo"
        )
    return {
        "snapshot_id": snapshot_id,
        "snapshot_schema_version": CORPUS_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": _serialize_filters(filters),
        "catalog_cursor": encode_ordinance_catalog_cursor(
            filters=filters,
            snapshot_id=snapshot_id,
            total=ordinance_counts["catalog_ordinances"],
            after_id=0,
            consumed=0,
        ),
        "eligibility": {
            "curation": (
                "approved_or_non_rejected"
                if filters.include_pending
                else "approved_only"
            ),
            "inactive_legal_statuses_included": filters.include_inactive,
            "excluded_legal_statuses": (
                [] if filters.include_inactive else list(DEFINITIVELY_INACTIVE_STATUSES)
            ),
            "population_unknown_records_are_excluded_when_filtered": (
                population_filter_applied
            ),
        },
        "population_coverage": {
            **population,
            "filter_applied": population_filter_applied,
            "coverage_complete": population_coverage_complete,
        },
        "layers": {
            **import_counts,
            **ordinance_counts,
            **chunk_counts,
        },
        "by_province": by_province,
        "reconciliation": {
            "catalog_ordinances": catalog_ordinances,
            "with_chunks": chunk_counts["ordinances_with_chunks"],
            "without_chunks": chunk_counts["ordinances_without_chunks"],
            "with_searchable_chunks": chunk_counts["ordinances_with_searchable_chunks"],
            "without_searchable_chunks": chunk_counts[
                "ordinances_without_searchable_chunks"
            ],
            "ordinance_partition_balanced": (
                chunk_counts["ordinances_with_chunks"]
                + chunk_counts["ordinances_without_chunks"]
                == catalog_ordinances
            ),
        },
        "completeness": {
            "manifest_counts_complete": True,
            "catalog_snapshot_complete": (
                population_coverage_complete and municipality_identity_complete
            ),
            "municipality_identity_complete": municipality_identity_complete,
            "complete_against_official_sources": False,
            "official_source_coverage": "not_verified",
            "can_claim_all_official_ordinances": False,
            "limitations": [
                (
                    "El manifiesto demuestra cobertura dentro de la base de "
                    "datos interna, no exhaustividad frente a todos los "
                    "boletines oficiales."
                ),
                (
                    "El estado approved representa curación interna y no "
                    "certifica por sí solo vigencia jurídica."
                ),
            ],
        },
    }


def list_ordinance_catalog_from_cursor(
    db: Session,
    *,
    embedding_model: str,
    cursor: str,
    limit: int,
) -> dict[str, Any]:
    """Read a page using an opaque cursor issued by the corpus manifest."""

    cursor_data = decode_ordinance_catalog_cursor(cursor)
    filters = OrdinanceCorpusFilters(**cursor_data["filters"])
    page = list_ordinance_catalog(
        db,
        embedding_model=embedding_model,
        options=OrdinanceCatalogOptions(
            **asdict(filters),
            limit=limit,
            after_id=cursor_data["after_id"],
            snapshot_id=cursor_data["snapshot_id"],
        ),
    )
    consumed = cursor_data["consumed"] + page["returned"]
    if page["total_catalog_ordinances"] != cursor_data["total"]:
        raise ValueError("El corpus cambió desde el manifiesto; solicita uno nuevo")
    next_cursor = None
    if page["has_more"] and page["results"]:
        next_cursor = encode_ordinance_catalog_cursor(
            filters=filters,
            snapshot_id=cursor_data["snapshot_id"],
            total=cursor_data["total"],
            after_id=page["results"][-1]["ordinance_id"],
            consumed=consumed,
        )
    return {
        **page,
        "cursor": cursor,
        "consumed": consumed,
        "remaining": max(cursor_data["total"] - consumed, 0),
        "complete": not page["has_more"],
        "next_cursor": next_cursor,
    }


def encode_ordinance_catalog_cursor(
    *,
    filters: OrdinanceCorpusFilters,
    snapshot_id: str,
    total: int,
    after_id: int,
    consumed: int,
) -> str:
    payload = {
        "v": CORPUS_SNAPSHOT_SCHEMA_VERSION,
        "filters": _serialize_filters(filters),
        "snapshot_id": snapshot_id,
        "total": total,
        "after_id": after_id,
        "consumed": consumed,
    }
    body = urlsafe_b64encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(
        settings.secret_key.encode("utf-8"),
        b"ordinance-catalog-v1\0" + body,
        hashlib.sha256,
    ).digest()
    return (
        body.decode("ascii")
        + "."
        + urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    )


def decode_ordinance_catalog_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor:
        raise InvalidOrdinanceCatalogCursor("cursor es obligatorio")
    if len(cursor) > MAX_CATALOG_CURSOR_CHARS:
        raise InvalidOrdinanceCatalogCursor("cursor supera el tamaño máximo")
    try:
        encoded_body, encoded_signature = cursor.split(".", 1)
        body = encoded_body.encode("ascii")
        signature = _decode_base64(encoded_signature)
        expected = hmac.new(
            settings.secret_key.encode("utf-8"),
            b"ordinance-catalog-v1\0" + body,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise InvalidOrdinanceCatalogCursor("firma de cursor inválida")
        payload = json.loads(_decode_base64(encoded_body).decode("utf-8"))
    except InvalidOrdinanceCatalogCursor:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise InvalidOrdinanceCatalogCursor("cursor no válido") from error
    if not isinstance(payload, dict) or payload.get("v") != (
        CORPUS_SNAPSHOT_SCHEMA_VERSION
    ):
        raise InvalidOrdinanceCatalogCursor("versión de cursor no válida")
    required = {
        "v",
        "filters",
        "snapshot_id",
        "total",
        "after_id",
        "consumed",
    }
    if set(payload) != required or not isinstance(payload["filters"], dict):
        raise InvalidOrdinanceCatalogCursor("contenido de cursor no válido")
    try:
        filters = OrdinanceCorpusFilters(**payload["filters"])
        _validate_filters(filters)
        snapshot_id = str(payload["snapshot_id"])
        total = int(payload["total"])
        after_id = int(payload["after_id"])
        consumed = int(payload["consumed"])
    except (TypeError, ValueError) as error:
        raise InvalidOrdinanceCatalogCursor("contenido de cursor no válido") from error
    if (
        len(snapshot_id) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_id)
        or min(total, after_id, consumed) < 0
        or consumed > total
    ):
        raise InvalidOrdinanceCatalogCursor("contenido de cursor no válido")
    return {
        "filters": asdict(filters),
        "snapshot_id": snapshot_id,
        "total": total,
        "after_id": after_id,
        "consumed": consumed,
    }


def advance_ordinance_catalog_cursor(
    cursor: str,
    *,
    after_id: int,
    returned: int,
) -> str:
    data = decode_ordinance_catalog_cursor(cursor)
    return encode_ordinance_catalog_cursor(
        filters=OrdinanceCorpusFilters(**data["filters"]),
        snapshot_id=data["snapshot_id"],
        total=data["total"],
        after_id=after_id,
        consumed=data["consumed"] + returned,
    )


def _decode_base64(value: str) -> bytes:
    encoded = value.encode("ascii")
    return urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))


def list_ordinance_catalog(
    db: Session,
    *,
    embedding_model: str,
    options: OrdinanceCatalogOptions,
) -> dict[str, Any]:
    """Enumerate one row per ordinance using a stable primary-key cursor."""

    _validate_filters(options)
    if options.limit < 1 or options.limit > MAX_CATALOG_PAGE_SIZE:
        raise ValueError(f"limit debe estar entre 1 y {MAX_CATALOG_PAGE_SIZE}")
    if options.after_id is not None and options.after_id < 0:
        raise ValueError("after_id debe ser mayor o igual que 0")

    snapshot_id = _corpus_snapshot_id(db, options)
    if options.snapshot_id is not None and options.snapshot_id != snapshot_id:
        raise ValueError(
            "El corpus cambió desde la página anterior; reinicia el catálogo sin cursor"
        )

    conditions = _ordinance_conditions(options)
    total = int(
        db.scalar(
            select(func.count(Ordinance.id))
            .select_from(Ordinance)
            .join(Ordinance.municipality)
            .where(*conditions)
        )
        or 0
    )
    statement = (
        select(Ordinance)
        .join(Ordinance.municipality)
        .where(*conditions)
        .options(selectinload(Ordinance.municipality))
        .order_by(Ordinance.id)
        .limit(options.limit + 1)
    )
    if options.after_id is not None:
        statement = statement.where(Ordinance.id > options.after_id)
    candidates = list(db.scalars(statement))
    has_more = len(candidates) > options.limit
    ordinances = candidates[: options.limit]
    chunk_counts = _page_chunk_counts(
        db,
        [ordinance.id for ordinance in ordinances],
        embedding_model,
    )
    results = [
        _serialize_catalog_ordinance(
            ordinance,
            chunk_counts.get(ordinance.id, _empty_page_chunk_counts()),
        )
        for ordinance in ordinances
    ]
    if _corpus_snapshot_id(db, options) != snapshot_id:
        raise ValueError(
            "El corpus cambió durante la lectura de la página; solicita un "
            "manifiesto nuevo"
        )
    next_cursor = ordinances[-1].id if has_more and ordinances else None
    return {
        "snapshot_id": snapshot_id,
        "filters": _serialize_filters(options),
        "ordering": "ordinance_id_asc",
        "after_id": options.after_id,
        "limit": options.limit,
        "returned": len(results),
        "total_catalog_ordinances": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "results": results,
    }


def _validate_filters(filters: OrdinanceCorpusFilters) -> None:
    for value, label in (
        (filters.municipality_id, "municipality_id"),
        (filters.population_gte, "population_gte"),
        (filters.population_lt, "population_lt"),
    ):
        if value is not None and value < 0:
            raise ValueError(f"{label} debe ser mayor o igual que 0")
    if filters.municipality_id == 0:
        raise ValueError("municipality_id debe ser mayor o igual que 1")
    if (
        filters.population_gte is not None
        and filters.population_lt is not None
        and filters.population_gte >= filters.population_lt
    ):
        raise ValueError("population_gte debe ser menor que population_lt")


def _municipality_conditions(
    filters: OrdinanceCorpusFilters,
    *,
    include_population: bool,
) -> list[Any]:
    conditions: list[Any] = [
        Municipality.status == "active",
        Municipality.municipality_type == "municipality",
    ]
    if filters.autonomous_community:
        conditions.append(
            func.lower(Municipality.autonomous_community)
            == filters.autonomous_community.casefold()
        )
    if filters.province:
        conditions.append(
            func.lower(Municipality.province) == filters.province.casefold()
        )
    if filters.municipality_id is not None:
        conditions.append(Municipality.id == filters.municipality_id)
    if filters.municipality_name:
        conditions.append(
            func.lower(Municipality.name) == filters.municipality_name.casefold()
        )
    if include_population:
        if filters.population_gte is not None:
            conditions.append(Municipality.population >= filters.population_gte)
        if filters.population_lt is not None:
            conditions.append(Municipality.population < filters.population_lt)
    return conditions


def _ordinance_conditions(filters: OrdinanceCorpusFilters) -> list[Any]:
    conditions = _municipality_conditions(filters, include_population=True)
    if filters.include_pending:
        conditions.append(Ordinance.curation_status != "rejected")
    else:
        conditions.append(Ordinance.curation_status == "approved")
    if not filters.include_inactive:
        conditions.append(Ordinance.status.not_in(DEFINITIVELY_INACTIVE_STATUSES))
    return conditions


def _serialize_filters(filters: OrdinanceCorpusFilters) -> dict[str, Any]:
    values = asdict(filters)
    values.pop("limit", None)
    values.pop("after_id", None)
    values.pop("snapshot_id", None)
    return values


def _corpus_snapshot_id(
    db: Session,
    filters: OrdinanceCorpusFilters,
) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "schema": CORPUS_SNAPSHOT_SCHEMA_VERSION,
                "filters": _serialize_filters(filters),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    for row in db.execute(
        select(
            Municipality.id,
            Municipality.updated_at,
            Municipality.status,
            Municipality.municipality_type,
            Municipality.ine_code,
            Municipality.province,
            Municipality.autonomous_community,
            Municipality.population,
            Municipality.population_reference_year,
        )
        .where(*_municipality_conditions(filters, include_population=False))
        .order_by(Municipality.id)
    ):
        _update_digest(digest, row)
    conditions = _ordinance_conditions(filters)
    ordinance_rows = db.execute(
        select(
            Ordinance.id,
            Ordinance.updated_at,
            Ordinance.municipality_id,
            Ordinance.curation_status,
            Ordinance.status,
            Municipality.updated_at,
            Municipality.population,
        )
        .join(Ordinance.municipality)
        .where(*conditions)
        .order_by(Ordinance.id)
    ).all()
    ordinance_ids: list[int] = []
    for row in ordinance_rows:
        ordinance_ids.append(int(row[0]))
        _update_digest(digest, row)
    if ordinance_ids:
        for row in db.execute(
            select(
                OrdinanceLegalChunk.id,
                OrdinanceLegalChunk.ordinance_id,
                OrdinanceLegalChunk.updated_at,
                OrdinanceLegalChunk.review_status,
                OrdinanceLegalChunk.embedding_status,
                OrdinanceLegalChunk.embedding_model,
            )
            .where(OrdinanceLegalChunk.ordinance_id.in_(ordinance_ids))
            .order_by(
                OrdinanceLegalChunk.ordinance_id,
                OrdinanceLegalChunk.id,
            )
        ):
            _update_digest(digest, row)
    return digest.hexdigest()


def _update_digest(digest: Any, values: Any) -> None:
    serialized = [_json_scalar(value) for value in values]
    digest.update(
        json.dumps(
            serialized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _json_scalar(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _population_coverage(
    db: Session,
    filters: OrdinanceCorpusFilters,
) -> dict[str, int]:
    scope_conditions = _municipality_conditions(
        filters,
        include_population=False,
    )
    row = db.execute(
        select(
            func.count(Municipality.id),
            func.sum(case((Municipality.population.is_not(None), 1), else_=0)),
            func.sum(case((Municipality.population.is_(None), 1), else_=0)),
            func.sum(case((Municipality.ine_code.is_(None), 1), else_=0)),
            func.sum(
                case(
                    (
                        Municipality.directory_reference_date.is_not(None)
                        & Municipality.ine_code.is_not(None),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (
                        Municipality.population_reference_year.is_not(None)
                        & Municipality.population_source_url.is_not(None)
                        & Municipality.population_source_sha256.is_not(None),
                        1,
                    ),
                    else_=0,
                )
            ),
        ).where(*scope_conditions)
    ).one()
    eligible = int(
        db.scalar(
            select(func.count(Municipality.id)).where(
                *_municipality_conditions(filters, include_population=True)
            )
        )
        or 0
    )
    return {
        "municipalities_in_geographic_scope": int(row[0] or 0),
        "municipalities_with_population": int(row[1] or 0),
        "municipalities_without_population": int(row[2] or 0),
        "municipalities_without_ine_code": int(row[3] or 0),
        "directory_backed_municipalities": int(row[4] or 0),
        "population_provenance_municipalities": int(row[5] or 0),
        "eligible_municipalities": eligible,
    }


def _ordinance_counts(
    db: Session,
    filters: OrdinanceCorpusFilters,
) -> dict[str, Any]:
    conditions = _ordinance_conditions(filters)
    all_conditions = _municipality_conditions(
        filters,
        include_population=True,
    )
    ordinance_records = int(
        db.scalar(
            select(func.count(Ordinance.id))
            .select_from(Ordinance)
            .join(Ordinance.municipality)
            .where(*all_conditions)
        )
        or 0
    )
    row = db.execute(
        select(
            func.count(Ordinance.id),
            func.count(distinct(Ordinance.municipality_id)),
            func.sum(case((Ordinance.document_id.is_not(None), 1), else_=0)),
            func.sum(case((Ordinance.source_url.is_not(None), 1), else_=0)),
            func.sum(case((Ordinance.source_hash.is_not(None), 1), else_=0)),
            func.sum(case((Ordinance.text_content.is_not(None), 1), else_=0)),
        )
        .select_from(Ordinance)
        .join(Ordinance.municipality)
        .where(*conditions)
    ).one()
    return {
        "ordinance_records": ordinance_records,
        "catalog_ordinances": int(row[0] or 0),
        "catalog_municipalities": int(row[1] or 0),
        "ordinances_with_document": int(row[2] or 0),
        "ordinances_with_source_url": int(row[3] or 0),
        "ordinances_with_source_hash": int(row[4] or 0),
        "ordinances_with_full_text": int(row[5] or 0),
        "curation_statuses": _grouped_ordinance_counts(
            db,
            all_conditions,
            Ordinance.curation_status,
        ),
        "legal_statuses": _grouped_ordinance_counts(
            db,
            all_conditions,
            Ordinance.status,
        ),
        "catalog_curation_statuses": _grouped_ordinance_counts(
            db,
            conditions,
            Ordinance.curation_status,
        ),
        "catalog_legal_statuses": _grouped_ordinance_counts(
            db,
            conditions,
            Ordinance.status,
        ),
    }


def _grouped_ordinance_counts(
    db: Session,
    conditions: list[Any],
    column: Any,
) -> dict[str, int]:
    rows = db.execute(
        select(column, func.count(Ordinance.id))
        .select_from(Ordinance)
        .join(Ordinance.municipality)
        .where(*conditions)
        .group_by(column)
        .order_by(column)
    ).all()
    return {str(value): int(count) for value, count in rows}


def _chunk_counts(
    db: Session,
    filters: OrdinanceCorpusFilters,
    embedding_model: str,
) -> dict[str, int]:
    conditions = _ordinance_conditions(filters)
    searchable = (
        (OrdinanceLegalChunk.review_status == "approved")
        & (OrdinanceLegalChunk.embedding_status == "ready")
        & OrdinanceLegalChunk.embedding.is_not(None)
        & (OrdinanceLegalChunk.embedding_model == embedding_model)
    )
    row = db.execute(
        select(
            func.count(OrdinanceLegalChunk.id),
            func.count(distinct(OrdinanceLegalChunk.ordinance_id)),
            func.sum(
                case((OrdinanceLegalChunk.review_status == "approved", 1), else_=0)
            ),
            func.sum(
                case(
                    (OrdinanceLegalChunk.review_status == "pending_review", 1),
                    else_=0,
                )
            ),
            func.sum(
                case((OrdinanceLegalChunk.review_status == "rejected", 1), else_=0)
            ),
            func.sum(case((searchable, 1), else_=0)),
            func.count(distinct(case((searchable, OrdinanceLegalChunk.ordinance_id)))),
            func.sum(
                case((OrdinanceLegalChunk.embedding_status == "failed", 1), else_=0)
            ),
            func.sum(
                case((OrdinanceLegalChunk.embedding_status == "pending", 1), else_=0)
            ),
            func.sum(
                case(
                    (
                        func.length(func.trim(OrdinanceLegalChunk.text)) == 0,
                        1,
                    ),
                    else_=0,
                )
            ),
        )
        .select_from(OrdinanceLegalChunk)
        .join(Ordinance)
        .join(Ordinance.municipality)
        .where(*conditions)
    ).one()
    catalog_ordinances = int(
        db.scalar(
            select(func.count(Ordinance.id))
            .select_from(Ordinance)
            .join(Ordinance.municipality)
            .where(*conditions)
        )
        or 0
    )
    with_chunks = int(row[1] or 0)
    with_searchable = int(row[6] or 0)
    return {
        "total_chunks": int(row[0] or 0),
        "ordinances_with_chunks": with_chunks,
        "ordinances_without_chunks": catalog_ordinances - with_chunks,
        "approved_chunks": int(row[2] or 0),
        "pending_review_chunks": int(row[3] or 0),
        "rejected_chunks": int(row[4] or 0),
        "searchable_chunks": int(row[5] or 0),
        "ordinances_with_searchable_chunks": with_searchable,
        "ordinances_without_searchable_chunks": catalog_ordinances - with_searchable,
        "failed_embedding_chunks": int(row[7] or 0),
        "pending_embedding_chunks": int(row[8] or 0),
        "empty_chunks": int(row[9] or 0),
    }


def _import_counts(
    db: Session,
    filters: OrdinanceCorpusFilters,
) -> dict[str, Any]:
    conditions = _municipality_conditions(filters, include_population=True)
    rows = db.execute(
        select(OrdinanceImportItem.status, func.count(OrdinanceImportItem.id))
        .join(Municipality, Municipality.id == OrdinanceImportItem.municipality_id)
        .where(*conditions)
        .group_by(OrdinanceImportItem.status)
        .order_by(OrdinanceImportItem.status)
    ).all()
    raw_text_count = int(
        db.scalar(
            select(func.count(OrdinanceImportItem.id))
            .join(
                Municipality,
                Municipality.id == OrdinanceImportItem.municipality_id,
            )
            .where(
                *conditions,
                OrdinanceImportItem.raw_text.is_not(None),
                func.length(func.trim(OrdinanceImportItem.raw_text)) > 0,
            )
        )
        or 0
    )
    status_counts = {str(status): int(count) for status, count in rows}
    return {
        "import_items": sum(status_counts.values()),
        "import_items_with_raw_text": raw_text_count,
        "import_statuses": status_counts,
    }


def _province_coverage(
    db: Session,
    filters: OrdinanceCorpusFilters,
    embedding_model: str,
) -> list[dict[str, Any]]:
    population_filtered_conditions = _municipality_conditions(
        filters,
        include_population=True,
    )
    scope_conditions = _municipality_conditions(
        filters,
        include_population=False,
    )
    provinces: dict[str, dict[str, Any]] = {}
    for province, total, missing_population in db.execute(
        select(
            Municipality.province,
            func.count(Municipality.id),
            func.sum(case((Municipality.population.is_(None), 1), else_=0)),
        )
        .where(*scope_conditions)
        .group_by(Municipality.province)
        .order_by(Municipality.province)
    ):
        provinces[str(province)] = {
            "province": str(province),
            "municipalities_in_scope": int(total or 0),
            "municipalities_without_population": int(missing_population or 0),
            "eligible_municipalities": 0,
            "catalog_municipalities": 0,
            "catalog_ordinances": 0,
            "total_chunks": 0,
            "searchable_chunks": 0,
        }
    for province, eligible in db.execute(
        select(Municipality.province, func.count(Municipality.id))
        .where(*population_filtered_conditions)
        .group_by(Municipality.province)
    ):
        provinces.setdefault(str(province), {"province": str(province)})[
            "eligible_municipalities"
        ] = int(eligible or 0)

    ordinance_conditions = _ordinance_conditions(filters)
    for province, municipalities, ordinances in db.execute(
        select(
            Municipality.province,
            func.count(distinct(Ordinance.municipality_id)),
            func.count(Ordinance.id),
        )
        .select_from(Ordinance)
        .join(Ordinance.municipality)
        .where(*ordinance_conditions)
        .group_by(Municipality.province)
    ):
        data = provinces.setdefault(str(province), {"province": str(province)})
        data["catalog_municipalities"] = int(municipalities or 0)
        data["catalog_ordinances"] = int(ordinances or 0)

    searchable = (
        (OrdinanceLegalChunk.review_status == "approved")
        & (OrdinanceLegalChunk.embedding_status == "ready")
        & OrdinanceLegalChunk.embedding.is_not(None)
        & (OrdinanceLegalChunk.embedding_model == embedding_model)
    )
    for province, chunks, searchable_chunks in db.execute(
        select(
            Municipality.province,
            func.count(OrdinanceLegalChunk.id),
            func.sum(case((searchable, 1), else_=0)),
        )
        .select_from(OrdinanceLegalChunk)
        .join(Ordinance)
        .join(Ordinance.municipality)
        .where(*ordinance_conditions)
        .group_by(Municipality.province)
    ):
        data = provinces.setdefault(str(province), {"province": str(province)})
        data["total_chunks"] = int(chunks or 0)
        data["searchable_chunks"] = int(searchable_chunks or 0)
    return [provinces[name] for name in sorted(provinces)]


def _page_chunk_counts(
    db: Session,
    ordinance_ids: list[int],
    embedding_model: str,
) -> dict[int, dict[str, int]]:
    if not ordinance_ids:
        return {}
    searchable = (
        (OrdinanceLegalChunk.review_status == "approved")
        & (OrdinanceLegalChunk.embedding_status == "ready")
        & OrdinanceLegalChunk.embedding.is_not(None)
        & (OrdinanceLegalChunk.embedding_model == embedding_model)
    )
    rows = db.execute(
        select(
            OrdinanceLegalChunk.ordinance_id,
            func.count(OrdinanceLegalChunk.id),
            func.sum(
                case((OrdinanceLegalChunk.review_status == "approved", 1), else_=0)
            ),
            func.sum(case((searchable, 1), else_=0)),
            func.sum(
                case((OrdinanceLegalChunk.embedding_status == "failed", 1), else_=0)
            ),
        )
        .where(OrdinanceLegalChunk.ordinance_id.in_(ordinance_ids))
        .group_by(OrdinanceLegalChunk.ordinance_id)
    ).all()
    return {
        int(ordinance_id): {
            "total": int(total or 0),
            "approved": int(approved or 0),
            "searchable": int(searchable_count or 0),
            "embedding_failed": int(failed or 0),
        }
        for ordinance_id, total, approved, searchable_count, failed in rows
    }


def _empty_page_chunk_counts() -> dict[str, int]:
    return {
        "total": 0,
        "approved": 0,
        "searchable": 0,
        "embedding_failed": 0,
    }


def _serialize_catalog_ordinance(
    ordinance: Ordinance,
    chunk_counts: dict[str, int],
) -> dict[str, Any]:
    municipality = ordinance.municipality
    return {
        "ordinance_id": ordinance.id,
        "document_id": ordinance.document_id,
        "municipality": {
            "id": municipality.id,
            "ine_code": municipality.ine_code,
            "name": municipality.name,
            "province": municipality.province,
            "autonomous_community": municipality.autonomous_community,
            "population": municipality.population,
            "population_reference_year": municipality.population_reference_year,
        },
        "title": ordinance.title,
        "topic": ordinance.topic,
        "subtopic": ordinance.subtopic,
        "ordinance_type": ordinance.ordinance_type,
        "curation_status": ordinance.curation_status,
        "curation_status_meaning": "internal_corpus_review",
        "legal_status": ordinance.status,
        "legal_status_requires_validation": True,
        "approval_date": _json_scalar(ordinance.approval_date),
        "publication_date": _json_scalar(ordinance.publication_date),
        "effective_date": _json_scalar(ordinance.effective_date),
        "official_bulletin": ordinance.official_bulletin,
        "bulletin_number": ordinance.bulletin_number,
        "source_url": ordinance.source_url,
        "source_hash": ordinance.source_hash,
        "extraction_status": ordinance.extraction_status,
        "chunk_counts": chunk_counts,
        "updated_at": _json_scalar(ordinance.updated_at),
    }
