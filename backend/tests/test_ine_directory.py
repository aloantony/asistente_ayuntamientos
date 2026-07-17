import hashlib
import io
import json
from contextlib import nullcontext
from dataclasses import replace

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.municipalities import ine_directory
from app.municipalities.ine_directory import (
    INE_DIRECTORY_SOURCE_SPECS,
    DirectoryProvenance,
    IneDirectoryError,
    IneDirectoryRecord,
    apply_directory_sync,
    build_directory_sync_plan,
    load_castilla_y_leon_directory,
)
from app.municipalities.ine_population import (
    INE_SOURCE_SPECS,
    InePopulationRecord,
    PopulationProvenance,
)
from app.municipalities.models import Municipality


def build_directory_workbook(
    rows: list[tuple[object, ...]],
    *,
    title: str | None = None,
    headers: tuple[str, ...] | None = None,
    expected_province_counts: tuple[tuple[str, int], ...] | None = None,
) -> tuple[bytes, object]:
    base_spec = INE_DIRECTORY_SOURCE_SPECS[2026]
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append([title or base_spec.title])
    worksheet.append(list(headers or base_spec.headers))
    for row in rows:
        worksheet.append(list(row))

    workbook_buffer = io.BytesIO()
    workbook.save(workbook_buffer)
    workbook.close()
    workbook_bytes = workbook_buffer.getvalue()
    region_rows = [row for row in rows if str(row[0]).zfill(2) == "07"]
    province_counts = expected_province_counts or tuple(
        sorted(
            {
                str(row[1]).zfill(2): sum(
                    1
                    for candidate in region_rows
                    if str(candidate[1]).zfill(2) == str(row[1]).zfill(2)
                )
                for row in region_rows
            }.items()
        )
    )
    spec = replace(
        base_spec,
        expected_sha256=hashlib.sha256(workbook_bytes).hexdigest(),
        expected_rows=len(rows),
        expected_region_rows=len(region_rows),
        expected_province_counts=province_counts,
    )
    return workbook_bytes, spec


def directory_record(
    *,
    ine_code: str = "09137",
    name: str = "Fuentelcésped",
) -> IneDirectoryRecord:
    return IneDirectoryRecord(
        autonomous_community_code="07",
        province_code=ine_code[:2],
        municipality_code=ine_code[2:],
        check_digit="0",
        name=name,
    )


def population_record(
    *,
    ine_code: str = "09137",
    name: str = "Fuentelcésped",
    population: int = 290,
) -> InePopulationRecord:
    men = population // 2
    return InePopulationRecord(
        ine_code=ine_code,
        province="Burgos" if ine_code.startswith("09") else "Ávila",
        name=name,
        population=population,
        men=men,
        women=population - men,
    )


def directory_provenance() -> DirectoryProvenance:
    spec = INE_DIRECTORY_SOURCE_SPECS[2026]
    return DirectoryProvenance(
        reference_date=spec.reference_date,
        source_url=spec.workbook_url,
        source_sha256="b" * 64,
    )


def population_provenance() -> PopulationProvenance:
    return PopulationProvenance(
        reference_year=2025,
        source_url=INE_SOURCE_SPECS[2025].archive_url,
        source_sha256="a" * 64,
    )


def test_directory_parser_preserves_codes_and_validates_province_counts() -> None:
    workbook, spec = build_directory_workbook(
        [
            (7, 5, 1, 3, "Adanero"),
            (7, 9, 137, 0, "Fuentelcésped"),
            (16, 1, 51, 3, "Agurain/Salvatierra"),
        ]
    )

    records, source = load_castilla_y_leon_directory(workbook, spec)

    assert list(records) == ["05001", "09137"]
    assert records["05001"].province == "Ávila"
    assert records["09137"].check_digit == "0"
    assert source.reference_date.isoformat() == "2026-01-01"
    assert source.source_sha256 == spec.expected_sha256


def test_directory_parser_rejects_unreviewed_or_inconsistent_sources() -> None:
    workbook, spec = build_directory_workbook([(7, 9, 137, 0, "Fuentelcésped")])

    with pytest.raises(IneDirectoryError, match="versión revisada"):
        load_castilla_y_leon_directory(
            workbook,
            replace(spec, expected_sha256="0" * 64),
        )

    with pytest.raises(IneDirectoryError, match="Recuento provincial"):
        load_castilla_y_leon_directory(
            workbook,
            replace(spec, expected_province_counts=(("09", 2),)),
        )


