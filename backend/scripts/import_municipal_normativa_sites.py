"""Import discovered municipal normativa pages for remaining Burgos gaps."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib import parse, request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.ordinances.burgos_full_coverage import approve_successful_job_items
from app.ordinances.import_service import run_import_job
from app.ordinances.models import OfficialLegalSource, OrdinanceImportJob

SITES = [
    (74, "Campolara", "https://campolara.es/normativa"),
    (113, "Encío", "https://encio.es/normativa"),
    (135, "Gumiel de Izán", "https://www.gumieldeizan.es/normativa"),
    (159, "Jaramillo Quemado", "https://www.jaramilloquemado.es/normativa"),
    (174, "Mecerreyes", "https://mecerreyes.es/normativa"),
    (188, "Monterrubio de la Demanda", "https://monterrubiodelademanda.es/normativa"),
    (191, "Nava de Roa", "https://navaderoa.es/normativa"),
    (210, "Partido de la Sierra en Tobalina", "https://www.partidodelasierraentobalina.es/normativa"),
    (213, "Pedrosa del Páramo", "https://www.pedrosadelparamo.es/normativa"),
    (242, "Rábanos", "https://www.rabanos.es/normativa"),
    (256, "Roa", "https://www.roadeduero.es/normativa"),
    (270, "San Millán de Lara", "https://www.sanmillandelara.es/normativa"),
    (299, "Tinieblas de la Sierra", "https://www.tinieblasdelasierra.es/normativa"),
    (304, "Torrelara", "https://torrelara.es/normativa"),
    (308, "Tosantos", "https://tosantos.es/normativa"),
    (347, "Villalbilla de Burgos", "https://villalbilladeburgos.burgos.es/normativa"),
    (358, "Villanueva de Carazo", "https://www.villanuevadecarazo.es/normativa"),
    (367, "Villatuelda", "https://villatuelda.es/normativa"),
]


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.current: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.current = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.current:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current:
            self.links.append((self.current, " ".join(" ".join(self.text).split())))
            self.current = None


def fetch(url: str) -> str | None:
    try:
        return request.urlopen(
            request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=30,
        ).read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def discover_site_pages(base_url: str) -> list[str]:
    candidates: set[str] = set()
    pages_to_scan = [base_url, f"{base_url}?page=1", f"{base_url}?page=2", f"{base_url}?page=3"]
    for page_url in pages_to_scan:
        html = fetch(page_url)
        if not html:
            continue
        parser = LinkParser()
        parser.feed(html)
        page_has_normative_text = bool(re.search(r"ordenanza|reglamento|normativa", html, re.I))
        for href, text in parser.links:
            full_url = parse.urljoin(page_url, href)
            haystack = f"{full_url} {text}".lower()
            if "/node/" in full_url and re.search(r"leer|ordenanza|normativa", haystack):
                candidates.add(full_url)
            if full_url.lower().split("?", 1)[0].endswith(".pdf") and re.search(
                r"ordenanza|reglamento|normativa|normas|urban", haystack
            ):
                candidates.add(full_url)
        if page_has_normative_text and not candidates:
            candidates.add(page_url)
    return sorted(candidates)


def source_for(db, url: str, municipality_name: str) -> OfficialLegalSource:
    host = parse.urlparse(url).hostname or ""
    host = host.removeprefix("www.")
    source = db.scalar(select(OfficialLegalSource).where(OfficialLegalSource.domain == host))
    if source is None:
        source = OfficialLegalSource(
            name=f"Ayuntamiento de {municipality_name}",
            base_url=f"https://{host}/",
            domain=host,
            source_type="municipal",
            status="active",
            notes="Web municipal oficial con normativa/ordenanzas.",
        )
        db.add(source)
        db.commit()
        db.refresh(source)
    return source


def main() -> None:
    discovered = []
    errors = []
    with SessionLocal() as db:
        source_urls = []
        source_ids = set()
        for municipality_id, municipality_name, base_url in SITES:
            pages = discover_site_pages(base_url)
            if not pages:
                errors.append({"municipality": municipality_name, "base_url": base_url})
                continue
            source = source_for(db, base_url, municipality_name)
            source_ids.add(source.id)
            discovered.append(
                {"municipality": municipality_name, "base_url": base_url, "pages": len(pages)}
            )
            for page in pages:
                source_urls.append(
                    {
                        "url": page,
                        "municipality_id": municipality_id,
                        "official_source_id": source.id,
                        "title": f"Normativa municipal {municipality_name}",
                    }
                )
        print(json.dumps({"discovered": discovered, "errors": errors, "urls": len(source_urls)}, ensure_ascii=False, indent=2))
        if not source_urls:
            return
        job = OrdinanceImportJob(
            title="MVP Ordenanzas Burgos - webs municipales normativa gaps",
            description="Importación fallback desde webs municipales oficiales con sección de normativa.",
            topic="ordenanzas municipales",
            subtopic="web municipal normativa",
            municipality_ids_json="[]",
            official_source_ids_json=json.dumps(sorted(source_ids)),
            source_urls_json=json.dumps(source_urls, ensure_ascii=False),
            review_criteria="Web municipal oficial con páginas de normativa/ordenanzas.",
            status="draft",
        )
        db.add(job)
        db.commit()
        print(json.dumps({"job_id": job.id}, ensure_ascii=False))
        run_import_job(job.id, db=db)
        print(json.dumps(approve_successful_job_items(db, job.id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
