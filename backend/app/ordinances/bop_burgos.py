"""Deterministic connector and coverage helpers for BOP Burgos.

Sprint 1 turns the controlled Burgos demo into a reproducible pipeline. This
module deliberately avoids generic web search: it talks to the official BOPBUR
search page, parses official announcement links, and reports local DB coverage.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from urllib import parse as urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.municipalities.models import Municipality
from app.ordinances.embeddings import EmbeddingsUnavailableError, embed_text
from app.ordinances.models import Ordinance, OrdinanceImportItem, OrdinanceLegalChunk

BOP_BURGOS_BASE_URL = "https://bopbur.diputaciondeburgos.es"
BOP_BURGOS_DOMAIN = "bopbur.diputaciondeburgos.es"
BOP_BURGOS_SEARCH_PATH = "/busqueda"
BOP_BURGOS_PROVINCE = "Burgos"

MAX_BOP_BURGOS_SEARCH_RESULTS = 50


@dataclass(frozen=True)
class BopBurgosAnnouncement:
    """Official BOP Burgos announcement discovered from the search page."""

    title: str
    entity: str | None
    bulletin_number: str | None
    bulletin_date: str | None
    cve: str | None
    pdf_url: str
    bulletin_url: str | None = None
    excerpt: str | None = None


def search_bop_burgos_announcements(
    query: str,
    *,
    fetch_html: Callable[[str], str],
    year: int | None = None,
    limit: int = 20,
) -> list[BopBurgosAnnouncement]:
    """Search the official BOP Burgos page and return PDF announcements.

    The endpoint is a Drupal exposed form. The stable GET parameters currently
    accepted by BOPBUR are `keys` and `field_bop_anio_numero[value][date]`.
    Network failures are intentionally surfaced to the caller so import jobs can
    mark the attempt as failed instead of silently falling back to non-official
    sources.
    """

    params = {"keys": query.strip()}
    if year is not None:
        params["field_bop_anio_numero[value][date]"] = str(year)
    url = f"{BOP_BURGOS_BASE_URL}{BOP_BURGOS_SEARCH_PATH}?{urlparse.urlencode(params)}"
    html = fetch_html(url)
    return parse_bop_burgos_search_results(html, limit=limit)


def parse_bop_burgos_search_results(
    html: str,
    *,
    limit: int = 20,
) -> list[BopBurgosAnnouncement]:
    """Parse official BOPBUR search-result HTML into announcements.

    BOPBUR nests announcements below bulletin rows and emitting bodies/entities.
    This parser intentionally depends on semantic classes present in the
    official HTML (`views-row`, `bopbur-anuncio`, `bopbur-filefield-file`) rather
    than exact visual layout.
    """

    announcements: list[BopBurgosAnnouncement] = []
    for row_html in _blocks_by_class(html, "div", "views-row"):
        bulletin_url, bulletin_number, bulletin_date = _extract_bulletin_metadata(row_html)
        entities = _split_entity_sections(row_html)
        for entity, entity_html in entities:
            for announcement_html in _blocks_by_class(entity_html, "li", "bopbur-anuncio"):
                parsed = _parse_announcement(
                    announcement_html,
                    entity=entity,
                    bulletin_url=bulletin_url,
                    bulletin_number=bulletin_number,
                    bulletin_date=bulletin_date,
                )
                if parsed is None:
                    continue
                announcements.append(parsed)
                if len(announcements) >= min(limit, MAX_BOP_BURGOS_SEARCH_RESULTS):
                    return announcements
    return announcements


def build_burgos_coverage_report(db: Session) -> dict:
    """Return deterministic local coverage metrics for Burgos municipalities."""

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
        .where(Municipality.province.ilike(BOP_BURGOS_PROVINCE))
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
    import_failures = _burgos_import_failures(db)
    return {
        "province": BOP_BURGOS_PROVINCE,
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


def retry_failed_burgos_embeddings(db: Session) -> dict:
    """Retry failed embeddings for Burgos legal chunks."""

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
                Municipality.province.ilike(BOP_BURGOS_PROVINCE),
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
        "province": BOP_BURGOS_PROVINCE,
        "retried": retried,
        "restored": restored,
        "failed": len(still_failed),
        "still_failed": still_failed,
    }


def _burgos_import_failures(db: Session) -> list[dict]:
    items = db.scalars(
        select(OrdinanceImportItem)
        .options(selectinload(OrdinanceImportItem.municipality))
        .where(OrdinanceImportItem.status == "failed")
        .order_by(OrdinanceImportItem.updated_at.desc(), OrdinanceImportItem.id.desc())
    )
    failures = []
    for item in items:
        host = (urlparse.urlparse(item.source_url).hostname or "").lower()
        is_burgos_municipality = bool(
            item.municipality
            and item.municipality.province.lower() == BOP_BURGOS_PROVINCE.lower()
        )
        if host != BOP_BURGOS_DOMAIN and not is_burgos_municipality:
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
    return any(marker in normalized for marker in ("ocr", "revisión manual", "texto suficiente"))


def _parse_announcement(
    announcement_html: str,
    *,
    entity: str | None,
    bulletin_url: str | None,
    bulletin_number: str | None,
    bulletin_date: str | None,
) -> BopBurgosAnnouncement | None:
    file_match = re.search(
        r'<p[^>]*class="[^"]*bopbur-filefield-file[^"]*"[^>]*>.*?'
        r'<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<label>.*?)</a>',
        announcement_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if file_match is None:
        return None
    pdf_url = _absolute_bopbur_url(unescape(file_match.group("href")))
    if not _is_bopbur_url(pdf_url):
        return None
    label_text = _clean_text(file_match.group("label"))
    cve = _extract_cve(label_text) or _extract_cve(_clean_text(announcement_html))
    paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", announcement_html, re.IGNORECASE | re.DOTALL)
    title = _clean_text(paragraphs[0]) if paragraphs else label_text
    excerpt = _clean_text(paragraphs[1]) if len(paragraphs) > 1 else None
    return BopBurgosAnnouncement(
        title=title,
        entity=entity,
        bulletin_number=bulletin_number,
        bulletin_date=bulletin_date,
        cve=cve,
        pdf_url=pdf_url,
        bulletin_url=bulletin_url,
        excerpt=excerpt,
    )


def _extract_bulletin_metadata(row_html: str) -> tuple[str | None, str | None, str | None]:
    number_match = re.search(
        r'class="title-number"[^>]*>\s*<a\s+href="(?P<href>[^"]+)"[^>]*>(?P<number>.*?)</a>',
        row_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    date_match = re.search(
        r'class="title-date"[^>]*>\s*<a\s+href="[^"]+"[^>]*>(?P<date>.*?)</a>',
        row_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    bulletin_url = _absolute_bopbur_url(number_match.group("href")) if number_match else None
    number = _clean_text(number_match.group("number")) if number_match else None
    date = _clean_text(date_match.group("date")) if date_match else None
    return bulletin_url, number, date


def _split_entity_sections(row_html: str) -> list[tuple[str | None, str]]:
    matches = list(
        re.finditer(r"<h3\b[^>]*>(?P<entity>.*?)</h3>", row_html, re.IGNORECASE | re.DOTALL)
    )
    if not matches:
        return [(None, row_html)]
    sections: list[tuple[str | None, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(row_html)
        sections.append((_clean_text(match.group("entity")), row_html[start:end]))
    return sections


def _blocks_by_class(html: str, tag: str, class_name: str) -> list[str]:
    pattern = re.compile(
        rf"<{tag}\b(?=[^>]*class=\"[^\"]*{re.escape(class_name)}[^\"]*\")[^>]*>",
        re.IGNORECASE,
    )
    blocks: list[str] = []
    search_from = 0
    while True:
        match = pattern.search(html, search_from)
        if match is None:
            break
        end = _find_matching_end(html, tag, match.end())
        if end == -1:
            break
        blocks.append(html[match.start() : end])
        search_from = end
    return blocks


def _find_matching_end(html: str, tag: str, content_start: int) -> int:
    token_re = re.compile(rf"</?{tag}\b[^>]*>", re.IGNORECASE)
    depth = 1
    for token in token_re.finditer(html, content_start):
        if token.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                return token.end()
        else:
            depth += 1
    return -1


def _clean_text(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(value).split())


def _extract_cve(text: str) -> str | None:
    match = re.search(r"BOPBUR-\d{4}-\d{5}", text, re.IGNORECASE)
    return match.group(0).upper() if match else None


def _absolute_bopbur_url(href: str) -> str:
    absolute_url = urlparse.urljoin(BOP_BURGOS_BASE_URL, href)
    parsed = urlparse.urlparse(absolute_url)
    if parsed.hostname == BOP_BURGOS_DOMAIN and parsed.scheme == "http":
        return parsed._replace(scheme="https").geturl()
    return absolute_url


def _is_bopbur_url(url: str) -> bool:
    host = (urlparse.urlparse(url).hostname or "").lower()
    return host == BOP_BURGOS_DOMAIN
