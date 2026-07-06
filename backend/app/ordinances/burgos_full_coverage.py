"""Burgos full-coverage utilities for the ordinance MVP.

This module upgrades the initial demo bootstrap into an operator workflow:

1. Seed the full Burgos municipality catalogue from the official INE 2025 code
   dictionary.
2. Discover BOPBUR ordinance/regulation PDF announcements per municipality.
3. Import discovered official PDFs through the normal ordinance import pipeline.
4. Technically approve imported chunks for demo retrieval with explicit legal
   review caveats.
5. Print a measurable coverage report.

It is intentionally CLI-oriented so the first full Burgos run can be audited
without hiding long-running import effects behind a request timeout.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
import json
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from urllib import parse as urlparse
from urllib import request as urlrequest
import xml.etree.ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assistant import models as _assistant_models  # noqa: F401 - register mappers
from app.db.session import SessionLocal
from app.documents import models as _document_models  # noqa: F401 - register mappers
from app.municipalities.models import Municipality
from app.organizations import models as _organization_models  # noqa: F401 - register mappers
from app.ordinances.bop_burgos import (
    BOP_BURGOS_DOMAIN,
    build_burgos_coverage_report,
    is_municipal_bopbur_announcement,
    is_normative_bopbur_announcement,
    natural_municipality_name,
    normalize_bopbur_text,
    search_bop_burgos_announcements,
)
from app.ordinances.demo_bootstrap import _ensure_text_embedding_column
from app.ordinances.embeddings import embed_text
from app.ordinances.import_service import run_import_job
from app.ordinances.models import (
    OfficialLegalSource,
    Ordinance,
    OrdinanceImportItem,
    OrdinanceImportJob,
)
from app.ordinances.seed import ensure_initial_official_legal_sources
from app.projects import models as _project_models  # noqa: F401 - register mappers
from app.rbac import models as _rbac_models  # noqa: F401 - register mappers
from app.requirements import models as _requirement_models  # noqa: F401 - register mappers
from app.telegram import models as _telegram_models  # noqa: F401 - register mappers
from app.users import models as _user_models  # noqa: F401 - register mappers

BURGOS_PROVINCE = "Burgos"
BURGOS_AUTONOMOUS_COMMUNITY = "Castilla y León"
INE_MUNICIPALITY_DICTIONARY_2025_URL = (
    "https://www.ine.es/daco/daco42/codmun/diccionario25.xlsx"
)
DIPUTACION_BURGOS_DOMAIN = "burgos.es"
DIPUTACION_FISCAL_ORDINANCES_URL = (
    "https://www.burgos.es/ayuntamientos/servicios/recaudacion/ciudadano/"
    "ordenanzas-fiscales-de-los-ayuntamientos"
)
FULL_COVERAGE_REVIEW_NOTE = (
    "Aprobada técnicamente para recuperación en el MVP Ordenanzas Burgos desde "
    "fuente oficial. Requiere validación jurídica humana antes de uso oficial."
)
DEFAULT_DISCOVERY_QUERIES = (
    "ordenanza Ayuntamiento",
    "reglamento Ayuntamiento",
    "ordenanza fiscal Ayuntamiento",
)


@dataclass(frozen=True)
class IneMunicipality:
    name: str
    ine_code: str
    control_digit: str


@dataclass(frozen=True)
class DiscoveryCandidate:
    municipality_id: int
    municipality_name: str
    title: str
    source_url: str
    cve: str | None
    bulletin_number: str | None
    bulletin_date: str | None
    query: str


@dataclass(frozen=True)
class FiscalOrdinanceCandidate:
    municipality_id: int
    municipality_name: str
    title: str
    source_url: str
    tax_type: str
    publication_hint: str | None


def load_ine_burgos_municipalities() -> list[IneMunicipality]:
    """Download and parse the official INE 2025 municipality code dictionary."""

    request = urlrequest.Request(
        INE_MUNICIPALITY_DICTIONARY_2025_URL,
        headers={"User-Agent": "AsistenteAyuntamientos/0.1 burgos-catalog"},
        method="GET",
    )
    with urlrequest.urlopen(request, timeout=30) as response:
        content = response.read()

    with zipfile.ZipFile(BytesIO(content)) as archive:
        namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        shared_strings = []
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in shared_root.findall("a:si", namespace):
            shared_strings.append(
                "".join(text.text or "" for text in item.findall(".//a:t", namespace))
            )
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
        _autonomous_code, province_code, municipality_code, control_digit, name = values[:5]
        if province_code != "09":
            continue
        municipalities.append(
            IneMunicipality(
                name=name.strip(),
                ine_code=f"{province_code}{municipality_code}",
                control_digit=control_digit.strip(),
            )
        )
    return municipalities


def seed_burgos_municipalities(db: Session) -> dict:
    """Create/update the 371 official Burgos municipalities in local DB."""

    official_municipalities = load_ine_burgos_municipalities()
    created = 0
    updated = 0
    existing_by_code = {
        municipality.ine_code: municipality
        for municipality in db.scalars(
            select(Municipality).where(Municipality.ine_code.is_not(None))
        )
    }
    existing_burgos = list(
        db.scalars(
            select(Municipality)
            .where(Municipality.province.ilike(BURGOS_PROVINCE))
            .order_by(Municipality.id)
        )
    )

    for official in official_municipalities:
        municipality = existing_by_code.get(official.ine_code)
        if municipality is None:
            municipality = next(
                (
                    candidate
                    for candidate in existing_burgos
                    if candidate.name == official.name and not candidate.ine_code
                ),
                None,
            )
        if municipality is None:
            municipality = Municipality(
                name=official.name,
                province=BURGOS_PROVINCE,
                autonomous_community=BURGOS_AUTONOMOUS_COMMUNITY,
                country="España",
                ine_code=official.ine_code,
                municipality_type="municipality",
                rural_urban_profile="unknown",
                administrative_notes=(
                    "Alta automática desde diccionario oficial INE 2025 para "
                    "cobertura MVP Ordenanzas Burgos."
                ),
            )
            db.add(municipality)
            db.flush()
            created += 1
            existing_burgos.append(municipality)
            existing_by_code[official.ine_code] = municipality
            continue

        changed = False
        for field, value in (
            ("name", official.name),
            ("province", BURGOS_PROVINCE),
            ("autonomous_community", BURGOS_AUTONOMOUS_COMMUNITY),
            ("country", "España"),
            ("ine_code", official.ine_code),
            ("municipality_type", "municipality"),
            ("status", "active"),
        ):
            if getattr(municipality, field) != value:
                setattr(municipality, field, value)
                changed = True
        note = municipality.administrative_notes or ""
        if "diccionario oficial INE 2025" not in note:
            municipality.administrative_notes = (
                f"{note}\n" if note else ""
            ) + "Actualizado desde diccionario oficial INE 2025 para cobertura Burgos."
            changed = True
        if changed:
            updated += 1

    db.commit()
    return {
        "source": INE_MUNICIPALITY_DICTIONARY_2025_URL,
        "official_municipalities": len(official_municipalities),
        "created": created,
        "updated": updated,
    }


def _ensure_diputacion_fiscal_source(db: Session) -> OfficialLegalSource:
    """Create/get Diputación's official fiscal ordinance repository source."""

    source = db.scalar(
        select(OfficialLegalSource).where(
            OfficialLegalSource.domain == DIPUTACION_BURGOS_DOMAIN,
        )
    )
    if source is None:
        source = OfficialLegalSource(
            name="Diputación de Burgos - Ordenanzas fiscales municipales",
            base_url="https://www.burgos.es/",
            domain=DIPUTACION_BURGOS_DOMAIN,
            source_type="other",
            status="active",
            notes=(
                "Repositorio oficial de la Diputación Provincial de Burgos con "
                "ordenanzas fiscales de ayuntamientos. Fuente auxiliar para "
                "cobertura masiva del MVP Ordenanzas Burgos."
            ),
        )
        db.add(source)
        db.commit()
    elif source.status != "active":
        source.status = "active"
        db.commit()
    return source


