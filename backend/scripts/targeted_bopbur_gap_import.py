"""Targeted BOPBUR retry for Burgos municipalities still without coverage."""

from __future__ import annotations

import json
import time

from sqlalchemy import text

from app.db.session import SessionLocal
from app.ordinances.bop_burgos import (
    is_municipal_bopbur_announcement,
    is_normative_bopbur_announcement,
    natural_municipality_name,
    search_bop_burgos_announcements,
)
from app.ordinances.burgos_full_coverage import (
    approve_successful_job_items,
    create_source_job,
)
from app.ordinances.import_service import run_import_job

QUERIES = (
    "ordenanza",
    "ordenanza fiscal",
    "reglamento",
    "aprobación definitiva ordenanza",
    "aprobacion definitiva ordenanza",
)

GAP_SQL = """
with covered as (
  select distinct m.name
  from ordinances o
  join municipalities m on m.id=o.municipality_id
  where m.province ilike 'Burgos' and o.curation_status='approved'
)
select m.id, m.name
from municipalities m
left join covered c on c.name=m.name
where m.province ilike 'Burgos' and m.status='active' and c.name is null
order by m.name
"""


def main() -> None:
    with SessionLocal() as db:
        rows = db.execute(text(GAP_SQL)).fetchall()
        candidates = []
        errors = []
        seen = set()
        for municipality_id, name in rows:
            natural = natural_municipality_name(name)
            for query_base in QUERIES:
                query = f"{query_base} {natural}"
                try:
                    announcements = search_bop_burgos_announcements(query, limit=50)
                except Exception as error:  # noqa: BLE001
                    errors.append(
                        {
                            "municipality": name,
                            "query": query,
                            "error": str(error)[:200],
                        }
                    )
                    time.sleep(1)
                    continue
                for announcement in announcements:
                    if not is_municipal_bopbur_announcement(announcement, name):
                        continue
                    if not is_normative_bopbur_announcement(announcement):
                        continue
                    key = (municipality_id, announcement.pdf_url)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        {
                            "municipality_id": municipality_id,
                            "municipality_name": name,
                            "title": announcement.title,
                            "source_url": announcement.pdf_url,
                            "cve": announcement.cve,
                            "bulletin_number": announcement.bulletin_number,
                            "bulletin_date": announcement.bulletin_date,
                            "query": query,
                        }
                    )
                time.sleep(0.5)
        print(
            json.dumps(
                {
                    "municipalities": len(rows),
                    "candidates": len(candidates),
                    "candidate_municipalities": len(
                        {candidate["municipality_name"] for candidate in candidates}
                    ),
                    "errors": len(errors),
                    "sample": candidates[:10],
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        if not candidates:
            return
        job = create_source_job(
            db,
            candidates,
            title="MVP Ordenanzas Burgos - BOPBUR fallback municipios sin cobertura",
        )
        run_import_job(job.id, db=db)
        approval = approve_successful_job_items(db, job.id)
        print(
            json.dumps(
                {"job_id": job.id, "approval": approval},
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