def test_directory_sync_creates_missing_updates_existing_and_is_idempotent(db) -> None:
    existing = Municipality(
        name="Fuentelcesped",
        province="Burgos",
        autonomous_community="Castilla y Leon",
        ine_code="09137",
        surface_km2=2.0,
    )
    extra = Municipality(
        name="Municipio de prueba",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add_all([existing, extra])
    db.commit()
    directories = {
        "09137": directory_record(),
        "05001": directory_record(ine_code="05001", name="Adanero"),
    }
    populations = {
        "09137": population_record(),
        "05001": population_record(
            ine_code="05001",
            name="Adanero",
            population=960,
        ),
    }

    plan = build_directory_sync_plan(
        db,
        directories,
        populations,
        directory_provenance(),
        population_provenance(),
    )

    assert plan.blocking_issues == []
    assert plan.official_rows_before == 1
    assert len(plan.creates) == 1
    assert len(plan.updates) == 1
    assert len(plan.extra_region_rows) == 1
    assert len(plan.official_name_changes) == 1
    assert existing.population is None

    apply_directory_sync(db, plan)

    official = {
        row.ine_code: row
        for row in db.scalars(
            select(Municipality).where(Municipality.ine_code.in_(directories))
        )
    }
    assert official["09137"].name == "Fuentelcésped"
    assert official["09137"].autonomous_community == "Castilla y León"
    assert official["09137"].ine_check_digit == "0"
    assert official["09137"].directory_reference_date.isoformat() == "2026-01-01"
    assert official["09137"].directory_source_url.endswith("diccionario26.xlsx")
    assert official["09137"].directory_source_sha256 == "b" * 64
    assert official["09137"].population == 290
    assert official["09137"].density == 145.0
    assert official["05001"].province == "Ávila"
    assert official["05001"].population == 960
    assert official["05001"].population_reference_year == 2025

    second_plan = build_directory_sync_plan(
        db,
        directories,
        populations,
        directory_provenance(),
        population_provenance(),
    )
    assert second_plan.creates == []
    assert second_plan.updates == []
    assert second_plan.unchanged_rows == 2


def test_directory_sync_requires_explicit_population_overwrite(db) -> None:
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code="09137",
        population=100,
    )
    db.add(municipality)
    db.commit()
    directories = {"09137": directory_record()}
    populations = {"09137": population_record()}

    blocked = build_directory_sync_plan(
        db,
        directories,
        populations,
        directory_provenance(),
        population_provenance(),
    )

    assert len(blocked.population_conflicts) == 1
    assert blocked.population_conflicts[0].reason == "unprovenanced_population"
    with pytest.raises(IneDirectoryError, match="conflictos"):
        apply_directory_sync(db, blocked)
    assert municipality.population == 100

    allowed = build_directory_sync_plan(
        db,
        directories,
        populations,
        directory_provenance(),
        population_provenance(),
        overwrite_existing=True,
    )
    assert allowed.blocking_issues == []
    apply_directory_sync(db, allowed)
    assert municipality.population == 290


def test_directory_sync_blocks_when_population_is_missing(db) -> None:
    directories = {"09137": directory_record()}

    plan = build_directory_sync_plan(
        db,
        directories,
        {},
        directory_provenance(),
        population_provenance(),
    )

    assert plan.creates == []
    assert len(plan.missing_population_codes) == 1
    with pytest.raises(IneDirectoryError, match="conflictos"):
        apply_directory_sync(db, plan)


@pytest.mark.parametrize("legacy_ine_code", [None, "legacy-code"])
def test_directory_sync_blocks_a_possible_legacy_match_instead_of_guessing(
    db,
    legacy_ine_code: str | None,
) -> None:
    legacy = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code=legacy_ine_code,
    )
    db.add(legacy)
    db.commit()

    plan = build_directory_sync_plan(
        db,
        {"09137": directory_record()},
        {"09137": population_record()},
        directory_provenance(),
        population_provenance(),
    )

    assert plan.creates == []
    assert len(plan.legacy_match_conflicts) == 1
    assert plan.legacy_match_conflicts[0].municipality_id == legacy.id
    with pytest.raises(IneDirectoryError, match="conflictos"):
        apply_directory_sync(db, plan)


@pytest.mark.parametrize("legacy_ine_code", ["9137", "091370"])
def test_directory_sync_blocks_a_noncanonical_code_candidate(
    db,
    legacy_ine_code: str,
) -> None:
    legacy = Municipality(
        name="Nombre legacy distinto",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code=legacy_ine_code,
    )
    db.add(legacy)
    db.commit()

    plan = build_directory_sync_plan(
        db,
        {"09137": directory_record()},
        {"09137": population_record()},
        directory_provenance(),
        population_provenance(),
    )

    assert plan.creates == []
    assert len(plan.legacy_match_conflicts) == 1
    assert plan.legacy_match_conflicts[0].reason == "possible_legacy_code"
    assert plan.legacy_match_conflicts[0].municipality_id == legacy.id


def test_directory_cli_is_dry_run_unless_apply_is_explicit(
    db,
    monkeypatch,
    capsys,
) -> None:
    directories = {"09137": directory_record()}
    populations = {"09137": population_record()}
    applied_plans = []
    monkeypatch.setattr(
        ine_directory,
        "download_directory_workbook",
        lambda spec: b"directory fixture",
    )
    monkeypatch.setattr(
        ine_directory,
        "load_castilla_y_leon_directory",
        lambda data, spec: (directories, directory_provenance()),
    )
    monkeypatch.setattr(
        ine_directory,
        "download_ine_archive",
        lambda spec: b"population fixture",
    )
    monkeypatch.setattr(
        ine_directory,
        "load_ine_population",
        lambda data, spec: (populations, population_provenance()),
    )
    monkeypatch.setattr(ine_directory, "SessionLocal", lambda: nullcontext(db))
    monkeypatch.setattr(
        ine_directory,
        "apply_directory_sync",
        lambda session, plan: applied_plans.append(plan),
    )

    exit_code = ine_directory.main([])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "dry-run"
    assert output["database"]["would_create"] == 1
    assert applied_plans == []