def _get_bopbur_source(db: Session) -> OfficialLegalSource:
    ensure_initial_official_legal_sources(db)
    source = db.scalar(
        select(OfficialLegalSource).where(
            OfficialLegalSource.domain == BOP_BURGOS_DOMAIN,
            OfficialLegalSource.status == "active",
        )
    )
    if source is None:
        raise RuntimeError("BOP Burgos official source is not active")
    return source


def _burgos_municipalities(db: Session, *, limit: int | None = None) -> list[Municipality]:
    query = (
        select(Municipality)
        .where(
            Municipality.province.ilike(BURGOS_PROVINCE),
            Municipality.status == "active",
        )
        .order_by(Municipality.name, Municipality.id)
    )
    municipalities: list[Municipality] = []
    seen_names: set[str] = set()
    for municipality in db.scalars(query):
        if municipality.name in seen_names:
            continue
        seen_names.add(municipality.name)
        municipalities.append(municipality)
        if limit is not None and len(municipalities) >= limit:
            break
    return municipalities


class _FiscalOrdinanceTableParser(HTMLParser):
    TAX_COLUMNS = {
        1: "IBI",
        2: "IAE",
        3: "IVTM",
        4: "Agua",
        5: "Basuras/Varios",
        6: "IIVT",
    }

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str, str | None, str]] = []
        self._in_row = False
        self._in_cell = False
        self._cell_index = -1
        self._cell_text: list[str] = []
        self._row_municipality = ""
        self._current_href: str | None = None
        self._current_title: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._in_row = True
            self._cell_index = -1
            self._row_municipality = ""
        elif tag == "td" and self._in_row:
            self._in_cell = True
            self._cell_index += 1
            self._cell_text = []
        elif tag == "a" and self._in_cell:
            self._current_href = attrs_dict.get("href")
            self._current_title = attrs_dict.get("title")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_cell and self._current_href:
            if self._current_href.lower().split("?", 1)[0].endswith(".pdf"):
                tax_type = self.TAX_COLUMNS.get(self._cell_index, "Ordenanza fiscal")
                self.rows.append((self._row_municipality, tax_type, self._current_title, self._current_href))
            self._current_href = None
            self._current_title = None
        elif tag == "td" and self._in_cell:
            text = " ".join(" ".join(self._cell_text).split())
            if self._cell_index == 0:
                self._row_municipality = text
            self._in_cell = False
            self._cell_text = []
        elif tag == "tr":
            self._in_row = False
            self._cell_index = -1
            self._row_municipality = ""


