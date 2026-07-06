"""Targeted BOP Soria retry for municipalities still without coverage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import json
import time

from sqlalchemy import select, text

from app.db.session import SessionLocal
from app.municipalities.models import Municipality
from app.ordinances.burgos_full_coverage import approve_successful_job_items
from app.ordinances.import_service import run_import_job
from app.ordinances.soria_coverage import (
    BopSoriaCandidate,
    _candidate_from_detail,
    _normalize_key,
    _search_result_detail_urls,
    create_source_job,
    coverage_report,
)

QUERY_TEMPLATES = (
    "{name} ordenanza",
    "{name} ordenanza fiscal",
    "{name} reglamento",
    "{name} tasa",
)


def uncovered_soria_municipalities(db):
    rows = db.execute(
        text(
            """
            with covered as (
              select distinct m.name
              from ordinances o
              join municipalities m on m.id=o.municipality_id
              where m.province ilike 'Soria' and o.curation_status='approved'
            )
            select m.*
            from municipalities m
            left join covered c on c.name=m.name
            where m.province ilike 'Soria'
              and m.status='active'
              and c.name is null
            order by m.name
            """
        )
    )
    return [db.get(Municipality, row.id) for row in rows]


def municipality_search_name(name: str) -> str:
    if ", La" in name:
        return "La " + name.replace(", La", "")
    if ", Las" in name:
        return "Las " + name.replace(", Las", "")
    if ", Los" in name:
        return "Los " + name.replace(", Los", "")
    if ", El" in name:
        return "El " + name.replace(", El", "")
    return name


def discover_for_gap(municipality, municipality_by_key, *, max_results_per_query: int, workers: int):
    search_name = municipality_search_name(municipality.name)
    candidates: list[BopSoriaCandidate] = []
    seen_urls: set[str] = set()
    for template in QUERY_TEMPLATES:
        query = template.format(name=search_name)
        try:
            detail_urls = _search_result_detail_urls(query, max_results=max_results_per_query)
        except Exception:
            continue
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [
                executor.submit(
                    _candidate_from_detail,
                    detail_url,
                    municipality_by_key=municipality_by_key,
                    query=query,
                )
                for detail_url in detail_urls
            ]
            for future in as_completed(futures):
                for candidate in future.result():
                    if candidate.municipality_id != municipality.id:
                        continue
                    if candidate.source_url in seen_urls:
                        continue
                    seen_urls.add(candidate.source_url)
                    candidates.append(candidate)
        time.sleep(0.2)
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-results-per-query", type=int, default=80)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        before = coverage_report(db)
        gaps = uncovered_soria_municipalities(db)
        all_soria = list(
            db.scalars(
                select(Municipality).where(
                    Municipality.province.ilike("Soria"),
                    Municipality.status == "active",
                )
            )
        )
        municipality_by_key = {_normalize_key(m.name): m for m in all_soria}
        all_candidates: list[BopSoriaCandidate] = []
        per_municipality: dict[str, int] = {}
        for index, municipality in enumerate(gaps, start=1):
            candidates = discover_for_gap(
                municipality,
                municipality_by_key,
                max_results_per_query=args.max_results_per_query,
                workers=args.workers,
            )
            per_municipality[municipality.name] = len(candidates)
            all_candidates.extend(candidates)
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(gaps)}",
                        "municipality": municipality.name,
                        "candidates": len(candidates),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        unique: dict[str, BopSoriaCandidate] = {}
        for candidate in all_candidates:
            unique.setdefault(candidate.source_url, candidate)
        candidates = sorted(
            unique.values(),
            key=lambda candidate: (
                candidate.municipality_name,
                candidate.title,
                candidate.source_url,
            ),
        )
        result = {
            "coverage_before": before,
            "gaps": len(gaps),
            "candidates": len(candidates),
            "candidate_municipalities": len({candidate.municipality_name for candidate in candidates}),
            "per_municipality": per_municipality,
            "sample": [candidate.__dict__ for candidate in candidates[:30]],
            "dry_run": args.dry_run,
        }
        if not args.dry_run and candidates:
            job = create_source_job(db, candidates)
            if job is not None:
                run_import_job(job.id, db=db)
                result["job_id"] = job.id
                result["approvals"] = approve_successful_job_items(db, job.id)
        result["coverage_after"] = coverage_report(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
