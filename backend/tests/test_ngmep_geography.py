import csv
import hashlib
import io
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from conftest import headers_for
from sqlalchemy import func, select

from app.municipalities import ngmep_geography
from app.municipalities.models import (
    Municipality,
    MunicipalityGeographySnapshot,
    ReferenceDatasetVersion,
)
from app.municipalities.ngmep_geography import (
    CSV_HEADERS,
    NGMEP_SOURCE_SPECS,
    NgmepGeographyError,
    apply_ngmep_sync,
    build_ngmep_sync_plan,
    load_ngmep_geography,
)
def ngmep_row(**overrides: str) -> dict[str, str]:
    row = {
        "COD_INE": "05001000000",
        "ID_REL": "1050013",
        "COD_GEO": "05003",
        "COD_PROV": "05",
        "PROVINCIA": "Ávila",
        "NOMBRE_ACTUAL": "Adanero",
        "POBLACION_MUNI": "214",
        "SUPERFICIE": "3141,7781",
        "PERIMETRO": "24382",
        "COD_INE_CAPITAL": "05001000101",
        "CAPITAL": "Adanero",
        "POBLACION_CAPITAL": "214",
        "HOJA_MTN25": "0481-2",
        "LONGITUD_ETRS89_REGCAN95": "-4,604007136",
        "LATITUD_ETRS89_REGCAN95": "40,94378789",
        "ORIGENCOOR": "Detección automática",
        "ALTITUD": "908",
        "ORIGENALTITUD": "MDT",
    }
    row.update(overrides)
    return row


def build_archive(
    rows: list[dict[str, str]],
    *,
    headers: tuple[str, ...] = CSV_HEADERS,
) -> tuple[bytes, object]:
    text_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        text_buffer,
        fieldnames=list(headers),
        delimiter=";",
        lineterminator="\r\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    content = text_buffer.getvalue().encode("cp1252")

    archive_buffer = io.BytesIO()
    with ZipFile(archive_buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("MUNICIPIOS.csv", content)
    archive_bytes = archive_buffer.getvalue()

    region_rows = [row for row in rows if row.get("COD_PROV") in {"05", "09", "24", "34", "37", "40", "42", "47", "49"}]
    province_counts = {
        code: sum(row.get("COD_PROV") == code for row in region_rows)
        for code in sorted({row["COD_PROV"] for row in region_rows})
    }
    spec = replace(
        NGMEP_SOURCE_SPECS[2026],
        expected_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
        expected_content_sha256=hashlib.sha256(content).hexdigest(),
        expected_rows=len(rows),
        expected_region_rows=len(region_rows),
        expected_province_counts=tuple(province_counts.items()),
    )
    return archive_bytes, spec


def load_single_row(**overrides: str):
    archive, spec = build_archive([ngmep_row(**overrides)])
    return load_ngmep_geography(
        archive,
        spec,
        retrieved_at=datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc),
    )


def make_municipality(db, **overrides) -> Municipality:
    values = {
        "name": "Adanero",
        "province": "Ávila",
        "autonomous_community": "Castilla y León",
        "ine_code": "05001",
        "population": 214,
        "population_reference_year": 2025,
        "population_source_url": "https://www.ine.es/pob_xls/pobmun.zip",
        "population_source_sha256": "a" * 64,
    }
    values.update(overrides)
    municipality = Municipality(**values)
    db.add(municipality)
    db.commit()
    return municipality


def test_parser_preserves_codes_converts_units_and_records_provenance() -> None:
    records, provenance, province_counts = load_single_row()

    record = records["05001"]
    assert record.source_municipality_code == "05001000000"
    assert record.relationship_id == 1050013
    assert record.surface_km2 == Decimal("31.417781")
    assert record.perimeter_m == Decimal("24382.000")
    assert record.longitude == Decimal("-4.604007136")
    assert record.latitude == Decimal("40.943787890")
    assert record.altitude_m == Decimal("908.00")
    assert record.capital_ine_code == "05001000101"
    assert province_counts == {"05": 1}
    assert provenance.reference_date == date(2026, 3, 31)
    assert provenance.content_sha256
    assert provenance.license_name == "CC BY 4.0"