def discover_diputacion_fiscal_candidates(db: Session) -> dict:
    """Parse Diputación's fiscal ordinance table and map PDFs to municipalities."""

    request = urlrequest.Request(
        DIPUTACION_FISCAL_ORDINANCES_URL,
        headers={"User-Agent": "AsistenteAyuntamientos/0.1 fiscal-ordinances"},
        method="GET",
    )
    with urlrequest.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="ignore")
    parser = _FiscalOrdinanceTableParser()
    parser.feed(html)

    def key_variants(value: str) -> set[str]:
        normalized = normalize_bopbur_text(value)
        return {normalized, normalized.replace(" ", "")}

    by_name: dict[str, Municipality] = {}
    for municipality in _burgos_municipalities(db):
        for key in key_variants(municipality.name) | key_variants(
            natural_municipality_name(municipality.name)
        ):
            by_name[key] = municipality

    candidates: list[FiscalOrdinanceCandidate] = []
    unmatched: set[str] = set()
    seen: set[tuple[int, str]] = set()
    for raw_municipality, tax_type, publication_hint, href in parser.rows:
        municipality = next(
            (by_name[key] for key in key_variants(raw_municipality) if key in by_name),
            None,
        )
        if municipality is None:
            unmatched.add(raw_municipality)
            continue
        source_url = urlparse.urljoin(DIPUTACION_FISCAL_ORDINANCES_URL, href)
        key = (municipality.id, source_url)
        if key in seen:
            continue
        seen.add(key)
        hint = f" ({publication_hint})" if publication_hint else ""
        candidates.append(
            FiscalOrdinanceCandidate(
                municipality_id=municipality.id,
                municipality_name=municipality.name,
                title=f"Ordenanza fiscal {tax_type} - {municipality.name}{hint}",
                source_url=source_url,
                tax_type=tax_type,
                publication_hint=publication_hint,
            )
        )
    return {
        "source": DIPUTACION_FISCAL_ORDINANCES_URL,
        "pdf_links_total": len(parser.rows),
        "candidates_total": len(candidates),
        "municipalities_with_candidates": len({candidate.municipality_name for candidate in candidates}),
        "unmatched_municipalities_total": len(unmatched),
        "unmatched_municipalities": sorted(unmatched),
        "candidates": [candidate.__dict__ for candidate in candidates],
    }


