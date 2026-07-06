"""OCR retry for failed Burgos fiscal ordinance PDFs.

This is an operator script for the Burgos coverage sprint. It retries failed
`ordinance_import_items` from the Diputación Burgos fiscal ordinance fallback by
rendering PDFs with PyMuPDF and OCRing them with Tesseract Spanish.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO

import fitz  # type: ignore[import-untyped]
import pytesseract  # type: ignore[import-untyped]
from PIL import Image  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.session import SessionLocal
from app.ordinances import import_service
from app.ordinances.burgos_full_coverage import approve_successful_job_items
from app.ordinances.models import Ordinance, OrdinanceImportItem

OCR_ERROR_MARKERS = (
    "requiere OCR",
    "texto legible suficiente",
    "texto suficiente",
    "PostgreSQL text fields cannot contain NUL",
    "URL can't contain control characters",
    "No se pudo descargar la fuente",
)


def _candidate_query(job_ids: list[int] | None, limit: int | None):
    query = (
        select(OrdinanceImportItem)
        .options(
            selectinload(OrdinanceImportItem.job),
            selectinload(OrdinanceImportItem.municipality),
            selectinload(OrdinanceImportItem.official_source),
        )
        .where(
            OrdinanceImportItem.status == "failed",
        )
        .order_by(OrdinanceImportItem.id)
    )
    if job_ids:
        query = query.where(OrdinanceImportItem.job_id.in_(job_ids))
    else:
        query = query.where(OrdinanceImportItem.source_url.like("https://www.burgos.es/%"))
    if limit:
        query = query.limit(limit)
    return query


def _is_ocr_candidate(item: OrdinanceImportItem) -> bool:
    message = item.error_message or ""
    return any(marker in message for marker in OCR_ERROR_MARKERS)


def _ocr_pdf(content: bytes, *, zoom: float, lang: str, max_pages: int | None) -> str:
    doc = fitz.open(stream=content, filetype="pdf")
    texts: list[str] = []
    matrix = fitz.Matrix(zoom, zoom)
    pages = list(doc)
    if max_pages is not None:
        pages = pages[:max_pages]
    for page in pages:
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.open(BytesIO(pix.tobytes("png")))
        texts.append(pytesseract.image_to_string(image, lang=lang))
    return import_service._normalize_text("\n\n".join(texts))


def _retry_item(db, item_id: int, *, zoom: float, lang: str, max_pages: int | None) -> str:
    item = db.scalar(
        select(OrdinanceImportItem)
        .options(
            selectinload(OrdinanceImportItem.job),
            selectinload(OrdinanceImportItem.municipality),
            selectinload(OrdinanceImportItem.official_source),
        )
        .where(OrdinanceImportItem.id == item_id)
    )
    if item is None:
        return "missing"
    if not _is_ocr_candidate(item):
        return "skipped_non_ocr"

    try:
        item.status = "fetching"
        item.error_message = None
        db.commit()

        fetched = import_service._fetch_source(item.source_url)
        text = _ocr_pdf(fetched.content, zoom=zoom, lang=lang, max_pages=max_pages)
        if len(text.strip()) < 80 or import_service._readable_text_ratio(text) < 0.55:
            raise import_service.ImportSourceError(
                "OCR no produjo texto legible suficiente; requiere revisión manual."
            )

        source_hash = hashlib.sha256(f"{item.source_url}\n{text}".encode("utf-8")).hexdigest()
        duplicate = db.scalar(select(Ordinance).where(Ordinance.source_hash == source_hash))
        if duplicate is not None:
            item.status = "duplicate"
            item.ordinance_id = duplicate.id
            item.source_hash = source_hash
            item.raw_text = text
            item.error_message = "Fuente duplicada de una ordenanza existente tras OCR."
            db.commit()
            return "duplicate"

        metadata = import_service._extract_metadata(item, text, "application/pdf+ocr")
        ordinance = import_service._create_pending_ordinance(db, item, text, metadata, source_hash)
        item.status = "pending_review"
        item.ordinance_id = ordinance.id
        item.raw_text = text
        item.extracted_metadata_json = json.dumps(metadata, ensure_ascii=False)
        item.source_hash = source_hash
        item.confidence_score = ordinance.confidence_score
        db.commit()

        import_service._create_chunks(db, ordinance, item)
        import_service._create_review_report(db, ordinance, item)
        db.commit()
        return "pending_review"
    except Exception as error:  # noqa: BLE001 - keep batch going and persist item error
        db.rollback()
        item = db.get(OrdinanceImportItem, item_id)
        if item is not None:
            item.status = "failed"
            item.error_message = str(error)[:2000]
            db.commit()
        return "failed"


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR retry for failed Burgos fiscal ordinances")
    parser.add_argument("--job-id", action="append", type=int, dest="job_ids")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--zoom", type=float, default=2.0)
    parser.add_argument("--lang", default="spa")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    with SessionLocal() as db:
        items = list(db.scalars(_candidate_query(args.job_ids, args.limit)))
        item_ids = [item.id for item in items if _is_ocr_candidate(item)]

    counts: dict[str, int] = {
        "selected": len(item_ids),
        "pending_review": 0,
        "duplicate": 0,
        "failed": 0,
        "skipped_non_ocr": 0,
        "missing": 0,
    }
    started = datetime.now(UTC)
    def run_one(item_id: int) -> tuple[int, str]:
        with SessionLocal() as db:
            status = _retry_item(
                db,
                item_id,
                zoom=args.zoom,
                lang=args.lang,
                max_pages=args.max_pages,
            )
        return item_id, status

    if args.workers <= 1:
        completed = (run_one(item_id) for item_id in item_ids)
        for index, (item_id, status) in enumerate(completed, start=1):
            counts[status] = counts.get(status, 0) + 1
            print(f"{index}/{len(item_ids)} item={item_id} status={status}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(run_one, item_id) for item_id in item_ids]
            for index, future in enumerate(as_completed(futures), start=1):
                item_id, status = future.result()
                counts[status] = counts.get(status, 0) + 1
                print(f"{index}/{len(item_ids)} item={item_id} status={status}", flush=True)

    approval_summaries = []
    with SessionLocal() as db:
        job_ids = args.job_ids or sorted({item.job_id for item in items})
        for job_id in job_ids:
            approval_summaries.append(approve_successful_job_items(db, job_id))

    finished = datetime.now(UTC)
    print(
        json.dumps(
            {
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "counts": counts,
                "approvals": approval_summaries,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
