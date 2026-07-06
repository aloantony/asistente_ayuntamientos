"""Import manually verified official fallback URLs for Burgos gaps."""

from __future__ import annotations

import json
from urllib import parse

from sqlalchemy import select

from app.db.session import SessionLocal
from app.ordinances.burgos_full_coverage import approve_successful_job_items
from app.ordinances.import_service import run_import_job
from app.ordinances.models import OfficialLegalSource, OrdinanceImportJob

CANDIDATES = [
    {
        "municipality_id": 78,
        "municipality_name": "Carcedo de Bureba",
        "url": "https://bopbur.diputaciondeburgos.es/sites/default/files/bops/BOP_2004-12-17.pdf",
        "title": "BOP Burgos 17/12/2004 - ordenanzas fiscales Carcedo de Bureba",
        "domain": "bopbur.diputaciondeburgos.es",
        "source_name": "Boletín Oficial de la Provincia de Burgos",
        "source_type": "bop",
    },
    {
        "municipality_id": 119,
        "municipality_name": "Fresneda de la Sierra Tirón",
        "url": "https://bopbur.diputaciondeburgos.es/sites/default/files/bops/BOP_2004-04-29.pdf",
        "title": "BOP Burgos 29/04/2004 - ordenanza reguladora tasa recogida de basuras",
        "domain": "bopbur.diputaciondeburgos.es",
        "source_name": "Boletín Oficial de la Provincia de Burgos",
        "source_type": "bop",
    },
    {
        "municipality_id": 365,
        "municipality_name": "Villasandino",
        "url": "https://bopbur.diputaciondeburgos.es/sites/default/files/bops/BOP_2010-06-08.pdf",
        "title": "BOP Burgos 08/06/2010 - ordenanza contribuciones especiales Villasandino",
        "domain": "bopbur.diputaciondeburgos.es",
        "source_name": "Boletín Oficial de la Provincia de Burgos",
        "source_type": "bop",
    },
    {
        "municipality_id": 236,
        "municipality_name": "Quintanilla del Agua y Tordueles",
        "url": "https://quintanilladelaguaytordueles.es/documentacion/ordenanza-fiscal-reguladora-de-la-tasa-por-la-prestacion-de-los-servicios-de-piscinas",
        "title": "Ordenanza fiscal reguladora de la tasa por servicios de piscinas",
        "domain": "quintanilladelaguaytordueles.es",
        "source_name": "Ayuntamiento de Quintanilla del Agua y Tordueles",
        "source_type": "municipal",
    },
]


def source_for(db, candidate: dict) -> OfficialLegalSource:
    domain = candidate["domain"]
    source = db.scalar(select(OfficialLegalSource).where(OfficialLegalSource.domain == domain))
    if source is None:
        scheme = parse.urlparse(candidate["url"]).scheme or "https"
        source = OfficialLegalSource(
            name=candidate["source_name"],
            base_url=f"{scheme}://{domain}/",
            domain=domain,
            source_type=candidate["source_type"],
            status="active",
            notes="Fuente oficial fallback para cobertura de ordenanzas Burgos.",
        )
        db.add(source)
        db.commit()
        db.refresh(source)
    return source


def main() -> None:
    with SessionLocal() as db:
        source_urls = []
        source_ids = set()
        for candidate in CANDIDATES:
            source = source_for(db, candidate)
            source_ids.add(source.id)
            source_urls.append(
                {
                    "url": candidate["url"],
                    "municipality_id": candidate["municipality_id"],
                    "official_source_id": source.id,
                    "title": candidate["title"],
                }
            )
        job = OrdinanceImportJob(
            title="MVP Ordenanzas Burgos - fallback manual oficial gaps",
            description="Importación de URLs oficiales verificadas manualmente para municipios sin cobertura.",
            topic="ordenanzas municipales",
            subtopic="fallback manual oficial",
            municipality_ids_json="[]",
            official_source_ids_json=json.dumps(sorted(source_ids)),
            source_urls_json=json.dumps(source_urls, ensure_ascii=False),
            review_criteria="URL oficial BOPBUR o web municipal oficial con ordenanza/normativa.",
            status="draft",
        )
        db.add(job)
        db.commit()
        print(json.dumps({"job_id": job.id, "urls": len(source_urls)}, ensure_ascii=False))
        run_import_job(job.id, db=db)
        print(json.dumps(approve_successful_job_items(db, job.id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
