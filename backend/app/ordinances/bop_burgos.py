"""Deterministic connector and coverage helpers for BOP Burgos.

Sprint 1 turns the controlled Burgos demo into a reproducible pipeline. This
module deliberately avoids generic web search: it talks to the official BOPBUR
search page, parses official announcement links, and reports local DB coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib import parse as urlparse
from urllib import request as urlrequest

from sqlalchemy.orm import Session

from app.ordinances.coverage import (
    build_province_coverage_report,
    retry_failed_province_embeddings,
)

BOP_BURGOS_BASE_URL = "http://bopbur.diputaciondeburgos.es"
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
    request = urlrequest.Request(
        url,
        headers={"User-Agent": "AsistenteAyuntamientos/0.1 bopbur-connector"},
        method="GET",
    )
    with urlrequest.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="ignore")
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

    return build_province_coverage_report(db, BOP_BURGOS_PROVINCE)


def retry_failed_burgos_embeddings(db: Session) -> dict:
    """Retry failed embeddings for Burgos legal chunks."""

    return retry_failed_province_embeddings(db, BOP_BURGOS_PROVINCE)


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
    return urlparse.urljoin(BOP_BURGOS_BASE_URL, href)


def _is_bopbur_url(url: str) -> bool:
    host = (urlparse.urlparse(url).hostname or "").lower()
    return host == BOP_BURGOS_DOMAIN
