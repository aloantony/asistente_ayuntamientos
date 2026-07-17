import hashlib
import io
import json
from contextlib import nullcontext
from dataclasses import replace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook
from sqlalchemy.exc import IntegrityError

from app.municipalities import ine_population
from app.municipalities.ine_population import (
    INE_SOURCE_SPECS,
    InePopulationError,
    InePopulationRecord,
    IneSourceSpec,
    PopulationProvenance,
    apply_population_sync,
    build_population_sync_plan,
    extract_workbook,
    load_ine_population,
)
from app.municipalities.models import Municipality


def build_archive(
    rows: list[tuple[object, ...]],
    *,
    title: str | None = None,
    headers: tuple[str, ...] | None = None,
) -> tuple[bytes, IneSourceSpec]:
    base_spec = INE_SOURCE_SPECS[2025]
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
    spec = replace(
        base_spec,
        expected_sha256=hashlib.sha256(workbook_bytes).hexdigest(),
        expected_rows=len(rows),
    )

    archive_buffer = io.BytesIO()
    with ZipFile(archive_buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(spec.workbook_member, workbook_bytes)
    return archive_buffer.getvalue(), spec


def population_record(
    *,
    ine_code: str = "09137",
    name: str = "Fuentelcésped",
    population: int = 290,
) -> InePopulationRecord:
    return InePopulationRecord(
        ine_code=ine_code,
        province="Burgos",
        name=name,
        population=population,
        men=157,
        women=population - 157,
    )


def provenance(**overrides) -> PopulationProvenance:
    values = {
        "reference_year": 2025,
        "source_url": INE_SOURCE_SPECS[2025].archive_url,
        "source_sha256": "a" * 64,
    }
    values.update(overrides)
    return PopulationProvenance(**values)


def test_load_ine_population_preserves_leading_zero_codes() -> None:
    archive, spec = build_archive(
        [(9, "Burgos", 137, "Fuentelcésped", 290, 157, 133)]
    )

    records, source = load_ine_population(archive, spec)

    assert records == {
        "09137": InePopulationRecord(
            ine_code="09137",
            province="Burgos",
            name="Fuentelcésped",
            population=290,
            men=157,
            women=133,
        )
    }
    assert source.reference_year == 2025
    assert source.source_url == spec.archive_url
    assert source.source_sha256 == spec.expected_sha256


def test_extract_workbook_rejects_an_unreviewed_source_revision() -> None:
    archive, spec = build_archive(
        [(9, "Burgos", 137, "Fuentelcésped", 290, 157, 133)]
    )

    with pytest.raises(InePopulationError, match="versión revisada"):
        extract_workbook(archive, replace(spec, expected_sha256="0" * 64))


@pytest.mark.parametrize(
    ("rows", "expected_error"),
    [
        (
            [(9, "Burgos", 137, "Fuentelcésped", 290, 157, 132)],
            "HOMBRES \\+ MUJERES",
        ),
        (
            [
                (9, "Burgos", 137, "Fuentelcésped", 290, 157, 133),
                (9, "Burgos", 137, "Nombre duplicado", 290, 157, 133),
            ],
            "código INE duplicado",
        ),
    ],
)
def test_load_ine_population_rejects_inconsistent_rows(
    rows: list[tuple[object, ...]],
    expected_error: str,
) -> None:
    archive, spec = build_archive(rows)

    with pytest.raises(InePopulationError, match=expected_error):
        load_ine_population(archive, spec)


def test_load_ine_population_rejects_unexpected_headers() -> None:
    base_headers = INE_SOURCE_SPECS[2025].headers
    archive, spec = build_archive(
        [(9, "Burgos", 137, "Fuentelcésped", 290, 157, 133)],
        headers=("PROVINCIA", *base_headers[1:]),
    )

    with pytest.raises(InePopulationError, match="Cabeceras inesperadas"):
        load_ine_population(archive, spec)


def test_sync_is_dry_run_by_default_and_apply_is_idempotent(db) -> None:
    municipality = Municipality(
        name="Fuentelcesped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code="09137",
        surface_km2=2.0,
    )
    without_code = Municipality(
        name="Sin código",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add_all([municipality, without_code])
    db.commit()
    record = population_record()
    source = provenance()

    plan = build_population_sync_plan(db, {record.ine_code: record}, source)

    assert plan.matched_rows == 1
    assert len(plan.updates) == 1
    assert len(plan.missing_ine_codes) == 1
    assert municipality.population is None

    apply_population_sync(db, plan)

    assert municipality.population == 290
    assert municipality.population_reference_year == 2025
    assert municipality.population_source_url == source.source_url
    assert municipality.population_source_sha256 == source.source_sha256
    assert municipality.density == 145.0

    second_plan = build_population_sync_plan(
        db,
        {record.ine_code: record},
        source,
    )
    assert second_plan.unchanged_rows == 1
    assert second_plan.updates == []


def test_cli_is_a_dry_run_unless_apply_is_explicit(
    db,
    monkeypatch,
    capsys,
) -> None:
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code="09137",
    )
    db.add(municipality)
    db.commit()
    record = population_record()
    source = provenance()
    applied_plans = []
    monkeypatch.setattr(
        ine_population,
        "download_ine_archive",
        lambda spec: b"fixture archive",
    )
    monkeypatch.setattr(
        ine_population,
        "load_ine_population",
        lambda archive, spec: ({record.ine_code: record}, source),
    )
    monkeypatch.setattr(
        ine_population,
        "SessionLocal",
        lambda: nullcontext(db),
    )
    monkeypatch.setattr(
        ine_population,
        "apply_population_sync",
        lambda session, plan: applied_plans.append(plan),
    )

    exit_code = ine_population.main([])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "dry-run"
    assert output["database"]["would_update"] == 1
    assert applied_plans == []
    assert municipality.population is None


def test_sync_requires_explicit_overwrite_for_unprovenanced_population(db) -> None:
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code="09137",
        population=100,
    )
    db.add(municipality)
    db.commit()
    record = population_record()
    source = provenance()

    blocked_plan = build_population_sync_plan(
        db,
        {record.ine_code: record},
        source,
    )

    assert len(blocked_plan.conflicts) == 1
    assert blocked_plan.conflicts[0].reason == "unprovenanced_population"
    with pytest.raises(InePopulationError, match="conflictos"):
        apply_population_sync(db, blocked_plan)
    assert municipality.population == 100

    allowed_plan = build_population_sync_plan(
        db,
        {record.ine_code: record},
        source,
        overwrite_existing=True,
    )
    assert allowed_plan.conflicts == []
    apply_population_sync(db, allowed_plan)
    assert municipality.population == 290