def test_parser_rejects_unreviewed_hash_header_and_invalid_rows() -> None:
    archive, spec = build_archive([ngmep_row()])

    with pytest.raises(NgmepGeographyError, match="ZIP.*versión revisada"):
        load_ngmep_geography(
            archive,
            replace(spec, expected_archive_sha256="0" * 64),
        )

    wrong_header_archive, wrong_header_spec = build_archive(
        [ngmep_row()],
        headers=CSV_HEADERS[:-1],
    )
    with pytest.raises(NgmepGeographyError, match="Cabeceras inesperadas"):
        load_ngmep_geography(wrong_header_archive, wrong_header_spec)

    for field_name, value, message in (
        ("COD_INE", "05001000001", "COD_INE"),
        ("COD_PROV", "09", "COD_PROV"),
        ("COD_INE_CAPITAL", "09001000101", "COD_INE_CAPITAL"),
        ("SUPERFICIE", "NaN", "decimal válido"),
        ("SUPERFICIE", "-1", "deben ser positivos"),
        ("LONGITUD_ETRS89_REGCAN95", "-9", "Castilla y León"),
    ):
        invalid_archive, invalid_spec = build_archive(
            [ngmep_row(**{field_name: value})]
        )
        with pytest.raises(NgmepGeographyError, match=message):
            load_ngmep_geography(invalid_archive, invalid_spec)


def test_parser_scopes_geographic_code_uniqueness_to_target_region() -> None:
    first_external = ngmep_row(
        COD_INE="01001000000",
        ID_REL="1010014",
        COD_GEO="00000",
        COD_PROV="01",
        PROVINCIA="Araba/Álava",
        NOMBRE_ACTUAL="Alegría-Dulantzi",
        COD_INE_CAPITAL="01001000101",
        CAPITAL="Alegría-Dulantzi",
        LONGITUD_ETRS89_REGCAN95="-2,512507724",
        LATITUD_ETRS89_REGCAN95="42,84045247",
    )
    second_external = ngmep_row(
        COD_INE="02001000000",
        ID_REL="1020014",
        COD_GEO="00000",
        COD_PROV="02",
        PROVINCIA="Albacete",
        NOMBRE_ACTUAL="Abengibre",
        COD_INE_CAPITAL="02001000101",
        CAPITAL="Abengibre",
        LONGITUD_ETRS89_REGCAN95="-1,850000000",
        LATITUD_ETRS89_REGCAN95="39,00000000",
    )
    archive, spec = build_archive(
        [first_external, second_external, ngmep_row()]
    )

    records, _, _ = load_ngmep_geography(archive, spec)

    assert list(records) == ["05001"]


def test_sync_applies_versioned_geography_and_is_idempotent(db) -> None:
    municipality = make_municipality(db)
    records, provenance, province_counts = load_single_row()

    plan = build_ngmep_sync_plan(
        db,
        records,
        provenance,
        province_counts,
    )

    assert plan.blocking_issues == []
    assert plan.matched_rows == 1
    assert len(plan.updates) == 1
    assert municipality.surface_km2 is None

    apply_ngmep_sync(db, plan)

    db.refresh(municipality)
    assert municipality.surface_km2 == pytest.approx(31.417781)
    assert municipality.density == pytest.approx(214 / 31.417781)
    snapshot = db.scalar(
        select(MunicipalityGeographySnapshot).where(
            MunicipalityGeographySnapshot.municipality_id == municipality.id,
            MunicipalityGeographySnapshot.is_current.is_(True),
        )
    )
    assert snapshot is not None
    assert snapshot.capital_name == "Adanero"
    assert snapshot.surface_km2 == Decimal("31.417781")
    assert snapshot.dataset_version.content_sha256 == provenance.content_sha256

    second = build_ngmep_sync_plan(
        db,
        records,
        provenance,
        province_counts,
    )
    assert second.blocking_issues == []
    assert second.updates == []
    assert second.unchanged_rows == 1
    assert db.scalar(select(func.count(ReferenceDatasetVersion.id))) == 1
    assert db.scalar(select(func.count(MunicipalityGeographySnapshot.id))) == 1


