"""Durable, metadata-only inventory analysis for the ordinance corpus.

This workflow deliberately does not call an LLM and never copies ordinance or
chunk text into task state, item results, RQ arguments or audit events.  Its
claims are limited to metadata already present in the curated internal catalog
and to aggregate chunk readiness statistics.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_office.models import (
    AgentOfficeOrdinanceAnalysisItem,
    AgentOfficeTask,
)
from app.core.config import settings
from app.ordinances.catalog import (
    OrdinanceCorpusFilters,
    build_ordinance_corpus_manifest,
    list_ordinance_catalog_from_cursor,
    validate_ordinance_corpus_filters,
)
from app.rbac.permissions import has_permission
from app.users.models import User

ANALYZE_ORDINANCE_CORPUS_ACTION = "analyze_ordinance_corpus"
ORDINANCE_ANALYSIS_WORKFLOW_VERSION = 1
ORDINANCE_ANALYSIS_CLASSIFIER_VERSION = "metadata-chunk-stats-v1"
ORDINANCE_ANALYSIS_CATALOG_PAGE_SIZE = 100
ORDINANCE_ANALYSIS_PAGES_PER_RQ_JOB = 1
ORDINANCE_ANALYSIS_MAX_RESUME_PASSES = 3
ORDINANCE_ANALYSIS_MAX_ITEM_ATTEMPTS = ORDINANCE_ANALYSIS_MAX_RESUME_PASSES + 1

_FILTER_KEYS = frozenset(
    {
        "autonomous_community",
        "province",
        "municipality_id",
        "municipality_name",
        "population_gte",
        "population_lt",
        "include_pending",
        "include_inactive",
    }
)
_IGNORED_TASK_INPUT_KEYS = frozenset({"organization_id"})


@dataclass(frozen=True)
class OrdinanceAnalysisPageOutcome:
    state: dict[str, Any]
    page_candidates: int
    created_items: int
    completed_items: int
    failed_items: int
    skipped_completed_items: int
    complete: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _catalog_source_digest(catalog_row: dict[str, Any]) -> str:
    return hashlib.sha256(_json_dumps(catalog_row).encode("utf-8")).hexdigest()


def _source_updated_at(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("updated_at de ordenanza no es válido")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("updated_at de ordenanza no es válido") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} debe ser texto")
    normalized = value.strip()
    return normalized or None


def _optional_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} debe ser un entero")
    return value


def _optional_bool(
    value: object,
    *,
    field: str,
    default: bool = False,
) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} debe ser booleano")
    return value


def ordinance_analysis_filters_from_task(
    task: AgentOfficeTask,
) -> OrdinanceCorpusFilters:
    task_input = task.input
    unexpected = set(task_input) - _FILTER_KEYS - _IGNORED_TASK_INPUT_KEYS
    if unexpected:
        fields = ", ".join(sorted(unexpected))
        raise ValueError(
            "Campos no admitidos para analyze_ordinance_corpus: " f"{fields}"
        )
    filters = OrdinanceCorpusFilters(
        autonomous_community=_optional_text(
            task_input.get("autonomous_community"),
            field="autonomous_community",
        ),
        province=_optional_text(
            task_input.get("province"),
            field="province",
        ),
        municipality_id=_optional_int(
            task_input.get("municipality_id"),
            field="municipality_id",
        ),
        municipality_name=_optional_text(
            task_input.get("municipality_name"),
            field="municipality_name",
        ),
        population_gte=_optional_int(
            task_input.get("population_gte"),
            field="population_gte",
        ),
        population_lt=_optional_int(
            task_input.get("population_lt"),
            field="population_lt",
        ),
        include_pending=_optional_bool(
            task_input.get("include_pending"),
            field="include_pending",
        ),
        include_inactive=_optional_bool(
            task_input.get("include_inactive"),
            field="include_inactive",
        ),
    )
    validate_ordinance_corpus_filters(filters)
    return filters


def require_ordinance_analysis_access(
    db: Session,
    user: User,
    filters: OrdinanceCorpusFilters,
) -> None:
    can_compare = has_permission(
        user,
        "ordinances.compare",
        db,
    ) or has_permission(user, "ordinances.manage", db)
    if not can_compare:
        raise HTTPException(
            status_code=403,
            detail="Permission required: ordinances.compare",
        )
    if filters.include_pending and not (
        has_permission(user, "ordinances.review", db)
        or has_permission(user, "ordinances.manage", db)
    ):
        raise HTTPException(
            status_code=403,
            detail="Permission required: ordinances.review",
        )


def initialize_ordinance_analysis_state(
    db: Session,
    task: AgentOfficeTask,
    user: User,
) -> dict[str, Any]:
    filters = ordinance_analysis_filters_from_task(task)
    require_ordinance_analysis_access(db, user, filters)
    embedding_model = settings.embeddings_model
    manifest = build_ordinance_corpus_manifest(
        db,
        embedding_model=embedding_model,
        filters=filters,
    )
    catalog_total = int(manifest["reconciliation"]["catalog_ordinances"])
    return {
        "mode": "ordinance_corpus_analysis",
        "workflow_version": ORDINANCE_ANALYSIS_WORKFLOW_VERSION,
        "classifier_version": ORDINANCE_ANALYSIS_CLASSIFIER_VERSION,
        "phase": "enumerating",
        "snapshot_id": str(manifest["snapshot_id"]),
        "snapshot_schema_version": int(manifest["snapshot_schema_version"]),
        "embedding_model": embedding_model,
        "filters": dict(manifest["filters"]),
        "initial_cursor": str(manifest["catalog_cursor"]),
        "next_cursor": str(manifest["catalog_cursor"]),
        "expected_ordinances": catalog_total,
        "catalog_consumed": 0,
        "enumerated_ordinances": 0,
        "pages_completed": 0,
        "resume_passes": 0,
        "resume_required": False,
        "last_ordinance_id": None,
        "started_at": _utc_now().isoformat(),
        "updated_at": _utc_now().isoformat(),
        "manifest": {
            "generated_at": manifest["generated_at"],
            "eligibility": manifest["eligibility"],
            "population_coverage": manifest["population_coverage"],
            "layers": manifest["layers"],
            "pipeline": manifest["pipeline"],
            "reconciliation": manifest["reconciliation"],
            "completeness": manifest["completeness"],
        },
        "limitations": [
            (
                "Clasificación basada solo en metadata del catálogo y "
                "estadísticas de chunks; no analiza el texto normativo."
            ),
            (
                "El resultado demuestra inventario del snapshot interno, no "
                "exhaustividad frente a boletines oficiales."
            ),
            (
                "Los estados internos no constituyen validación jurídica de "
                "vigencia ni competencia municipal."
            ),
        ],
    }


def validate_ordinance_analysis_state(
    state: dict[str, Any],
) -> None:
    if (
        state.get("mode") != "ordinance_corpus_analysis"
        or state.get("workflow_version") != ORDINANCE_ANALYSIS_WORKFLOW_VERSION
        or not isinstance(state.get("snapshot_id"), str)
        or not isinstance(state.get("embedding_model"), str)
        or not isinstance(state.get("initial_cursor"), str)
        or state.get("phase") not in {"enumerating", "enumeration_complete"}
    ):
        raise ValueError("El checkpoint de analyze_ordinance_corpus no es válido")
    if state["phase"] == "enumerating" and not isinstance(
        state.get("next_cursor"),
        str,
    ):
        raise ValueError("El checkpoint no contiene cursor de continuación")
    resume_passes = state.get("resume_passes")
    if (
        isinstance(resume_passes, bool)
        or not isinstance(resume_passes, int)
        or resume_passes < 0
    ):
        raise ValueError(
            "El checkpoint contiene un contador de reanudaciones no válido"
        )


def verify_ordinance_analysis_snapshot(
    db: Session,
    state: dict[str, Any],
) -> None:
    validate_ordinance_analysis_state(state)
    filters = OrdinanceCorpusFilters(**state["filters"])
    manifest = build_ordinance_corpus_manifest(
        db,
        embedding_model=str(state["embedding_model"]),
        filters=filters,
    )
    current_snapshot = str(manifest["snapshot_id"])
    current_total = int(manifest["reconciliation"]["catalog_ordinances"])
    if current_snapshot != state["snapshot_id"] or current_total != int(
        state["expected_ordinances"]
    ):
        raise ValueError(
            "El corpus cambió después de la enumeración; el "
            "resultado no puede declararse completo"
        )


def prepare_ordinance_analysis_resume(
    db: Session,
    task: AgentOfficeTask,
    state: dict[str, Any],
) -> dict[str, Any]:
    validate_ordinance_analysis_state(state)
    incomplete = int(
        db.scalar(
            select(func.count(AgentOfficeOrdinanceAnalysisItem.id)).where(
                AgentOfficeOrdinanceAnalysisItem.task_id == task.id,
                AgentOfficeOrdinanceAnalysisItem.status != "completed",
            )
        )
        or 0
    )
    if (
        state["phase"] == "enumeration_complete"
        and incomplete
        and state.get("resume_required") is True
    ):
        resume_passes = int(state.get("resume_passes") or 0)
        if resume_passes >= ORDINANCE_ANALYSIS_MAX_RESUME_PASSES:
            raise ValueError(
                "El análisis alcanzó el límite de reanudaciones; "
                "requiere revisión manual"
            )
        state = dict(state)
        state["phase"] = "enumerating"
        state["next_cursor"] = state["initial_cursor"]
        state["catalog_consumed"] = 0
        state["last_ordinance_id"] = None
        state["resume_passes"] = resume_passes + 1
        state["resume_required"] = False
        state["updated_at"] = _utc_now().isoformat()
    return state


def _retrieval_readiness(chunk_counts: dict[str, Any]) -> str:
    if int(chunk_counts.get("searchable") or 0) > 0:
        return "searchable"
    if int(chunk_counts.get("total") or 0) > 0:
        return "chunks_not_searchable"
    return "no_chunks"


def build_ordinance_inventory_result(
    catalog_row: dict[str, Any],
) -> dict[str, Any]:
    """Build a bounded result without ordinance or chunk body text."""

    municipality = dict(catalog_row.get("municipality") or {})
    chunk_counts = {
        key: int(value or 0)
        for key, value in dict(catalog_row.get("chunk_counts") or {}).items()
    }
    topic = _optional_text(
        catalog_row.get("topic"),
        field="topic",
    )
    subtopic = _optional_text(
        catalog_row.get("subtopic"),
        field="subtopic",
    )
    return {
        "result_schema_version": 1,
        "classifier_version": (ORDINANCE_ANALYSIS_CLASSIFIER_VERSION),
        "basis": [
            "catalog_metadata",
            "chunk_statistics",
        ],
        "municipality": {
            "id": municipality.get("id"),
            "ine_code": municipality.get("ine_code"),
            "name": municipality.get("name"),
            "province": municipality.get("province"),
            "autonomous_community": municipality.get("autonomous_community"),
            "population": municipality.get("population"),
            "population_reference_year": municipality.get("population_reference_year"),
        },
        "ordinance": {
            "id": catalog_row["ordinance_id"],
            "document_id": catalog_row.get("document_id"),
            "title": catalog_row.get("title"),
            "ordinance_type": catalog_row.get("ordinance_type"),
            "curation_status": catalog_row.get("curation_status"),
            "legal_status": catalog_row.get("legal_status"),
            "approval_date": catalog_row.get("approval_date"),
            "publication_date": catalog_row.get("publication_date"),
            "effective_date": catalog_row.get("effective_date"),
            "source_url": catalog_row.get("source_url"),
            "source_hash": catalog_row.get("source_hash"),
            "updated_at": catalog_row.get("updated_at"),
        },
        "inventory_classification": {
            "functional_topic": (topic or "sin_clasificar_por_metadata"),
            "functional_subtopic": subtopic,
            "retrieval_readiness": _retrieval_readiness(chunk_counts),
            "chunk_counts": chunk_counts,
        },
        "legal_status_requires_validation": True,
        "limitations": [
            "No se ha clasificado el contenido material del texto.",
            (
                "La categoría funcional reproduce metadata interna "
                "y no es una conclusión jurídica."
            ),
        ],
    }


def process_ordinance_analysis_page(
    db: Session,
    task: AgentOfficeTask,
    state: dict[str, Any],
) -> OrdinanceAnalysisPageOutcome:
    state = prepare_ordinance_analysis_resume(
        db,
        task,
        dict(state),
    )
    if state["phase"] == "enumeration_complete":
        return OrdinanceAnalysisPageOutcome(
            state=state,
            page_candidates=0,
            created_items=0,
            completed_items=0,
            failed_items=0,
            skipped_completed_items=0,
            complete=True,
        )

    page = list_ordinance_catalog_from_cursor(
        db,
        embedding_model=str(state["embedding_model"]),
        cursor=str(state["next_cursor"]),
        limit=ORDINANCE_ANALYSIS_CATALOG_PAGE_SIZE,
    )
    if page["snapshot_id"] != state["snapshot_id"]:
        raise ValueError(
            "El snapshot del catálogo no coincide con el fijado " "para la tarea"
        )

    created_items = 0
    completed_items = 0
    failed_items = 0
    skipped_completed_items = 0
    now = _utc_now()
    for catalog_row in page["results"]:
        ordinance_id = int(catalog_row["ordinance_id"])
        source_digest = _catalog_source_digest(catalog_row)
        item = db.scalar(
            select(AgentOfficeOrdinanceAnalysisItem)
            .where(
                AgentOfficeOrdinanceAnalysisItem.task_id == task.id,
                AgentOfficeOrdinanceAnalysisItem.source_ordinance_id == ordinance_id,
            )
            .with_for_update()
        )
        if item is not None and item.status == "completed":
            skipped_completed_items += 1
            continue
        if item is None:
            item = AgentOfficeOrdinanceAnalysisItem(
                task_id=task.id,
                ordinance_id=ordinance_id,
                source_ordinance_id=ordinance_id,
                source_updated_at=_source_updated_at(catalog_row.get("updated_at")),
                source_hash=catalog_row.get("source_hash"),
                source_digest=source_digest,
                status="pending",
                attempts=0,
            )
            db.add(item)
            db.flush()
            created_items += 1
        elif item.source_digest != source_digest:
            raise ValueError(
                "La metadata de una ordenanza cambió dentro del " "snapshot fijado"
            )

        if item.attempts >= ORDINANCE_ANALYSIS_MAX_ITEM_ATTEMPTS:
            item.status = "failed"
            item.completed_at = item.completed_at or _utc_now()
            failed_items += 1
            continue

        item.ordinance_id = ordinance_id
        item.status = "running"
        item.attempts += 1
        item.error_message = None
        item.started_at = now
        item.completed_at = None
        try:
            item.result_json = _json_dumps(
                build_ordinance_inventory_result(catalog_row)
            )
            item.status = "completed"
            item.completed_at = _utc_now()
            completed_items += 1
        except Exception as error:
            item.status = "failed"
            item.result_json = None
            item.error_message = (f"{type(error).__name__}: {error}")[:2000]
            item.completed_at = _utc_now()
            failed_items += 1

    state["next_cursor"] = page["next_cursor"]
    state["catalog_consumed"] = int(page["consumed"])
    state["last_ordinance_id"] = (
        int(page["results"][-1]["ordinance_id"])
        if page["results"]
        else state.get("last_ordinance_id")
    )
    state["pages_completed"] = int(state.get("pages_completed") or 0) + 1
    state["enumerated_ordinances"] = int(
        db.scalar(
            select(func.count(AgentOfficeOrdinanceAnalysisItem.id)).where(
                AgentOfficeOrdinanceAnalysisItem.task_id == task.id
            )
        )
        or 0
    )
    state["phase"] = "enumeration_complete" if page["complete"] else "enumerating"
    state["updated_at"] = _utc_now().isoformat()
    return OrdinanceAnalysisPageOutcome(
        state=state,
        page_candidates=int(page["returned"]),
        created_items=created_items,
        completed_items=completed_items,
        failed_items=failed_items,
        skipped_completed_items=skipped_completed_items,
        complete=bool(page["complete"]),
    )


def reconcile_ordinance_analysis(
    db: Session,
    task: AgentOfficeTask,
    state: dict[str, Any],
) -> dict[str, Any]:
    validate_ordinance_analysis_state(state)
    if state["phase"] != "enumeration_complete":
        raise ValueError("La enumeración no ha terminado; no se puede reconciliar")

    items = list(
        db.scalars(
            select(AgentOfficeOrdinanceAnalysisItem)
            .where(AgentOfficeOrdinanceAnalysisItem.task_id == task.id)
            .order_by(AgentOfficeOrdinanceAnalysisItem.id)
        )
    )
    status_counts = Counter(item.status for item in items)
    topic_counts: Counter[str] = Counter()
    readiness_counts: Counter[str] = Counter()
    for item in items:
        if item.status != "completed":
            continue
        classification = item.result.get(
            "inventory_classification",
            {},
        )
        topic_counts[
            str(classification.get("functional_topic") or "sin_clasificar_por_metadata")
        ] += 1
        readiness_counts[
            str(classification.get("retrieval_readiness") or "unknown")
        ] += 1

    expected = int(state["expected_ordinances"])
    completed = int(status_counts.get("completed", 0))
    failed = int(status_counts.get("failed", 0))
    pending = int(status_counts.get("pending", 0))
    running = int(status_counts.get("running", 0))
    actual = len(items)
    balanced = (
        actual == expected
        and completed == expected
        and failed == 0
        and pending == 0
        and running == 0
    )
    reconciliation = {
        "snapshot_id": state["snapshot_id"],
        "expected_ordinances": expected,
        "item_rows": actual,
        "completed_items": completed,
        "failed_items": failed,
        "pending_items": pending,
        "running_items": running,
        "unique_task_ordinance_items": actual,
        "enumeration_complete": True,
        "balanced": balanced,
    }
    if not balanced:
        raise ValueError(
            "La reconciliación del análisis no cuadra: "
            f"expected={expected}, rows={actual}, "
            f"completed={completed}, failed={failed}, "
            f"pending={pending}, running={running}"
        )

    return {
        "mode": "ordinance_corpus_analysis",
        "workflow_version": ORDINANCE_ANALYSIS_WORKFLOW_VERSION,
        "classifier_version": (ORDINANCE_ANALYSIS_CLASSIFIER_VERSION),
        "status": "completed",
        "snapshot": {
            "id": state["snapshot_id"],
            "schema_version": state["snapshot_schema_version"],
            "embedding_model": state["embedding_model"],
            "filters": state["filters"],
        },
        "progress": {
            "pages_completed": state["pages_completed"],
            "resume_passes": state["resume_passes"],
            "completed_ordinances": completed,
        },
        "manifest": state["manifest"],
        "summary": {
            "functional_topics": dict(sorted(topic_counts.items())),
            "retrieval_readiness": dict(sorted(readiness_counts.items())),
        },
        "reconciliation": reconciliation,
        "limitations": state["limitations"],
        "completed_at": _utc_now().isoformat(),
    }