def discover_bopbur_candidates(
    db: Session,
    *,
    limit_municipalities: int | None = None,
    result_limit: int = 50,
    sleep_seconds: float = 0.15,
    max_workers: int = 1,
    queries: tuple[str, ...] = DEFAULT_DISCOVERY_QUERIES,
) -> dict:
    """Search official BOPBUR for ordinance PDFs for each Burgos municipality."""

    municipalities = _burgos_municipalities(db, limit=limit_municipalities)
    seen_urls: set[tuple[int, str]] = set()
    candidates: list[DiscoveryCandidate] = []
    errors: list[dict] = []

    def search_one(municipality: Municipality, query_base: str) -> tuple[list[dict], dict | None]:
        natural_name = natural_municipality_name(municipality.name)
        query = f"{query_base} {natural_name}".strip()
        try:
            announcements = search_bop_burgos_announcements(query, limit=result_limit)
        except Exception as error:  # noqa: BLE001 - report and continue batch
            return [], {
                "municipality": municipality.name,
                "query": query,
                "error": str(error)[:500],
            }
        rows: list[dict] = []
        for announcement in announcements:
            if not is_municipal_bopbur_announcement(announcement, municipality.name):
                continue
            if not is_normative_bopbur_announcement(announcement):
                continue
            rows.append(
                DiscoveryCandidate(
                    municipality_id=municipality.id,
                    municipality_name=municipality.name,
                    title=announcement.title,
                    source_url=announcement.pdf_url,
                    cve=announcement.cve,
                    bulletin_number=announcement.bulletin_number,
                    bulletin_date=announcement.bulletin_date,
                    query=query,
                ).__dict__
            )
        return rows, None

    work = [(municipality, query_base) for municipality in municipalities for query_base in queries]
    if max_workers <= 1:
        for municipality, query_base in work:
            rows, error = search_one(municipality, query_base)
            if error is not None:
                errors.append(error)
            for row in rows:
                key = (row["municipality_id"], row["source_url"])
                if key not in seen_urls:
                    seen_urls.add(key)
                    candidates.append(DiscoveryCandidate(**row))
            if sleep_seconds:
                time.sleep(sleep_seconds)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(search_one, municipality, query_base) for municipality, query_base in work]
            for future in as_completed(futures):
                rows, error = future.result()
                if error is not None:
                    errors.append(error)
                for row in rows:
                    key = (row["municipality_id"], row["source_url"])
                    if key not in seen_urls:
                        seen_urls.add(key)
                        candidates.append(DiscoveryCandidate(**row))

    return {
        "municipalities_scanned": len(municipalities),
        "queries_per_municipality": len(queries),
        "candidates_total": len(candidates),
        "errors_total": len(errors),
        "errors": errors,
        "candidates": [candidate.__dict__ for candidate in candidates],
    }


def create_source_job(
    db: Session,
    candidates: list[dict],
    *,
    title: str = "MVP Ordenanzas Burgos - cobertura BOPBUR completa",
    source: OfficialLegalSource | None = None,
) -> OrdinanceImportJob:
    source = source or _get_bopbur_source(db)
    source_urls = [
        {
            "url": candidate["source_url"],
            "municipality_id": int(candidate["municipality_id"]),
            "official_source_id": source.id,
            "title": candidate.get("title") or "Ordenanza BOP Burgos",
        }
        for candidate in candidates
    ]
    job = OrdinanceImportJob(
        title=title,
        description=(
            "Importación masiva oficial BOP Burgos para ampliar cobertura del MVP "
            "a todos los municipios de la provincia."
        ),
        topic="ordenanzas municipales",
        subtopic="cobertura provincia de Burgos",
        search_query=None,
        # Municipality ids live on each explicit source URL. Keep the job-level
        # discovery list empty so run_import_job does not search BOPBUR again
        # when this job is meant to process already-vetted candidates.
        municipality_ids_json="[]",
        official_source_ids_json=json.dumps([source.id]),
        source_urls_json=json.dumps(source_urls, ensure_ascii=False),
        review_criteria=(
            "Fuente BOP Burgos oficial, entidad Ayuntamiento del municipio esperado, "
            "anuncio normativo de ordenanza/reglamento y texto extraíble."
        ),
        status="draft",
    )
    db.add(job)
    db.commit()
    return job


def create_diputacion_fiscal_job(
    db: Session,
    candidates: list[dict],
    *,
    title: str = "MVP Ordenanzas Burgos - fallback Diputación ordenanzas fiscales",
) -> OrdinanceImportJob:
    return create_source_job(
        db,
        candidates,
        title=title,
        source=_ensure_diputacion_fiscal_source(db),
    )


