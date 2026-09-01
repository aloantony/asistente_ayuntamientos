"""Read-first audit and explicitly confirmed repair for ordinance chunks.

The module never repairs the corpus implicitly. ``audit`` emits a deterministic
plan hash; ``repair`` requires that exact hash and explicit ordinance IDs, then
rebuilds derived chunks as pending review. This keeps previously unindexed text
out of the approved assistant corpus until a person reviews it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.session import SessionLocal
from app.ordinances.embeddings import (
    embedding_vector_is_usable,
    has_searchable_text,
)
from app.ordinances.import_service import (
    ImportSourceError,
    create_review_report,
    dispatch_ordinance_embeddings,
    rebuild_ordinance_chunks,
)
from app.ordinances.models import (
    Ordinance,
    OrdinanceImportItem,
)

PRELIMINARY_TITLE_RE = re.compile(
    r"\b(aprobaci[oó]n\s+(?:inicial|provisional)|consulta\s+p[uú]blica)\b",
    re.IGNORECASE,
)
VALID_LATER_IMPORT_STATUSES = ("approved", "pending_review", "duplicate")


def audit_ordinance_corpus(
    db: Session,
    *,
    ordinance_ids: Sequence[int] | None = None,
) -> dict:
    normalized_ids = sorted(set(ordinance_ids or []))
    query = (
        select(Ordinance)
        .options(selectinload(Ordinance.legal_chunks))
        .order_by(Ordinance.id)
    )
    if normalized_ids:
        query = query.where(Ordinance.id.in_(normalized_ids))

    entries = [_audit_ordinance(ordinance) for ordinance in db.scalars(query)]
    payload = {
        "embedding_model": settings.embeddings_model,
        "chunk_chars": settings.ordinance_chunk_chars,
        "max_chunks": settings.ordinance_import_max_chunks,
        "requested_ordinance_ids": normalized_ids,
        "entries": entries,
    }
    canonical = _canonical_json(payload)
    import_failures = (
        _audit_unresolved_import_failures(db) if not normalized_ids else None
    )
    summary = {
        "ordinances_checked": len(entries),
        "chunk_mismatches": sum(entry["chunk_mismatch"] for entry in entries),
        "chunk_index_mismatches": sum(
            entry["chunk_index_mismatch"] for entry in entries
        ),
        "chunk_limit_exceeded": sum(
            entry["expected_chunk_count"]
            > settings.ordinance_import_max_chunks
            for entry in entries
        ),
        "invalid_chunks": sum(
            len(entry["invalid_chunk_ids"]) for entry in entries
        ),
        "oversized_chunks": sum(
            len(entry["oversized_chunk_ids"]) for entry in entries
        ),
        "invalid_ready_embeddings": sum(
            len(entry["invalid_ready_embedding_chunk_ids"])
            for entry in entries
        ),
        "stale_embeddings": sum(
            len(entry["stale_embedding_chunk_ids"]) for entry in entries
        ),
        "embeddings_not_ready": sum(
            len(entry["embedding_not_ready_chunk_ids"])
            for entry in entries
        ),
        "missing_source_references": sum(
            entry["missing_source_reference"] for entry in entries
        ),
        "missing_legal_text": sum(
            entry["missing_text_content"] for entry in entries
        ),
        "missing_approval_dates": sum(
            entry["missing_approval_date"] for entry in entries
        ),
        "missing_publication_dates": sum(
            entry["missing_publication_date"] for entry in entries
        ),
        "missing_effective_dates": sum(
            entry["missing_effective_date"] for entry in entries
        ),
        "preliminary_titles": sum(
            entry["preliminary_title"] for entry in entries
        ),
        "preliminary_recoverable_titles": sum(
            entry["preliminary_title"]
            and entry["curation_status"] == "approved"
            and entry["status"] not in {"repealed", "superseded", "archived"}
            for entry in entries
        ),
        "pending_human_review": sum(
            entry["legal_review_status"] != "human_approved"
            for entry in entries
        ),
        "unresolved_import_urls": (
            import_failures["unresolved_distinct_urls"]
            if import_failures is not None
            else None
        ),
    }
    return {
        **payload,
        "plan_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "summary": summary,
        "import_failures": import_failures,
    }


def repair_ordinance_chunks(
    db: Session,
    *,
    ordinance_ids: Sequence[int],
    expected_plan_sha256: str,
) -> dict:
    normalized_ids = sorted(set(ordinance_ids))
    if not normalized_ids:
        raise ValueError("repair requires at least one ordinance_id")
    audit = audit_ordinance_corpus(db, ordinance_ids=normalized_ids)
    if audit["plan_sha256"] != expected_plan_sha256:
        raise ValueError(
            "corpus audit drifted; run audit again and confirm the new plan_sha256"
        )
    found_ids = [entry["ordinance_id"] for entry in audit["entries"]]
    if found_ids != normalized_ids:
        raise ValueError("one or more ordinances do not exist")
    if any(entry["missing_text_content"] for entry in audit["entries"]):
        raise ValueError("one or more ordinances have no auditable legal text")

    repaired: list[int] = []
    unchanged: list[int] = []
    unresolved_embedding_issues: list[int] = []
    repair_entries: list[dict] = []
    for entry in audit["entries"]:
        ordinance_id = entry["ordinance_id"]
        if entry["expected_chunk_count"] > settings.ordinance_import_max_chunks:
            raise ImportSourceError(
                f"Ordinance {ordinance_id} exceeds the configured chunk limit"
            )
        if (
            not entry["chunk_mismatch"]
            and not entry["chunk_index_mismatch"]
            and not entry["invalid_chunk_ids"]
        ):
            unchanged.append(ordinance_id)
            if any(
                entry[field]
                for field in (
                    "invalid_ready_embedding_chunk_ids",
                    "stale_embedding_chunk_ids",
                    "embedding_not_ready_chunk_ids",
                )
            ):
                unresolved_embedding_issues.append(ordinance_id)
            continue
        repair_entries.append(entry)

    locked_repairs: list[tuple[dict, Ordinance]] = []
    for entry in repair_entries:
        ordinance_id = entry["ordinance_id"]
        ordinance = db.scalar(
            select(Ordinance)
            .options(selectinload(Ordinance.legal_chunks))
            .where(Ordinance.id == ordinance_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if ordinance is None:
            raise ValueError(f"ordinance disappeared during repair: {ordinance_id}")
        current_entry = _audit_ordinance(ordinance)
        if current_entry != entry:
            db.rollback()
            raise ValueError(
                f"ordinance {ordinance_id} drifted after the confirmed audit"
            )
        locked_repairs.append((entry, ordinance))

    for entry, ordinance in locked_repairs:
        ordinance_id = entry["ordinance_id"]
        import_item = _current_import_item(db, ordinance)
        rebuild_ordinance_chunks(
            db,
            ordinance,
            import_item=import_item,
            review_status="pending_review",
            generate_embeddings=False,
        )
        ordinance.curation_status = (
            "needs_changes"
            if ordinance.curation_status == "approved"
            else "pending_review"
        )
        ordinance.legal_review_status = "pending_review"
        ordinance.legal_reviewed_by_id = None
        ordinance.legal_reviewed_at = None
        if import_item is not None:
            import_item.status = "pending_review"
            import_item.raw_text = ordinance.text_content
            current_report = next(
                (
                    report
                    for report in sorted(
                        import_item.review_reports,
                        key=lambda report: report.id,
                        reverse=True,
                    )
                    if report.status != "superseded"
                ),
                None,
            )
            if current_report is not None:
                current_report.status = "superseded"
            create_review_report(db, ordinance, import_item)
        repaired.append(ordinance_id)

    embedding_dispatch: dict[str, dict] = {}
    if repaired:
        db.commit()
        for ordinance_id in repaired:
            embedding_dispatch[str(ordinance_id)] = dispatch_ordinance_embeddings(
                db,
                ordinance_id,
            )

    return {
        "confirmed_plan_sha256": expected_plan_sha256,
        "repaired_ordinance_ids": repaired,
        "unchanged_ordinance_ids": unchanged,
        "unresolved_embedding_issue_ordinance_ids": unresolved_embedding_issues,
        "embedding_dispatch": embedding_dispatch,
        "all_repaired_content_requires_human_review": True,
    }


def _audit_ordinance(ordinance: Ordinance) -> dict:
    # Importing the private splitter is deliberate here: the audit must compare
    # stored derived rows with the exact current production derivation.
    from app.ordinances.import_service import _split_chunks

    expected = _split_chunks((ordinance.text_content or "").strip())
    actual_chunks = sorted(
        ordinance.legal_chunks,
        key=lambda chunk: (chunk.chunk_index, chunk.id),
    )
    actual = [chunk.text for chunk in actual_chunks]
    expected_indices = list(range(len(actual_chunks)))
    actual_indices = [chunk.chunk_index for chunk in actual_chunks]
    ordinance_record = {
        "id": ordinance.id,
        "municipality_id": ordinance.municipality_id,
        "title": ordinance.title,
        "topic": ordinance.topic,
        "subtopic": ordinance.subtopic,
        "ordinance_type": ordinance.ordinance_type,
        "source_url": ordinance.source_url,
        "official_bulletin": ordinance.official_bulletin,
        "bulletin_number": ordinance.bulletin_number,
        "approval_date": ordinance.approval_date,
        "publication_date": ordinance.publication_date,
        "effective_date": ordinance.effective_date,
        "status": ordinance.status,
        "curation_status": ordinance.curation_status,
        "legal_review_status": ordinance.legal_review_status,
        "text_content": ordinance.text_content,
    }
    stored_chunk_structure = [
        {
            "id": chunk.id,
            "chunk_index": chunk.chunk_index,
            "heading": chunk.heading,
            "citation": chunk.citation,
            "text": chunk.text,
            "source_url": chunk.source_url,
            "source_locator": chunk.source_locator,
            "review_status": chunk.review_status,
        }
        for chunk in actual_chunks
    ]
    return {
        "ordinance_id": ordinance.id,
        "title": ordinance.title,
        "status": ordinance.status,
        "curation_status": ordinance.curation_status,
        "legal_review_status": ordinance.legal_review_status,
        "ordinance_record_sha256": _sha256_json(ordinance_record),
        "expected_chunks_sha256": _sha256_json(expected),
        "stored_chunk_structure_sha256": _sha256_json(
            stored_chunk_structure
        ),
        "missing_text_content": not bool((ordinance.text_content or "").strip()),
        "expected_chunk_count": len(expected),
        "stored_chunk_count": len(actual),
        "chunk_mismatch": actual != expected,
        "chunk_index_mismatch": actual_indices != expected_indices,
        "invalid_chunk_ids": [
            chunk.id for chunk in actual_chunks if not has_searchable_text(chunk.text)
        ],
        "oversized_chunk_ids": [
            chunk.id
            for chunk in actual_chunks
            if len(chunk.text) > settings.ordinance_chunk_chars
        ],
        "invalid_ready_embedding_chunk_ids": [
            chunk.id
            for chunk in actual_chunks
            if chunk.embedding_status == "ready"
            and chunk.embedding_model == settings.embeddings_model
            and not embedding_vector_is_usable(
                chunk.embedding,
                expected_dimensions=settings.embeddings_dimensions,
            )
        ],
        "stale_embedding_chunk_ids": [
            chunk.id
            for chunk in actual_chunks
            if chunk.embedding_status == "ready"
            and chunk.embedding_model != settings.embeddings_model
        ],
        "embedding_not_ready_chunk_ids": [
            chunk.id
            for chunk in actual_chunks
            if chunk.embedding_status != "ready"
        ],
        "missing_source_reference": not (ordinance.source_url or "").strip()
        and not any((chunk.source_url or "").strip() for chunk in actual_chunks),
        "preliminary_title": bool(PRELIMINARY_TITLE_RE.search(ordinance.title)),
        "missing_approval_date": ordinance.approval_date is None,
        "missing_publication_date": ordinance.publication_date is None,
        "missing_effective_date": ordinance.effective_date is None,
    }


def _audit_unresolved_import_failures(db: Session) -> dict:
    rows = list(
        db.execute(
            select(
                OrdinanceImportItem.id,
                OrdinanceImportItem.job_id,
                OrdinanceImportItem.municipality_id,
                OrdinanceImportItem.official_source_id,
                OrdinanceImportItem.ordinance_id,
                OrdinanceImportItem.source_url,
                OrdinanceImportItem.status,
                OrdinanceImportItem.error_message,
            ).order_by(OrdinanceImportItem.id)
        ).mappings()
    )
    rows_by_url: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        rows_by_url[str(row["source_url"])].append(dict(row))

    failed_rows = 0
    resolved_urls: list[str] = []
    unresolved_sources: list[dict] = []
    unresolved_by_host: Counter[str] = Counter()
    unresolved_by_error: Counter[str] = Counter()
    unresolved_by_job: Counter[str] = Counter()
    for source_url, attempts in sorted(rows_by_url.items()):
        failures = [attempt for attempt in attempts if attempt["status"] == "failed"]
        if not failures:
            continue
        failed_rows += len(failures)
        first_failed_id = min(int(attempt["id"]) for attempt in failures)
        resolved_later = any(
            int(attempt["id"]) > first_failed_id
            and attempt["status"] in VALID_LATER_IMPORT_STATUSES
            and attempt["ordinance_id"] is not None
            for attempt in attempts
        )
        if resolved_later:
            resolved_urls.append(source_url)
            continue

        error_messages = sorted(
            {
                (str(attempt["error_message"] or "")[:1000] or "<sin mensaje>")
                for attempt in failures
            }
        )
        job_ids = sorted({int(attempt["job_id"]) for attempt in failures})
        municipality_ids = sorted(
            {
                int(attempt["municipality_id"])
                for attempt in failures
                if attempt["municipality_id"] is not None
            }
        )
        official_source_ids = sorted(
            {
                int(attempt["official_source_id"])
                for attempt in failures
                if attempt["official_source_id"] is not None
            }
        )
        hostname = (urlsplit(source_url).hostname or "<url-inválida>").casefold()
        unresolved_by_host[hostname] += 1
        for error_message in error_messages:
            unresolved_by_error[error_message] += 1
        for job_id in job_ids:
            unresolved_by_job[str(job_id)] += 1
        unresolved_sources.append(
            {
                "source_url": source_url,
                "first_failed_item_id": first_failed_id,
                "latest_failed_item_id": max(
                    int(attempt["id"]) for attempt in failures
                ),
                "failed_attempts": len(failures),
                "municipality_ids": municipality_ids,
                "job_ids": job_ids,
                "official_source_ids": official_source_ids,
                "error_messages": error_messages,
            }
        )

    failed_distinct_urls = len(resolved_urls) + len(unresolved_sources)
    snapshot = {
        "scope": "global",
        "resolution_key": "exact_source_url",
        "valid_later_statuses": list(VALID_LATER_IMPORT_STATUSES),
        "resolution_requires_ordinance_id": True,
        "failed_rows": failed_rows,
        "failed_distinct_urls": failed_distinct_urls,
        "resolved_distinct_urls": len(resolved_urls),
        "unresolved_distinct_urls": len(unresolved_sources),
        "unresolved_by_host": dict(sorted(unresolved_by_host.items())),
        "unresolved_by_error_message": dict(
            sorted(unresolved_by_error.items())
        ),
        "unresolved_by_job_id": dict(sorted(unresolved_by_job.items())),
        "unresolved_sources": unresolved_sources,
    }
    return {
        **snapshot,
        "import_failure_snapshot_sha256": _sha256_json(snapshot),
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def _current_import_item(
    db: Session,
    ordinance: Ordinance,
) -> OrdinanceImportItem | None:
    if ordinance.import_job_id is None:
        return None
    return db.scalar(
        select(OrdinanceImportItem)
        .options(selectinload(OrdinanceImportItem.review_reports))
        .where(
            OrdinanceImportItem.ordinance_id == ordinance.id,
            OrdinanceImportItem.job_id == ordinance.import_job_id,
            OrdinanceImportItem.status != "duplicate",
        )
        .order_by(OrdinanceImportItem.id.desc())
        .limit(1)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("audit", "repair"))
    parser.add_argument(
        "--ordinance-id",
        type=int,
        action="append",
        dest="ordinance_ids",
        default=[],
    )
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.action == "audit":
            result = audit_ordinance_corpus(
                db,
                ordinance_ids=args.ordinance_ids or None,
            )
        else:
            if not args.expected_plan_sha256:
                parser.error("repair requires --expected-plan-sha256")
            result = repair_ordinance_chunks(
                db,
                ordinance_ids=args.ordinance_ids,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
