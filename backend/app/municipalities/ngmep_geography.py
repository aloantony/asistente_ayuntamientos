"""Synchronize versioned official municipality geography from IGN NGMEP.

The command is a dry-run unless ``--apply`` is supplied. Municipalities are
matched exclusively by their five-digit INE code. NGMEP population is retained
for source auditing, but never replaces the canonical INE population.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.municipalities.ine_directory import (
    CASTILLA_Y_LEON_NAME,
    EXPECTED_PROVINCE_COUNTS,
    PROVINCES,
)
from app.municipalities.ine_population import _density, _normalized_name
from app.municipalities.models import (
    Municipality,
    MunicipalityGeographySnapshot,
    ReferenceDatasetVersion,
)

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_CSV_BYTES = 3 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30.0
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_ISSUES_IN_SUMMARY = 25
CRS = "EPSG:4258"
DATASET_KEY = "ign_ngmep_municipalities"
INE_11_PATTERN = re.compile(r"^[0-9]{11}$")
CODE_5_PATTERN = re.compile(r"^[0-9]{5}$")
RELATIONSHIP_ID_PATTERN = re.compile(r"^[0-9]{7}$")
DECIMAL_PATTERN = re.compile(r"^-?[0-9]+(?:,[0-9]+)?$")
CASTILLA_Y_LEON_LONGITUDE = (Decimal("-8"), Decimal("-1"))
CASTILLA_Y_LEON_LATITUDE = (Decimal("40"), Decimal("44"))

CSV_HEADERS = (
    "COD_INE",
    "ID_REL",
    "COD_GEO",
    "COD_PROV",
    "PROVINCIA",
    "NOMBRE_ACTUAL",
    "POBLACION_MUNI",
    "SUPERFICIE",
    "PERIMETRO",
    "COD_INE_CAPITAL",
    "CAPITAL",
    "POBLACION_CAPITAL",
    "HOJA_MTN25",
    "LONGITUD_ETRS89_REGCAN95",
    "LATITUD_ETRS89_REGCAN95",
    "ORIGENCOOR",
    "ALTITUD",
    "ORIGENALTITUD",
)


class NgmepGeographyError(ValueError):
    """The official source or synchronization plan is not safe to apply."""


@dataclass(frozen=True)
class NgmepSourceSpec:
    year: int
    reference_date: date
    title: str
    version_label: str
    catalog_url: str
    download_url: str
    form_data: tuple[tuple[str, str], ...]
    member_name: str
    expected_archive_sha256: str
    expected_content_sha256: str
    expected_rows: int
    expected_region_rows: int
    expected_province_counts: tuple[tuple[str, int], ...]
    license_name: str
    license_url: str
    attribution: str


NGMEP_SOURCE_SPECS = {
    2026: NgmepSourceSpec(
        year=2026,
        reference_date=date(2026, 3, 31),
        title=(
            "Nomenclátor Geográfico de Municipios y Entidades de Población"
        ),
        version_label="NGMEP 2026-03-31",
        catalog_url=(
            "https://centrodedescargas.cnig.es/CentroDescargas/"
            "detalleArchivo?sec=9000004"
        ),
        download_url=(
            "https://centrodedescargas.cnig.es/CentroDescargas/descargaDir"
        ),
        form_data=(
            ("secuencial", "9000004"),
            ("secDescDirLA", "9000004"),
            ("codSerie", "NGMEN"),
        ),
        member_name="MUNICIPIOS.csv",
        expected_archive_sha256=(
            "496e3079d3b1844e2827d9dfc328fcd2e629e72c2640b46840daa3711b915116"
        ),
        expected_content_sha256=(
            "00784dfae9d601fd1d3cf2ac7e00e497e755922b260a103b6f01705041bb65a5"
        ),
        expected_rows=8132,
        expected_region_rows=2248,
        expected_province_counts=tuple(EXPECTED_PROVINCE_COUNTS.items()),
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="NGMEP CC-BY 4.0 Instituto Geográfico Nacional",
    )
}


@dataclass(frozen=True)
class NgmepRecord:
    ine_code: str
    source_municipality_code: str
    relationship_id: int
    geographic_code: str
    province_code: str
    province_name: str
    municipality_name: str
    source_population: int
    surface_km2: Decimal
    perimeter_m: Decimal
    capital_ine_code: str
    capital_name: str
    capital_population: int
    mtn25_sheet: str
    longitude: Decimal
    latitude: Decimal
    coordinate_origin: str
    altitude_m: Decimal
    altitude_origin: str


@dataclass(frozen=True)
class NgmepProvenance:
    dataset_key: str
    title: str
    version_label: str
    reference_date: date
    catalog_url: str
    download_url: str
    member_name: str
    archive_sha256: str
    content_sha256: str
    license_name: str
    license_url: str
    attribution: str
    retrieved_at: datetime
    national_row_count: int
    target_row_count: int


@dataclass(frozen=True)
class GeographyIssue:
    reason: str
    detail: str
    ine_code: str | None = None
    municipality_name: str | None = None
    municipality_id: int | None = None


@dataclass
class GeographyUpdate:
    municipality: Municipality
    record: NgmepRecord
    current_snapshot: MunicipalityGeographySnapshot | None
    replace_existing: bool = False


@dataclass
class NgmepSyncPlan:
    provenance: NgmepProvenance
    source_by_province: dict[str, int]
    database_rows: int
    matched_rows: int = 0
    unchanged_rows: int = 0
    dataset_version: ReferenceDatasetVersion | None = None
    updates: list[GeographyUpdate] = field(default_factory=list)
    missing_municipalities: list[GeographyIssue] = field(default_factory=list)
    database_conflicts: list[GeographyIssue] = field(default_factory=list)
    source_conflicts: list[GeographyIssue] = field(default_factory=list)
    name_mismatches: list[GeographyIssue] = field(default_factory=list)
    population_mismatches: list[GeographyIssue] = field(default_factory=list)

    @property
    def blocking_issues(self) -> list[GeographyIssue]:
        return [
            *self.missing_municipalities,
            *self.database_conflicts,
            *self.source_conflicts,
        ]

    def summary(self, *, applied: bool) -> dict[str, Any]:
        return {
            "ok": not self.blocking_issues,
            "mode": "apply" if applied else "dry-run",
            "source": {
                "dataset_key": self.provenance.dataset_key,
                "title": self.provenance.title,
                "version": self.provenance.version_label,
                "reference_date": self.provenance.reference_date.isoformat(),
                "catalog_url": self.provenance.catalog_url,
                "download_url": self.provenance.download_url,
                "member": self.provenance.member_name,
                "archive_sha256": self.provenance.archive_sha256,
                "content_sha256": self.provenance.content_sha256,
                "license": self.provenance.license_name,
                "license_url": self.provenance.license_url,
                "attribution": self.provenance.attribution,
                "retrieved_at": self.provenance.retrieved_at.isoformat(),
                "national_rows": self.provenance.national_row_count,
                "castilla_y_leon_rows": self.provenance.target_row_count,
                "by_province": self.source_by_province,
                "crs": CRS,
            },
            "database": {
                "rows": self.database_rows,
                "matched": self.matched_rows,
                "would_update": len(self.updates),
                "updated": len(self.updates) if applied else 0,
                "unchanged": self.unchanged_rows,
            },
            "semantics": {
                "canonical_population": "INE municipal population",
                "ngmep_population": "stored for source comparison only",
                "coordinates": (
                    "centroid of the municipal capital population nucleus; "
                    "not the municipal boundary centroid"
                ),
                "density": "INE population divided by IGN surface",
            },
            "issues": {
                "missing_municipalities": _issue_summary(
                    self.missing_municipalities
                ),
                "database_conflicts": _issue_summary(self.database_conflicts),
                "source_conflicts": _issue_summary(self.source_conflicts),
                "name_mismatches": _issue_summary(self.name_mismatches),
                "population_mismatches": _issue_summary(
                    self.population_mismatches
                ),
            },
        }


def download_ngmep_archive(
    spec: NgmepSourceSpec,
    *,
    max_bytes: int = MAX_ARCHIVE_BYTES,
    timeout_seconds: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> bytes:
    body = urlencode(dict(spec.form_data)).encode("ascii")
    request = Request(
        spec.download_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "asistente-ayuntamientos-ngmep-sync/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            final_url = urlsplit(response.geturl())
            expected_url = urlsplit(spec.download_url)
            if (
                final_url.scheme != "https"
                or final_url.hostname != expected_url.hostname
            ):
                raise NgmepGeographyError(
                    "La descarga del IGN redirigió a un origen no permitido"
                )

            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > max_bytes:
                raise NgmepGeographyError(
                    "El ZIP del NGMEP supera el tamaño permitido"
                )

            archive = bytearray()
            while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                archive.extend(chunk)
                if len(archive) > max_bytes:
                    raise NgmepGeographyError(
                        "El ZIP del NGMEP supera el tamaño permitido"
                    )
    except NgmepGeographyError:
        raise
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise NgmepGeographyError(
            f"No se pudo descargar el ZIP del NGMEP: {exc}"
        ) from exc

    if not archive:
        raise NgmepGeographyError("El ZIP descargado del NGMEP está vacío")
    return bytes(archive)


def read_local_ngmep_archive(
    path: Path,
    *,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise NgmepGeographyError(f"No se pudo leer el ZIP local: {exc}") from exc
    if size <= 0:
        raise NgmepGeographyError("El ZIP local está vacío")
    if size > max_bytes:
        raise NgmepGeographyError("El ZIP local supera el tamaño permitido")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise NgmepGeographyError(f"No se pudo leer el ZIP local: {exc}") from exc


def extract_municipalities_csv(
    archive_bytes: bytes,
    spec: NgmepSourceSpec,
    *,
    max_csv_bytes: int = MAX_CSV_BYTES,
) -> tuple[bytes, str, str]:
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise NgmepGeographyError("El ZIP del NGMEP supera el tamaño permitido")
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    if archive_sha256 != spec.expected_archive_sha256:
        raise NgmepGeographyError(
            "El ZIP del NGMEP no coincide con la versión revisada; "
            f"esperado {spec.expected_archive_sha256}, recibido {archive_sha256}"
        )

    try:
        with ZipFile(io.BytesIO(archive_bytes)) as archive:
            try:
                member = archive.getinfo(spec.member_name)
            except KeyError as exc:
                raise NgmepGeographyError(
                    f"El ZIP no contiene {spec.member_name}"
                ) from exc
            if member.flag_bits & 0x1:
                raise NgmepGeographyError("El CSV del NGMEP no puede estar cifrado")
            if member.file_size <= 0 or member.file_size > max_csv_bytes:
                raise NgmepGeographyError(
                    "El CSV del NGMEP tiene un tamaño no permitido"
                )
            content = archive.read(member)
    except NgmepGeographyError:
        raise
    except (BadZipFile, OSError) as exc:
        raise NgmepGeographyError(
            f"El archivo descargado no es un ZIP válido: {exc}"
        ) from exc

    if len(content) != member.file_size:
        raise NgmepGeographyError("El CSV extraído del NGMEP está incompleto")
    content_sha256 = hashlib.sha256(content).hexdigest()
    if content_sha256 != spec.expected_content_sha256:
        raise NgmepGeographyError(
            "El CSV del NGMEP no coincide con la versión revisada; "
            f"esperado {spec.expected_content_sha256}, recibido {content_sha256}"
        )
    return content, archive_sha256, content_sha256


def parse_municipalities_csv(
    content: bytes,
    spec: NgmepSourceSpec,
) -> tuple[dict[str, NgmepRecord], dict[str, int]]:
    try:
        decoded = content.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise NgmepGeographyError(
            f"El CSV del NGMEP no usa la codificación esperada: {exc}"
        ) from exc
    if "\x00" in decoded:
        raise NgmepGeographyError("El CSV del NGMEP contiene bytes NUL")

    reader = csv.DictReader(io.StringIO(decoded, newline=""), delimiter=";")
    if tuple(reader.fieldnames or ()) != CSV_HEADERS:
        raise NgmepGeographyError(
            f"Cabeceras inesperadas en el CSV del NGMEP: {reader.fieldnames!r}"
        )

    records: dict[str, NgmepRecord] = {}
    province_counts: Counter[str] = Counter()
    national_codes: set[str] = set()
    relationship_ids: set[int] = set()
    geographic_codes: set[str] = set()
    capital_codes: set[str] = set()
    national_rows = 0

    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise NgmepGeographyError(
                f"Fila {row_number}: número inesperado de columnas"
            )
        if not row or all(not str(value or "").strip() for value in row.values()):
            continue
        national_rows += 1
        values = {
            header: _required_text(row.get(header), header, row_number)
            for header in CSV_HEADERS
        }

        source_code = values["COD_INE"]
        if not INE_11_PATTERN.fullmatch(source_code) or not source_code.endswith(
            "000000"
        ):
            raise NgmepGeographyError(
                f"Fila {row_number}: COD_INE no es un código municipal válido"
            )
        if source_code in national_codes:
            raise NgmepGeographyError(
                f"Fila {row_number}: COD_INE duplicado {source_code}"
            )
        national_codes.add(source_code)

        province_code = values["COD_PROV"]
        if (
            not re.fullmatch(r"[0-9]{2}", province_code)
            or source_code[:2] != province_code
        ):
            raise NgmepGeographyError(
                f"Fila {row_number}: COD_PROV no coincide con COD_INE"
            )
        is_target_region = province_code in PROVINCES
        relationship_text = values["ID_REL"]
        if not RELATIONSHIP_ID_PATTERN.fullmatch(relationship_text):
            raise NgmepGeographyError(f"Fila {row_number}: ID_REL no es válido")
        relationship_id = int(relationship_text)
        if is_target_region and relationship_id in relationship_ids:
            raise NgmepGeographyError(
                f"Fila {row_number}: ID_REL duplicado {relationship_text}"
            )
        if is_target_region:
            relationship_ids.add(relationship_id)

        geographic_code = values["COD_GEO"]
        if not CODE_5_PATTERN.fullmatch(geographic_code):
            raise NgmepGeographyError(f"Fila {row_number}: COD_GEO no es válido")
        if is_target_region and geographic_code in geographic_codes:
            raise NgmepGeographyError(
                f"Fila {row_number}: COD_GEO duplicado {geographic_code}"
            )
        if is_target_region:
            geographic_codes.add(geographic_code)

        ine_code = source_code[:5]
        capital_code = values["COD_INE_CAPITAL"]
        if (
            not INE_11_PATTERN.fullmatch(capital_code)
            or capital_code[:5] != ine_code
        ):
            raise NgmepGeographyError(
                f"Fila {row_number}: COD_INE_CAPITAL no pertenece al municipio"
            )
        if is_target_region and capital_code in capital_codes:
            raise NgmepGeographyError(
                f"Fila {row_number}: COD_INE_CAPITAL duplicado {capital_code}"
            )
        if is_target_region:
            capital_codes.add(capital_code)

        source_population = _non_negative_integer(
            values["POBLACION_MUNI"],
            "POBLACION_MUNI",
            row_number,
        )
        capital_population = _non_negative_integer(
            values["POBLACION_CAPITAL"],
            "POBLACION_CAPITAL",
            row_number,
        )
        if capital_population > source_population:
            raise NgmepGeographyError(
                f"Fila {row_number}: la población de la capital supera la municipal"
            )

        surface_hectares = _decimal_value(
            values["SUPERFICIE"],
            "SUPERFICIE",
            row_number,
        )
        perimeter_m = _decimal_value(
            values["PERIMETRO"],
            "PERIMETRO",
            row_number,
        )
        if surface_hectares <= 0 or perimeter_m <= 0:
            raise NgmepGeographyError(
                f"Fila {row_number}: superficie y perímetro deben ser positivos"
            )
        surface_km2 = (surface_hectares / Decimal(100)).quantize(
            Decimal("0.000001")
        )
        perimeter_m = perimeter_m.quantize(Decimal("0.001"))

        longitude = _decimal_value(
            values["LONGITUD_ETRS89_REGCAN95"],
            "LONGITUD_ETRS89_REGCAN95",
            row_number,
        ).quantize(Decimal("0.000000001"))
        latitude = _decimal_value(
            values["LATITUD_ETRS89_REGCAN95"],
            "LATITUD_ETRS89_REGCAN95",
            row_number,
        ).quantize(Decimal("0.000000001"))
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise NgmepGeographyError(
                f"Fila {row_number}: coordenadas fuera de rango"
            )
        altitude_m = _decimal_value(
            values["ALTITUD"],
            "ALTITUD",
            row_number,
        ).quantize(Decimal("0.01"))
        if not (-500 <= altitude_m <= 5000):
            raise NgmepGeographyError(f"Fila {row_number}: altitud fuera de rango")

        if not is_target_region:
            continue
        expected_province = PROVINCES[province_code]
        if values["PROVINCIA"] != expected_province:
            raise NgmepGeographyError(
                f"Fila {row_number}: provincia inesperada para {province_code}"
            )
        if not (
            CASTILLA_Y_LEON_LONGITUDE[0]
            <= longitude
            <= CASTILLA_Y_LEON_LONGITUDE[1]
            and CASTILLA_Y_LEON_LATITUDE[0]
            <= latitude
            <= CASTILLA_Y_LEON_LATITUDE[1]
        ):
            raise NgmepGeographyError(
                f"Fila {row_number}: coordenadas fuera de Castilla y León"
            )
        if ine_code in records:
            raise NgmepGeographyError(
                f"Fila {row_number}: código municipal duplicado {ine_code}"
            )

        province_counts[province_code] += 1
        records[ine_code] = NgmepRecord(
            ine_code=ine_code,
            source_municipality_code=source_code,
            relationship_id=relationship_id,
            geographic_code=geographic_code,
            province_code=province_code,
            province_name=values["PROVINCIA"],
            municipality_name=values["NOMBRE_ACTUAL"],
            source_population=source_population,
            surface_km2=surface_km2,
            perimeter_m=perimeter_m,
            capital_ine_code=capital_code,
            capital_name=values["CAPITAL"],
            capital_population=capital_population,
            mtn25_sheet=values["HOJA_MTN25"],
            longitude=longitude,
            latitude=latitude,
            coordinate_origin=values["ORIGENCOOR"],
            altitude_m=altitude_m,
            altitude_origin=values["ORIGENALTITUD"],
        )

    if national_rows != spec.expected_rows:
        raise NgmepGeographyError(
            "Número inesperado de municipios en el CSV del NGMEP: "
            f"esperado {spec.expected_rows}, recibido {national_rows}"
        )
    if len(records) != spec.expected_region_rows:
        raise NgmepGeographyError(
            "Número inesperado de municipios de Castilla y León: "
            f"esperado {spec.expected_region_rows}, recibido {len(records)}"
        )
    expected_counts = dict(spec.expected_province_counts)
    if dict(province_counts) != expected_counts:
        raise NgmepGeographyError(
            "Recuento provincial inesperado en el CSV del NGMEP: "
            f"esperado {expected_counts}, recibido {dict(province_counts)}"
        )
    return records, dict(province_counts)


def load_ngmep_geography(
    archive_bytes: bytes,
    spec: NgmepSourceSpec,
    *,
    retrieved_at: datetime | None = None,
) -> tuple[dict[str, NgmepRecord], NgmepProvenance, dict[str, int]]:
    content, archive_sha256, content_sha256 = extract_municipalities_csv(
        archive_bytes,
        spec,
    )
    records, province_counts = parse_municipalities_csv(content, spec)
    retrieved = retrieved_at or datetime.now(timezone.utc)
    if retrieved.tzinfo is None:
        raise NgmepGeographyError("La fecha de descarga debe incluir zona horaria")
    provenance = NgmepProvenance(
        dataset_key=DATASET_KEY,
        title=spec.title,
        version_label=spec.version_label,
        reference_date=spec.reference_date,
        catalog_url=spec.catalog_url,
        download_url=spec.download_url,
        member_name=spec.member_name,
        archive_sha256=archive_sha256,
        content_sha256=content_sha256,
        license_name=spec.license_name,
        license_url=spec.license_url,
        attribution=spec.attribution,
        retrieved_at=retrieved,
        national_row_count=spec.expected_rows,
        target_row_count=len(records),
    )
    return records, provenance, province_counts


def build_ngmep_sync_plan(
    db: Session,
    records: dict[str, NgmepRecord],
    provenance: NgmepProvenance,
    province_counts: dict[str, int],
    *,
    overwrite_existing: bool = False,
) -> NgmepSyncPlan:
    register_all_models()
    municipalities = list(
        db.scalars(
            select(Municipality)
            .where(Municipality.ine_code.in_(records))
            .order_by(Municipality.id)
        ).all()
    )
    database_by_code = {
        municipality.ine_code: municipality
        for municipality in municipalities
        if municipality.ine_code is not None
    }
    plan = NgmepSyncPlan(
        provenance=provenance,
        source_by_province=province_counts,
        database_rows=len(municipalities),
    )

    dataset_versions = list(
        db.scalars(
            select(ReferenceDatasetVersion).where(
                ReferenceDatasetVersion.dataset_key == provenance.dataset_key
            )
        ).all()
    )
    for version in dataset_versions:
        if version.content_sha256 == provenance.content_sha256:
            plan.dataset_version = version
            if not _dataset_version_matches(version, provenance):
                plan.source_conflicts.append(
                    GeographyIssue(
                        reason="inconsistent_dataset_metadata",
                        detail=(
                            "La versión registrada con el mismo hash tiene "
                            "metadatos distintos"
                        ),
                    )
                )
            break
    if any(
        version.reference_date == provenance.reference_date
        and version.content_sha256 != provenance.content_sha256
        for version in dataset_versions
    ):
        plan.source_conflicts.append(
            GeographyIssue(
                reason="different_source_revision",
                detail="Ya existe otro artefacto para la misma fecha de referencia",
            )
        )

    current_snapshots = {
        snapshot.municipality_id: snapshot
        for snapshot in db.scalars(
            select(MunicipalityGeographySnapshot).where(
                MunicipalityGeographySnapshot.is_current.is_(True),
                MunicipalityGeographySnapshot.municipality_id.in_(
                    [municipality.id for municipality in municipalities]
                ),
            )
        ).all()
    }

    for ine_code, record in records.items():
        municipality = database_by_code.get(ine_code)
        if municipality is None:
            plan.missing_municipalities.append(
                GeographyIssue(
                    reason="missing_municipality",
                    detail="El código NGMEP no existe en el catálogo municipal",
                    ine_code=ine_code,
                    municipality_name=record.municipality_name,
                )
            )
            continue
        plan.matched_rows += 1

        if (
            _normalized_name(municipality.province)
            != _normalized_name(record.province_name)
            or _normalized_name(municipality.autonomous_community)
            != _normalized_name(CASTILLA_Y_LEON_NAME)
        ):
            plan.database_conflicts.append(
                _issue(
                    municipality,
                    record,
                    "territorial_mismatch",
                    "Provincia o comunidad autónoma incoherente con NGMEP",
                )
            )
            continue
        if _normalized_name(municipality.name) != _normalized_name(
            record.municipality_name
        ):
            plan.name_mismatches.append(
                _issue(
                    municipality,
                    record,
                    "name_mismatch",
                    f"NGMEP: {record.municipality_name}",
                )
            )
        if (
            municipality.population is not None
            and municipality.population != record.source_population
        ):
            plan.population_mismatches.append(
                _issue(
                    municipality,
                    record,
                    "population_mismatch",
                    (
                        f"INE: {municipality.population}; "
                        f"NGMEP: {record.source_population}"
                    ),
                )
            )

        current = current_snapshots.get(municipality.id)
        if current is None:
            plan.updates.append(
                GeographyUpdate(
                    municipality=municipality,
                    record=record,
                    current_snapshot=None,
                )
            )
            continue

        current_source = current.dataset_version
        if current_source.reference_date > provenance.reference_date:
            plan.database_conflicts.append(
                _issue(
                    municipality,
                    record,
                    "newer_geography",
                    (
                        "La geografía vigente corresponde a "
                        f"{current_source.reference_date.isoformat()}"
                    ),
                )
            )
            continue
        if current_source.reference_date == provenance.reference_date and (
            current_source.content_sha256 != provenance.content_sha256
        ):
            plan.database_conflicts.append(
                _issue(
                    municipality,
                    record,
                    "different_source_revision",
                    "La misma fecha vigente procede de otro fichero",
                )
            )
            continue

        desired_density = _density(
            municipality.population,
            float(record.surface_km2),
        ) if municipality.population is not None else None
        snapshot_matches = _snapshot_matches(current, record, provenance)
        projection_matches = (
            _optional_float_matches(
                municipality.surface_km2,
                float(record.surface_km2),
            )
            and _optional_float_matches(municipality.density, desired_density)
        )
        if snapshot_matches and projection_matches:
            plan.unchanged_rows += 1
            continue

        same_source = (
            current_source.content_sha256 == provenance.content_sha256
        )
        if same_source and not overwrite_existing:
            plan.database_conflicts.append(
                _issue(
                    municipality,
                    record,
                    "inconsistent_official_geography",
                    "Los datos guardados no coinciden con su fuente declarada",
                )
            )
            continue
        plan.updates.append(
            GeographyUpdate(
                municipality=municipality,
                record=record,
                current_snapshot=current,
                replace_existing=same_source,
            )
        )

    return plan


def apply_ngmep_sync(db: Session, plan: NgmepSyncPlan) -> None:
    if plan.blocking_issues:
        raise NgmepGeographyError(
            "La sincronización contiene conflictos; revisa el dry-run antes de aplicar"
        )
    try:
        dataset_version = plan.dataset_version
        if dataset_version is None:
            dataset_version = ReferenceDatasetVersion(
                **_dataset_values(plan.provenance)
            )
            db.add(dataset_version)
            db.flush()

        for update in plan.updates:
            snapshot = update.current_snapshot
            if update.replace_existing:
                if snapshot is None:
                    raise NgmepGeographyError(
                        "El plan de sustitución geográfica no es válido"
                    )
                for field_name, value in _snapshot_values(update.record).items():
                    setattr(snapshot, field_name, value)
                snapshot.dataset_version = dataset_version
                snapshot.is_current = True
            else:
                if snapshot is not None:
                    snapshot.is_current = False
                db.add(
                    MunicipalityGeographySnapshot(
                        municipality=update.municipality,
                        dataset_version=dataset_version,
                        is_current=True,
                        **_snapshot_values(update.record),
                    )
                )

            surface_km2 = float(update.record.surface_km2)
            update.municipality.surface_km2 = surface_km2
            update.municipality.density = (
                _density(update.municipality.population, surface_km2)
                if update.municipality.population is not None
                else None
            )
        db.commit()
        for update in plan.updates:
            db.expire(update.municipality, ["official_geography"])
    except Exception:
        db.rollback()
        raise


def _dataset_values(provenance: NgmepProvenance) -> dict[str, object]:
    return {
        "dataset_key": provenance.dataset_key,
        "title": provenance.title,
        "version_label": provenance.version_label,
        "reference_date": provenance.reference_date,
        "catalog_url": provenance.catalog_url,
        "download_url": provenance.download_url,
        "member_name": provenance.member_name,
        "archive_sha256": provenance.archive_sha256,
        "content_sha256": provenance.content_sha256,
        "license_name": provenance.license_name,
        "license_url": provenance.license_url,
        "attribution": provenance.attribution,
        "retrieved_at": provenance.retrieved_at,
        "national_row_count": provenance.national_row_count,
        "target_row_count": provenance.target_row_count,
    }


def _dataset_version_matches(
    version: ReferenceDatasetVersion,
    provenance: NgmepProvenance,
) -> bool:
    return all(
        getattr(version, field_name) == value
        for field_name, value in _dataset_values(provenance).items()
        if field_name != "retrieved_at"
    )


def _snapshot_values(record: NgmepRecord) -> dict[str, object]:
    return {
        "source_municipality_code": record.source_municipality_code,
        "relationship_id": record.relationship_id,
        "geographic_code": record.geographic_code,
        "source_province_code": record.province_code,
        "source_province_name": record.province_name,
        "source_municipality_name": record.municipality_name,
        "source_population": record.source_population,
        "surface_km2": record.surface_km2,
        "perimeter_m": record.perimeter_m,
        "capital_ine_code": record.capital_ine_code,
        "capital_name": record.capital_name,
        "capital_population": record.capital_population,
        "mtn25_sheet": record.mtn25_sheet,
        "longitude": record.longitude,
        "latitude": record.latitude,
        "coordinate_origin": record.coordinate_origin,
        "altitude_m": record.altitude_m,
        "altitude_origin": record.altitude_origin,
        "crs": CRS,
    }


def _snapshot_matches(
    snapshot: MunicipalityGeographySnapshot,
    record: NgmepRecord,
    provenance: NgmepProvenance,
) -> bool:
    return (
        snapshot.dataset_version.content_sha256 == provenance.content_sha256
        and snapshot.is_current
        and all(
            getattr(snapshot, field_name) == value
            for field_name, value in _snapshot_values(record).items()
        )
    )


def _optional_float_matches(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _issue(
    municipality: Municipality,
    record: NgmepRecord,
    reason: str,
    detail: str,
) -> GeographyIssue:
    return GeographyIssue(
        reason=reason,
        detail=detail,
        ine_code=record.ine_code,
        municipality_name=municipality.name,
        municipality_id=municipality.id,
    )


def _issue_summary(issues: list[GeographyIssue]) -> dict[str, object]:
    return {
        "count": len(issues),
        "items": [
            asdict(issue) for issue in issues[:MAX_ISSUES_IN_SUMMARY]
        ],
        "truncated": len(issues) > MAX_ISSUES_IN_SUMMARY,
    }


def _required_text(value: object, field_name: str, row_number: int) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} está vacío"
        )
    if len(text_value) > 255:
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} supera la longitud permitida"
        )
    return text_value


def _non_negative_integer(
    value: str,
    field_name: str,
    row_number: int,
) -> int:
    if not value.isdigit():
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} no es un entero"
        )
    parsed = int(value)
    if parsed < 0:
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} es negativo"
        )
    return parsed


def _decimal_value(
    value: str,
    field_name: str,
    row_number: int,
) -> Decimal:
    if not DECIMAL_PATTERN.fullmatch(value):
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} no es un decimal válido"
        )
    try:
        parsed = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} no es un decimal válido"
        ) from exc
    if not parsed.is_finite():
        raise NgmepGeographyError(
            f"Fila {row_number}: {field_name} no es finito"
        )
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sincroniza geografía municipal oficial del NGMEP para "
            "Castilla y León"
        ),
    )
    parser.add_argument(
        "--year",
        type=int,
        choices=sorted(NGMEP_SOURCE_SPECS),
        default=max(NGMEP_SOURCE_SPECS),
        help="Anualidad oficial del NGMEP",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="ZIP local del NGMEP; por defecto se descarga del IGN",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Aplica el plan; sin esta opción siempre se ejecuta en dry-run",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Restaura valores alterados de la misma fuente oficial",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    spec = NGMEP_SOURCE_SPECS[args.year]
    try:
        archive_bytes = (
            read_local_ngmep_archive(args.archive)
            if args.archive is not None
            else download_ngmep_archive(spec)
        )
        records, provenance, province_counts = load_ngmep_geography(
            archive_bytes,
            spec,
        )
        with SessionLocal() as db:
            plan = build_ngmep_sync_plan(
                db,
                records,
                provenance,
                province_counts,
                overwrite_existing=args.overwrite_existing,
            )
            if args.apply and not plan.blocking_issues:
                apply_ngmep_sync(db, plan)
            applied = args.apply and not plan.blocking_issues
            summary = plan.summary(applied=applied)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 3 if plan.blocking_issues else 0
    except NgmepGeographyError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except SQLAlchemyError:
        print(
            json.dumps(
                {"ok": False, "error": "No se pudo completar la transacción"},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
