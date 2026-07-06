"""Soria ordinance coverage utilities.

Operator workflow for the Castilla y León province-by-province ordinance corpus.
It seeds official Soria municipalities from INE and discovers official BOP Soria
ordinance PDF announcements for import through the existing ordinance pipeline.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
import ssl
import time
import unicodedata
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from urllib import parse as urlparse
from urllib import request as urlrequest
import xml.etree.ElementTree as ET

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.assistant import models as _assistant_models  # noqa: F401 - register mappers
from app.db.session import SessionLocal
from app.documents import models as _document_models  # noqa: F401 - register mappers
from app.municipalities.models import Municipality
from app.organizations import models as _organization_models  # noqa: F401 - register mappers
from app.ordinances.burgos_full_coverage import approve_successful_job_items
from app.ordinances.import_service import run_import_job
from app.ordinances.models import OfficialLegalSource, OrdinanceImportJob
from app.projects import models as _project_models  # noqa: F401 - register mappers
from app.rbac import models as _rbac_models  # noqa: F401 - register mappers
from app.requirements import models as _requirement_models  # noqa: F401 - register mappers
from app.telegram import models as _telegram_models  # noqa: F401 - register mappers
from app.users import models as _user_models  # noqa: F401 - register mappers

SORIA_PROVINCE = "Soria"
SORIA_PROVINCE_CODE = "42"
CASTILLA_Y_LEON = "Castilla y León"
INE_MUNICIPALITY_DICTIONARY_2025_URL = "https://www.ine.es/daco/daco42/codmun/diccionario25.xlsx"
BOP_SORIA_DOMAIN = "bop.dipsoria.es"
BOP_SORIA_BASE_URL = "https://bop.dipsoria.es/"
BOP_SORIA_SEARCH_URL = "https://bop.dipsoria.es/index.php/mod.boloficial/mem.buscadorbop/relmenu.149"
BOP_SORIA_REVIEW_NOTE = (
    "Aprobada técnicamente para recuperación en el MVP Ordenanzas Soria desde "
    "fuente oficial. Requiere validación jurídica humana antes de uso oficial."
)
DEFAULT_QUERIES = (
    "ordenanza",
    "ordenanza fiscal",
    "reglamento",
)
EXCLUDED_ENTITIES = (
    "DIPUTACIÓN",
    "MANCOMUNIDAD",
    "CONSORCIO",
    "COMUNIDAD DE",
    "JUNTA VECINAL",
    "ENTIDAD LOCAL MENOR",
)
NORMATIVE_RE = re.compile(
    r"\b(ordenanza|reglamento|norma urban|modificaci[oó]n ordenanza)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IneMunicipality:
    name: str
    ine_code: str
    control_digit: str


@dataclass(frozen=True)
class BopSoriaCandidate:
    municipality_id: int
    municipality_name: str
    title: str
    source_url: str
    detail_url: str
    query: str


class LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str, str]] = []
        self._href: str | None = None
        self._title: str = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attr_map = dict(attrs)
            self._href = attr_map.get("href")
            self._title = attr_map.get("title") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text_value = " ".join(" ".join(self._text).split())
            self.links.append((self._href, text_value, self._title))
            self._href = None
            self._title = ""
            self._text = []


def _normalize_key(text_value: str) -> str:
    value = unicodedata.normalize("NFKD", text_value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"\b(ayuntamiento|de|del|la|el|los|las)\b", " ", value.lower())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _ssl_context() -> ssl.SSLContext:
    # BOP Soria currently serves a chain that fails Python's default verifier in
    # this container. We still keep source provenance on the official domain.
    return ssl._create_unverified_context()


def _fetch_text(url: str, *, data: bytes | None = None, timeout: int = 60) -> str:
    request = urlrequest.Request(
        url,
        data=data,
        headers={
            "User-Agent": "AsistenteAyuntamientos/0.1 soria-coverage",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlrequest.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        return response.read().decode("utf-8", errors="ignore")


def load_ine_municipalities(province_code: str = SORIA_PROVINCE_CODE) -> list[IneMunicipality]:
    request = urlrequest.Request(
        INE_MUNICIPALITY_DICTIONARY_2025_URL,
        headers={"User-Agent": "AsistenteAyuntamientos/0.1 ine-catalog"},
    )
    with urlrequest.urlopen(request, timeout=30) as response:
        content = response.read()

    with zipfile.ZipFile(BytesIO(content)) as archive:
        namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        shared_strings: list[str] = []
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in shared_root.findall("a:si", namespace):
            shared_strings.append("".join(text.text or "" for text in item.findall(".//a:t", namespace)))
        sheet_root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows: list[list[str]] = []
        for row in sheet_root.findall(".//a:row", namespace):
            values: list[str] = []
            for cell in row.findall("a:c", namespace):
                raw_value = cell.find("a:v", namespace)
                value = "" if raw_value is None else raw_value.text or ""
                if cell.get("t") == "s" and value:
                    value = shared_strings[int(value)]
                values.append(value)
            rows.append(values)

    municipalities: list[IneMunicipality] = []
    for values in rows[2:]:
        if len(values) < 5:
            continue
        _autonomous_code, row_province_code, municipality_code, control_digit, name = values[:5]
        if row_province_code != province_code:
            continue
        municipalities.append(
            IneMunicipality(
                name=name.strip(),
                ine_code=f"{row_province_code}{municipality_code}",
                control_digit=control_digit.strip(),
            )
        )
    return municipalities


def seed_soria_municipalities(db: Session) -> dict:
    official_municipalities = load_ine_municipalities()
    created = 0
    updated = 0
    existing_by_code = {
        municipality.ine_code: municipality
        for municipality in db.scalars(select(Municipality).where(Municipality.ine_code.is_not(None)))
    }
    existing_soria = list(
        db.scalars(select(Municipality).where(Municipality.province.ilike(SORIA_PROVINCE)))
    )
    for official in official_municipalities:
        municipality = existing_by_code.get(official.ine_code)
        if municipality is None:
            municipality = next(
                (
                    candidate
                    for candidate in existing_soria
                    if candidate.name == official.name and not candidate.ine_code
                ),
                None,
            )
        if municipality is None:
            municipality = Municipality(
                name=official.name,
                province=SORIA_PROVINCE,
                autonomous_community=CASTILLA_Y_LEON,
                country="España",
                ine_code=official.ine_code,
                municipality_type="municipality",
                rural_urban_profile="unknown",
                administrative_notes=(
                    "Alta automática desde diccionario oficial INE 2025 para cobertura "
                    "MVP Ordenanzas Soria."
                ),
            )
            db.add(municipality)
            db.flush()
            created += 1
            existing_soria.append(municipality)
            existing_by_code[official.ine_code] = municipality
            continue
        changed = False
        for field, value in (
            ("name", official.name),
            ("province", SORIA_PROVINCE),
            ("autonomous_community", CASTILLA_Y_LEON),
            ("country", "España"),
            ("ine_code", official.ine_code),
            ("status", "active"),
        ):
            if getattr(municipality, field) != value:
                setattr(municipality, field, value)
                changed = True
        if changed:
            updated += 1
    db.commit()
    return {"province": SORIA_PROVINCE, "official": len(official_municipalities), "created": created, "updated": updated}


def _get_bop_soria_source(db: Session) -> OfficialLegalSource:
    source = db.scalar(select(OfficialLegalSource).where(OfficialLegalSource.domain == BOP_SORIA_DOMAIN))
    if source is None:
        source = OfficialLegalSource(
            name="Boletín Oficial de la Provincia de Soria",
            base_url=BOP_SORIA_BASE_URL,
            domain=BOP_SORIA_DOMAIN,
            source_type="bop",
            status="active",
            notes="Fuente oficial BOP Soria para ordenanzas municipales.",
        )
        db.add(source)
        db.commit()
        db.refresh(source)
    return source


def _search_result_detail_urls(query: str, *, max_results: int) -> list[str]:
    data = urlparse.urlencode({"palabra": query, "fecha_desde": "", "fecha_hasta": ""}).encode()
    html = _fetch_text(BOP_SORIA_SEARCH_URL, data=data, timeout=120)
    parser = LinkTextParser()
    parser.feed(html)
    urls: list[str] = []
    seen: set[str] = set()
    for href, _text, title in parser.links:
        if "mod.boloficial/mem.detalle/id." not in href:
            continue
        if "Más información" not in title:
            continue
        full_url = urlparse.urljoin(BOP_SORIA_BASE_URL, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        urls.append(full_url)
        if len(urls) >= max_results:
            break
    return urls


def _extract_candidate_sections(html: str) -> list[tuple[str, str, str]]:
    # BOP detail pages list hierarchy as p.fec-f1 nodes and then one or more
    # descargar links. Keep a small state machine over their textual order.
    tokens: list[tuple[str, str, str]] = []
    for match in re.finditer(
        r'(<p class="fec-f1">(?P<p>.*?)</p>)|(<a href="(?P<href>[^"]+)"[^>]*>(?P<a>.*?)</a>)',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        if match.group("p") is not None:
            text_value = re.sub(r"<[^>]+>", " ", match.group("p"))
            text_value = " ".join(text_value.replace("&nbsp;", " ").split())
            tokens.append(("text", text_value, ""))
        elif match.group("href") is not None:
            link_text = re.sub(r"<[^>]+>", " ", match.group("a") or "")
            link_text = " ".join(link_text.split())
            href = urlparse.urljoin(BOP_SORIA_BASE_URL, match.group("href"))
            tokens.append(("link", link_text, href))

    sections: list[tuple[str, str, str]] = []
    in_municipal_area = False
    current_municipality = ""
    current_title = ""
    for kind, value, href in tokens:
        if kind == "text":
            clean = value.strip(" -")
            upper = clean.upper()
            if upper.startswith("AYUNTAMIENTOS") or upper == "III. ADMINISTRACIÓN LOCAL":
                in_municipal_area = True
                current_municipality = ""
                current_title = ""
                continue
            if any(marker in upper for marker in EXCLUDED_ENTITIES):
                current_municipality = ""
                current_title = ""
                continue
            if in_municipal_area and clean:
                if clean.upper() == clean.upper() and len(clean.split()) <= 8 and not NORMATIVE_RE.search(clean):
                    current_municipality = clean
                    current_title = ""
                elif NORMATIVE_RE.search(clean):
                    current_title = clean
        elif kind == "link" and current_municipality and current_title:
            if "mod.documentos/mem.descargar" not in href:
                continue
            sections.append((current_municipality, current_title, href.replace("https://", "http://")))
    return sections


def _candidate_from_detail(
    detail_url: str,
    *,
    municipality_by_key: dict[str, Municipality],
    query: str,
) -> list[BopSoriaCandidate]:
    try:
        html = _fetch_text(detail_url, timeout=60)
    except Exception:
        return []
    candidates: list[BopSoriaCandidate] = []
    for municipality_text, title, pdf_url in _extract_candidate_sections(html):
        municipality = municipality_by_key.get(_normalize_key(municipality_text))
        if municipality is None:
            continue
        if not NORMATIVE_RE.search(title):
            continue
        candidates.append(
            BopSoriaCandidate(
                municipality_id=municipality.id,
                municipality_name=municipality.name,
                title=title[:500],
                source_url=pdf_url,
                detail_url=detail_url,
                query=query,
            )
        )
    return candidates


def discover_bop_soria_candidates(
    db: Session,
    *,
    max_results_per_query: int,
    workers: int,
) -> list[BopSoriaCandidate]:
    municipalities = list(
        db.scalars(
            select(Municipality)
            .where(Municipality.province.ilike(SORIA_PROVINCE), Municipality.status == "active")
            .order_by(Municipality.name)
        )
    )
    municipality_by_key = {_normalize_key(municipality.name): municipality for municipality in municipalities}
    seen_pdf: set[str] = set()
    all_candidates: list[BopSoriaCandidate] = []
    for query in DEFAULT_QUERIES:
        detail_urls = _search_result_detail_urls(query, max_results=max_results_per_query)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [
                executor.submit(
                    _candidate_from_detail,
                    url,
                    municipality_by_key=municipality_by_key,
                    query=query,
                )
                for url in detail_urls
            ]
            for future in as_completed(futures):
                for candidate in future.result():
                    if candidate.source_url in seen_pdf:
                        continue
                    seen_pdf.add(candidate.source_url)
                    all_candidates.append(candidate)
        time.sleep(1)
    return sorted(all_candidates, key=lambda item: (item.municipality_name, item.title, item.source_url))


def create_source_job(db: Session, candidates: list[BopSoriaCandidate]) -> OrdinanceImportJob | None:
    if not candidates:
        return None
    source = _get_bop_soria_source(db)
    source_urls = [
        {
            "url": candidate.source_url,
            "municipality_id": candidate.municipality_id,
            "official_source_id": source.id,
            "title": f"{candidate.title} ({candidate.municipality_name})",
        }
        for candidate in candidates
    ]
    job = OrdinanceImportJob(
        title="MVP Ordenanzas Soria - BOP Soria ordenanzas municipales",
        description=(
            "Importación automática de anuncios/PDFs oficiales del Boletín Oficial "
            "de la Provincia de Soria filtrados por entidad municipal y título normativo."
        ),
        topic="ordenanzas municipales",
        subtopic="BOP Soria",
        municipality_ids_json="[]",
        official_source_ids_json=json.dumps([source.id]),
        source_urls_json=json.dumps(source_urls, ensure_ascii=False),
        review_criteria=(
            "Anuncio/PDF oficial del BOP Soria con entidad municipal exacta y título de ordenanza, "
            "reglamento o modificación normativa."
        ),
        status="draft",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def coverage_report(db: Session) -> dict:
    values: dict[str, int] = {}
    queries = {
        "municipios_catalogo": "select count(distinct name) from municipalities where province ilike 'Soria' and status='active'",
        "municipios_con_ordenanzas": "select count(distinct m.name) from ordinances o join municipalities m on m.id=o.municipality_id where m.province ilike 'Soria' and o.curation_status='approved'",
        "municipios_sin_cobertura": "with covered as (select distinct m.name from ordinances o join municipalities m on m.id=o.municipality_id where m.province ilike 'Soria' and o.curation_status='approved') select count(*) from municipalities m left join covered c on c.name=m.name where m.province ilike 'Soria' and m.status='active' and c.name is null",
        "ordenanzas_aprobadas": "select count(*) from ordinances o join municipalities m on m.id=o.municipality_id where m.province ilike 'Soria' and o.curation_status='approved'",
        "chunks_ready_approved": "select count(*) from ordinance_legal_chunks c join ordinances o on o.id=c.ordinance_id join municipalities m on m.id=o.municipality_id where m.province ilike 'Soria' and c.embedding_status='ready' and c.review_status='approved'",
        "chunks_failed": "select count(*) from ordinance_legal_chunks c join ordinances o on o.id=c.ordinance_id join municipalities m on m.id=o.municipality_id where m.province ilike 'Soria' and c.embedding_status='failed'",
    }
    for key, sql in queries.items():
        values[key] = int(db.execute(text(sql)).scalar() or 0)
    return values


def run_soria_import(*, max_results_per_query: int, workers: int, dry_run: bool) -> dict:
    with SessionLocal() as db:
        started = datetime.now(UTC).isoformat()
        seed = seed_soria_municipalities(db)
        before = coverage_report(db)
        candidates = discover_bop_soria_candidates(db, max_results_per_query=max_results_per_query, workers=workers)
        discovery = {
            "candidates": len(candidates),
            "municipalities": len({candidate.municipality_name for candidate in candidates}),
            "sample": [candidate.__dict__ for candidate in candidates[:20]],
        }
        result: dict = {
            "started_at": started,
            "seed": seed,
            "coverage_before": before,
            "discovery": discovery,
            "dry_run": dry_run,
        }
        if dry_run:
            return result
        job = create_source_job(db, candidates)
        if job is None:
            result["job_id"] = None
            result["coverage_after"] = coverage_report(db)
            return result
        run_import_job(job.id, db=db)
        approvals = approve_successful_job_items(db, job.id)
        result["job_id"] = job.id
        result["approvals"] = approvals
        result["coverage_after"] = coverage_report(db)
        result["finished_at"] = datetime.now(UTC).isoformat()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "discover", "import", "report"))
    parser.add_argument("--max-results-per-query", type=int, default=800)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.action == "report":
        with SessionLocal() as db:
            print(json.dumps(coverage_report(db), ensure_ascii=False, indent=2))
        return
    if args.action == "seed":
        with SessionLocal() as db:
            print(json.dumps(seed_soria_municipalities(db), ensure_ascii=False, indent=2))
        return
    result = run_soria_import(
        max_results_per_query=args.max_results_per_query,
        workers=args.workers,
        dry_run=args.action == "discover",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
