"""Import Fuentespina municipal normativa pages."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib import parse, request

from sqlalchemy import select

from app.db.session import SessionLocal
from app.ordinances.burgos_full_coverage import approve_successful_job_items
from app.ordinances.import_service import run_import_job
from app.ordinances.models import OfficialLegalSource, OrdinanceImportJob


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


def main() -> None:
    base = "https://www.fuentespina.es/normativa"
    urls: set[str] = set()
    for page in ("", "?page=1", "?page=2"):
        url = base + page
        html = request.urlopen(
            request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=60,
        ).read().decode("utf-8", errors="ignore")
        parser = LinkParser()
        parser.feed(html)
        for href, _text in parser.links:
            full_url = parse.urljoin(url, href)
            if "/node/" in full_url:
                urls.add(full_url)

    with SessionLocal() as db:
        source = db.scalar(
            select(OfficialLegalSource).where(OfficialLegalSource.domain == "fuentespina.es")
        )
        if source is None:
            source = OfficialLegalSource(
                name="Ayuntamiento de Fuentespina",
                base_url="https://www.fuentespina.es/",
                domain="fuentespina.es",
                source_type="municipal",
                status="active",
                notes="Web municipal oficial con normativa/ordenanzas.",
            )
            db.add(source)
            db.commit()
            db.refresh(source)
        source_urls = [
            {
                "url": url,
                "municipality_id": 130,
                "official_source_id": source.id,
                "title": "Normativa municipal Fuentespina",
            }
            for url in sorted(urls)
        ]
        job = OrdinanceImportJob(
            title="MVP Ordenanzas Burgos - Fuentespina web municipal normativa",
            description="Importación fallback desde web municipal oficial de Fuentespina.",
            topic="ordenanzas municipales",
            subtopic="normativa municipal Fuentespina",
            municipality_ids_json="[]",
            official_source_ids_json=json.dumps([source.id]),
            source_urls_json=json.dumps(source_urls, ensure_ascii=False),
            review_criteria="Web municipal oficial con páginas de normativa/ordenanzas.",
            status="draft",
        )
        db.add(job)
        db.commit()
        print(json.dumps({"job_id": job.id, "urls": len(source_urls)}, ensure_ascii=False))
        run_import_job(job.id, db=db)
        print(json.dumps(approve_successful_job_items(db, job.id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