def test_sync_keeps_ine_population_and_versions_a_newer_snapshot(db) -> None:
    municipality = make_municipality(db, population=220)
    records, provenance, province_counts = load_single_row()
    first = build_ngmep_sync_plan(db, records, provenance, province_counts)
    assert len(first.population_mismatches) == 1
    apply_ngmep_sync(db, first)

    newer_record = replace(
        records["05001"],
        surface_km2=Decimal("32.000000"),
        perimeter_m=Decimal("25000.000"),
    )
    newer_provenance = replace(
        provenance,
        version_label="NGMEP 2027-03-31",
        reference_date=date(2027, 3, 31),
        archive_sha256="b" * 64,
        content_sha256="c" * 64,
    )
    newer = build_ngmep_sync_plan(
        db,
        {"05001": newer_record},
        newer_provenance,
        province_counts,
    )
    assert newer.blocking_issues == []
    apply_ngmep_sync(db, newer)

    db.refresh(municipality)
    assert municipality.population == 220
    assert municipality.surface_km2 == 32.0
    snapshots = list(
        db.scalars(
            select(MunicipalityGeographySnapshot)
            .where(
                MunicipalityGeographySnapshot.municipality_id == municipality.id
            )
            .order_by(MunicipalityGeographySnapshot.id)
        )
    )
    assert len(snapshots) == 2
    assert [snapshot.is_current for snapshot in snapshots] == [False, True]


def test_sync_blocks_missing_municipality_and_inconsistent_same_source(db) -> None:
    records, provenance, province_counts = load_single_row()

    missing = build_ngmep_sync_plan(
        db,
        records,
        provenance,
        province_counts,
    )
    assert len(missing.missing_municipalities) == 1
    with pytest.raises(NgmepGeographyError, match="conflictos"):
        apply_ngmep_sync(db, missing)
    assert db.scalar(select(func.count(ReferenceDatasetVersion.id))) == 0

    municipality = make_municipality(db)
    first = build_ngmep_sync_plan(db, records, provenance, province_counts)
    apply_ngmep_sync(db, first)
    municipality.surface_km2 = 999
    db.commit()

    blocked = build_ngmep_sync_plan(
        db,
        records,
        provenance,
        province_counts,
    )
    assert blocked.database_conflicts[0].reason == "inconsistent_official_geography"

    repair = build_ngmep_sync_plan(
        db,
        records,
        provenance,
        province_counts,
        overwrite_existing=True,
    )
    assert repair.blocking_issues == []
    assert repair.updates[0].replace_existing is True
    apply_ngmep_sync(db, repair)
    assert municipality.surface_km2 == pytest.approx(31.417781)


def test_municipality_api_exposes_official_geography(client, db, superuser) -> None:
    municipality = make_municipality(db)
    records, provenance, province_counts = load_single_row()
    apply_ngmep_sync(
        db,
        build_ngmep_sync_plan(db, records, provenance, province_counts),
    )

    response = client.get(
        f"/municipalities/{municipality.id}",
        headers=headers_for(superuser),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["official_geography"]["capital_name"] == "Adanero"
    assert body["official_geography"]["latitude"] == pytest.approx(40.94378789)
    assert body["official_geography"]["crs"] == "EPSG:4258"
    assert body["official_geography"]["dataset_version"]["license_name"] == "CC BY 4.0"


def test_cli_is_dry_run_unless_apply_is_explicit(monkeypatch, db, capsys) -> None:
    archive, spec = build_archive([ngmep_row()])
    make_municipality(db)
    monkeypatch.setitem(ngmep_geography.NGMEP_SOURCE_SPECS, 2026, spec)
    monkeypatch.setattr(
        ngmep_geography,
        "download_ngmep_archive",
        lambda selected_spec: archive,
    )
    monkeypatch.setattr(
        ngmep_geography,
        "SessionLocal",
        lambda: db,
    )

    exit_code = ngmep_geography.main(["--year", "2026"])

    assert exit_code == 0
    summary = capsys.readouterr().out
    assert '"mode": "dry-run"' in summary
    assert db.scalar(select(func.count(MunicipalityGeographySnapshot.id))) == 0