def test_sync_never_overwrites_a_newer_official_value(db) -> None:
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
        ine_code="09137",
        population=300,
        population_reference_year=2026,
        population_source_url=INE_SOURCE_SPECS[2025].archive_url,
        population_source_sha256="b" * 64,
    )
    db.add(municipality)
    db.commit()
    record = population_record()

    plan = build_population_sync_plan(
        db,
        {record.ine_code: record},
        provenance(),
        overwrite_existing=True,
    )

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].reason == "newer_population"


def test_apply_rolls_back_all_rows_when_provenance_violates_database_checks(db) -> None:
    municipalities = [
        Municipality(
            name="Fuentelcésped",
            province="Burgos",
            autonomous_community="Castilla y León",
            ine_code="09137",
        ),
        Municipality(
            name="Estépar",
            province="Burgos",
            autonomous_community="Castilla y León",
            ine_code="09125",
        ),
    ]
    db.add_all(municipalities)
    db.commit()
    records = {
        "09137": population_record(),
        "09125": population_record(
            ine_code="09125",
            name="Estépar",
            population=646,
        ),
    }
    invalid_source = provenance(source_url="http://example.invalid/source.zip")
    plan = build_population_sync_plan(db, records, invalid_source)

    with pytest.raises(IntegrityError):
        apply_population_sync(db, plan)

    for municipality in municipalities:
        db.refresh(municipality)
        assert municipality.population is None
        assert municipality.population_reference_year is None