def approve_successful_job_items(db: Session, job_id: int) -> dict:
    """Technically approve successful imported ordinances for demo retrieval."""

    items = list(
        db.scalars(
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
    )
    approved = 0
    duplicates = 0
    failed = 0
    ready_chunks = 0
    now = datetime.now(UTC)
    for item in items:
        ordinance = item.ordinance
        if item.status == "duplicate":
            duplicates += 1
            continue
        if ordinance is None:
            if item.status == "failed":
                failed += 1
            continue
        if item.status == "failed":
            failed += 1
            continue
        ordinance.status = "active"
        ordinance.curation_status = "approved"
        ordinance.official_bulletin = ordinance.official_bulletin or "Boletín Oficial de la Provincia de Burgos"
        ordinance.legal_review_notes = FULL_COVERAGE_REVIEW_NOTE
        item.status = "approved"
        item.error_message = None
        for chunk in ordinance.legal_chunks:
            chunk.review_status = "approved"
            if chunk.embedding_status != "ready" or not chunk.embedding:
                embedding, model, status = embed_text(chunk.text)
                chunk.embedding = embedding
                chunk.embedding_model = model
                chunk.embedding_status = status
                chunk.embedded_at = now if status == "ready" else None
            if chunk.embedding_status == "ready":
                ready_chunks += 1
        approved += 1
    db.commit()
    return {
        "job_id": job_id,
        "items_total": len(items),
        "approved": approved,
        "duplicates": duplicates,
        "failed": failed,
        "ready_chunks": ready_chunks,
    }


def run_discover_import(
    db: Session,
    *,
    limit_municipalities: int | None,
    max_workers: int,
    dry_run: bool,
) -> dict:
    _ensure_text_embedding_column(db)
    seed_summary = seed_burgos_municipalities(db)
    discovery = discover_bopbur_candidates(
        db,
        limit_municipalities=limit_municipalities,
        sleep_seconds=0 if max_workers > 1 else 0.15,
        max_workers=max_workers,
    )
    if dry_run or not discovery["candidates"]:
        return {
            "seed": seed_summary,
            "discovery": discovery,
            "import": None,
            "coverage": build_burgos_coverage_report(db),
        }
    job = create_source_job(db, discovery["candidates"])
    run_import_job(job.id, db=db)
    approval = approve_successful_job_items(db, job.id)
    return {
        "seed": seed_summary,
        "discovery": {
            key: value for key, value in discovery.items() if key != "candidates"
        },
        "job_id": job.id,
        "import": approval,
        "coverage": build_burgos_coverage_report(db),
    }


def run_diputacion_fiscal_import(db: Session, *, dry_run: bool) -> dict:
    _ensure_text_embedding_column(db)
    seed_summary = seed_burgos_municipalities(db)
    _ensure_diputacion_fiscal_source(db)
    discovery = discover_diputacion_fiscal_candidates(db)
    discovery_summary = {key: value for key, value in discovery.items() if key != "candidates"}
    if dry_run or not discovery["candidates"]:
        return {
            "seed": seed_summary,
            "discovery": discovery_summary,
            "import": None,
            "coverage": build_burgos_coverage_report(db),
        }
    job = create_diputacion_fiscal_job(db, discovery["candidates"])
    run_import_job(job.id, db=db)
    approval = approve_successful_job_items(db, job.id)
    return {
        "seed": seed_summary,
        "discovery": discovery_summary,
        "job_id": job.id,
        "import": approval,
        "coverage": build_burgos_coverage_report(db),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Burgos ordinance full-coverage runner")
    parser.add_argument(
        "command",
        choices=(
            "seed-municipalities",
            "discover",
            "discover-import",
            "fiscal-discover",
            "fiscal-import",
            "coverage",
        ),
    )
    parser.add_argument("--limit-municipalities", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.command == "seed-municipalities":
            summary = seed_burgos_municipalities(db)
        elif args.command == "discover":
            seed_summary = seed_burgos_municipalities(db)
            summary = {
                "seed": seed_summary,
                "discovery": discover_bopbur_candidates(
                    db,
                    limit_municipalities=args.limit_municipalities,
                    sleep_seconds=0 if args.max_workers > 1 else 0.15,
                    max_workers=args.max_workers,
                ),
            }
        elif args.command == "discover-import":
            summary = run_discover_import(
                db,
                limit_municipalities=args.limit_municipalities,
                max_workers=args.max_workers,
                dry_run=args.dry_run,
            )
        elif args.command == "fiscal-discover":
            seed_summary = seed_burgos_municipalities(db)
            _ensure_diputacion_fiscal_source(db)
            summary = {
                "seed": seed_summary,
                "discovery": discover_diputacion_fiscal_candidates(db),
            }
        elif args.command == "fiscal-import":
            summary = run_diputacion_fiscal_import(db, dry_run=args.dry_run)
        else:
            summary = build_burgos_coverage_report(db)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
