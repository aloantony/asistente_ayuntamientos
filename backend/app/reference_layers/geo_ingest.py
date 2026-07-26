"""Normalize acquired geodata into immutable local delivery assets."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import stat
import sqlite3
import subprocess
import tempfile
import time
from typing import Any
import warnings
import zipfile

from PIL import Image, UnidentifiedImageError

from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    StoredReferenceBlob,
)
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.local_tile_archive import (
    InvalidLocalTileArchiveError,
    validate_tile_image,
)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$", re.ASCII)
_VERSIONED_VECTOR_TABLE_RE = re.compile(
    r"^m_l[0-9a-z]+_r[0-9a-z]+_v_[0-9a-f]{24}$",
    re.ASCII,
)
_STAGING_VECTOR_TABLE_RE = re.compile(
    r"^s_v_(?P<identity>[0-9a-f]{24})_[0-9a-f]{16}$",
    re.ASCII,
)
_CONNECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$", re.ASCII)
_LAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_EPSG_WKT_RE = re.compile(
    r'(?:ID\["EPSG",\s*|AUTHORITY\["EPSG",\s*")(?P<code>[1-9][0-9]{2,6})',
    re.ASCII,
)
_ALLOWED_BINARIES = {
    "gdalinfo": "/usr/bin/gdalinfo",
    "gdal_translate": "/usr/bin/gdal_translate",
    "ogr2ogr": "/usr/bin/ogr2ogr",
}
_ALLOWED_VECTOR_DRIVERS = {
    "GeoJSON": ".geojson",
    "FlatGeobuf": ".fgb",
    "GMLZIP": ".zip",
}
_VECTOR_DATA_SCHEMA = "reference_data"
_VECTOR_STAGING_SCHEMA = "reference_data_staging"
_VECTOR_GEOMETRY_EVIDENCE_SCHEMA = "reference-vector-geometry-evidence/v1"
_VECTOR_TABLE_ATTESTATION_SCHEMA = "reference-vector-table-attestation/v1"
_VECTOR_NORMALIZATION_ALGORITHM = (
    "postgis-st-makevalid-collectionextract-promote-to-multi/v1"
)
_VECTOR_DRIVER_ALIASES = {
    name.casefold(): (name, suffix)
    for name, suffix in _ALLOWED_VECTOR_DRIVERS.items()
}
_RASTER_DRIVER_ALIASES = {
    "gtiff": ("GTiff", ".tif"),
    "geotiff": ("GTiff", ".tif"),
    "gtiffzip": ("GTiffZIP", ".zip"),
    "geotiffzip": ("GTiffZIP", ".zip"),
}
_RASTER_SAMPLE_BYTES = {
    "Byte": 1,
    "Int8": 1,
    "UInt16": 2,
    "Int16": 2,
    "UInt32": 4,
    "Int32": 4,
    "Float32": 4,
    "Float64": 8,
    "CInt16": 4,
    "CInt32": 8,
    "CFloat32": 8,
    "CFloat64": 16,
}
_STRICT_GDAL_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    # Artifacts are standalone snapshots. Never discover sibling files,
    # auxiliary metadata, plugins, remote VSI objects, or PROJ network grids.
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_PAM_ENABLED": "NO",
    "GDAL_DRIVER_PATH": "disable",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".reference-network-disabled",
    "GDAL_HTTP_TIMEOUT": "1",
    "GDAL_HTTP_MAX_RETRY": "0",
    "PROJ_NETWORK": "OFF",
    "VSI_CACHE": "FALSE",
    "GDAL_CACHEMAX": "256",
}
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_VECTOR_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
MAX_RASTER_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_RASTER_PIXELS = 250_000_000
MAX_RASTER_PIXELS = 2_000_000_000
MAX_RASTER_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_RASTER_BANDS = 16
DEFAULT_MAX_RASTER_OUTPUT_BYTES = 10 * 1024 * 1024 * 1024
MAX_TILE_COUNT = 10_000_000
MAX_VECTOR_ARTIFACTS = 10_000


class GeoIngestError(RuntimeError):
    """Local geodata could not be normalized or validated safely."""


@dataclass(frozen=True)
class GeoCommandResult:
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class GeoDatabaseTarget:
    host: str
    port: int
    database: str
    username: str
    password: str | None

    @classmethod
    def from_url(cls, database_url: str) -> "GeoDatabaseTarget":
        try:
            value = make_url(database_url)
        except Exception as error:
            raise GeoIngestError("geodata database URL is invalid") from error
        if not value.drivername.startswith("postgresql"):
            raise GeoIngestError("geodata database must be PostgreSQL")
        host = value.host or ""
        database = value.database or ""
        username = value.username or ""
        port = value.port or 5432
        if (
            _CONNECTION_NAME_RE.fullmatch(host) is None
            or _CONNECTION_NAME_RE.fullmatch(database) is None
            or _CONNECTION_NAME_RE.fullmatch(username) is None
            or not 1 <= port <= 65535
            or value.query
            or (value.password is not None and "\x00" in value.password)
        ):
            raise GeoIngestError("geodata database URL contains unsafe fields")
        return cls(host, port, database, username, value.password)

    @property
    def ogr_connection(self) -> str:
        return (
            f"PG:host={self.host} port={self.port} "
            f"dbname={self.database} user={self.username}"
        )

    def command_environment(self) -> dict[str, str]:
        result = _strict_gdal_environment()
        result["PGCONNECT_TIMEOUT"] = "10"
        if self.password is not None:
            result["PGPASSWORD"] = self.password
        return result


@dataclass(frozen=True)
class VectorIngestResult:
    table_name: str
    storage_key: str
    content_sha256: str
    feature_count: int
    crs: str
    bounds_json: dict[str, float]
    geometry_type: str
    validation_json: dict[str, Any]


@dataclass(frozen=True)
class VectorArtifactInput:
    source_path: Path
    input_sha256: str
    input_layer: str | None = None


@dataclass(frozen=True)
class RasterInspection:
    width: int
    height: int
    band_count: int
    band_types: tuple[str, ...]
    nodata_values: tuple[float | None, ...]
    pixel_size_x: float
    pixel_size_y: float
    overview_sizes: tuple[tuple[int, int], ...]
    crs: str
    bounds_json: dict[str, float]
    driver: str
    is_cog: bool
    uncompressed_bytes: int


@dataclass(frozen=True)
class RasterIngestResult:
    blob: StoredReferenceBlob
    inspection: RasterInspection
    validation_json: dict[str, Any]


@dataclass(frozen=True)
class TileArchiveInspection:
    tile_count: int
    min_zoom: int
    max_zoom: int
    image_format: str
    coordinate_sha256: str
    bounds_json: dict[str, float]
    validation_json: dict[str, Any]


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], int],
    GeoCommandResult,
]


@dataclass(frozen=True)
class _ArtifactSnapshot:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class _VectorGeometryStats:
    feature_count: int
    source_fid_count: int
    max_source_fid: int | None
    null_geometry_count: int
    empty_geometry_count: int
    invalid_geometry_count: int
    valid_geometry_count: int
    multi_geometry_count: int
    multiple_part_geometry_count: int
    geometry_part_count: int
    srid_count: int
    srid: int | None
    geometry_types: tuple[str, ...]
    extent: str | None

    def counters(self) -> dict[str, int]:
        return {
            "feature_count": self.feature_count,
            "null_geometry_count": self.null_geometry_count,
            "empty_geometry_count": self.empty_geometry_count,
            "invalid_geometry_count": self.invalid_geometry_count,
            "valid_geometry_count": self.valid_geometry_count,
            "multi_geometry_count": self.multi_geometry_count,
            "multiple_part_geometry_count": self.multiple_part_geometry_count,
            "geometry_part_count": self.geometry_part_count,
        }


def _strict_gdal_environment(*, cpl_tmpdir: Path | None = None) -> dict[str, str]:
    result = dict(_STRICT_GDAL_ENVIRONMENT)
    if cpl_tmpdir is not None:
        result["CPL_TMPDIR"] = str(cpl_tmpdir)
    return result


def _validated_command_environment(environment: Mapping[str, str]) -> dict[str, str]:
    allowed = {*_STRICT_GDAL_ENVIRONMENT, "CPL_TMPDIR", "PGCONNECT_TIMEOUT", "PGPASSWORD"}
    result = dict(environment)
    if set(result) - allowed or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\x00" in value
        for key, value in result.items()
    ):
        raise GeoIngestError("geodata command environment is invalid")
    if any(result.get(key) != value for key, value in _STRICT_GDAL_ENVIRONMENT.items()):
        raise GeoIngestError("geodata command security environment is incomplete")
    if result.get("PGCONNECT_TIMEOUT") not in {None, "10"}:
        raise GeoIngestError("geodata database timeout is invalid")
    password = result.get("PGPASSWORD")
    if password is not None and len(password) > 4096:
        raise GeoIngestError("geodata database credential is invalid")
    temporary = result.get("CPL_TMPDIR")
    if temporary is not None:
        try:
            temporary_path = Path(temporary)
            metadata = temporary_path.lstat()
        except (OSError, ValueError) as error:
            raise GeoIngestError("geodata temporary directory is invalid") from error
        if (
            not temporary_path.is_absolute()
            or temporary_path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise GeoIngestError("geodata temporary directory is invalid")
    return result


def run_geo_command(
    argv: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> GeoCommandResult:
    """Execute one allowlisted GDAL command without a shell."""

    if not argv or argv[0] not in _ALLOWED_BINARIES.values():
        raise GeoIngestError("geodata command is not allowlisted")
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 86_400:
        raise GeoIngestError("geodata command timeout is invalid")
    if any(
        not isinstance(item, str)
        or "\x00" in item
        or "\n" in item
        or "\r" in item
        for item in argv
    ):
        raise GeoIngestError("geodata command argument is invalid")
    child_environment = _validated_command_environment(environment)
    stdout, stderr, returncode = _run_bounded_process(
        argv,
        environment=child_environment,
        timeout_seconds=timeout_seconds,
    )
    if returncode != 0:
        raise GeoIngestError("geodata command rejected the local artifact")
    return GeoCommandResult(stdout, stderr)


def _run_bounded_process(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> tuple[bytes, bytes, int]:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment),
        )
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            raise OSError("geodata command pipes are unavailable")
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
            for key, _ in selector.select(min(remaining, 0.5)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = buffers[key.data]
                target.extend(chunk)
                if len(target) > MAX_COMMAND_OUTPUT_BYTES:
                    raise GeoIngestError("geodata command output exceeded its limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        returncode = process.wait(timeout=remaining)
    except GeoIngestError:
        _terminate_process(process)
        raise
    except (OSError, subprocess.TimeoutExpired) as error:
        _terminate_process(process)
        raise GeoIngestError("geodata command could not complete") from error
    finally:
        selector.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), returncode


def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def versioned_vector_table_name(
    *,
    provider_key: str,
    layer_id: int,
    run_id: int,
    input_sha256: str,
) -> str:
    if (
        not isinstance(provider_key, str)
        or not provider_key
        or len(provider_key) > 64
        or isinstance(layer_id, bool)
        or not isinstance(layer_id, int)
        or layer_id < 1
        or isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id < 1
        or _SHA256_RE.fullmatch(input_sha256) is None
    ):
        raise GeoIngestError("vector table identity is invalid")
    version_digest = canonical_json_sha256(
        {
            "schema_version": "reference-vector-table-name-v1",
            "provider_key": provider_key,
            "layer_id": layer_id,
            "run_id": run_id,
            "input_sha256": input_sha256,
        }
    )
    value = (
        f"m_l{_base36(layer_id)}_r{_base36(run_id)}"
        f"_v_{version_digest[:24]}"
    )
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise GeoIngestError("vector table identity is too large")
    return value


def ingest_vector_artifact(
    db: Session,
    *,
    database: GeoDatabaseTarget,
    source_path: Path,
    input_sha256: str,
    provider_key: str,
    layer_id: int,
    run_id: int,
    input_layer: str | None = None,
    input_driver: str | None = None,
    minimum_features: int = 1,
    max_source_bytes: int = MAX_VECTOR_SOURCE_BYTES,
    timeout_seconds: int = 3600,
    runner: CommandRunner = run_geo_command,
) -> VectorIngestResult:
    """Import one immutable vector snapshot into a versioned PostGIS table."""

    return ingest_vector_artifacts(
        db,
        database=database,
        artifacts=(VectorArtifactInput(source_path, input_sha256, input_layer),),
        provider_key=provider_key,
        layer_id=layer_id,
        run_id=run_id,
        input_driver=input_driver,
        minimum_features=minimum_features,
        max_source_bytes=max_source_bytes,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )


def ingest_vector_artifacts(
    db: Session,
    *,
    database: GeoDatabaseTarget,
    artifacts: Sequence[
        VectorArtifactInput | tuple[Path, str] | tuple[Path, str, str | None]
    ],
    provider_key: str,
    layer_id: int,
    run_id: int,
    input_driver: str | None = None,
    minimum_features: int = 1,
    max_source_bytes: int = MAX_VECTOR_SOURCE_BYTES,
    timeout_seconds: int = 3600,
    runner: CommandRunner = run_geo_command,
) -> VectorIngestResult:
    """Import ordered vector pages with durable pre/post geometry evidence."""

    if (
        isinstance(minimum_features, bool)
        or not isinstance(minimum_features, int)
        or not 0 <= minimum_features <= 1_000_000_000
    ):
        raise GeoIngestError("minimum vector feature count is invalid")
    _bounded_positive_integer(
        max_source_bytes,
        maximum=MAX_VECTOR_SOURCE_BYTES,
        message="vector source byte limit is invalid",
    )
    normalized_inputs = _vector_artifact_inputs(artifacts)
    with tempfile.TemporaryDirectory(prefix="reference-vector-") as directory:
        private_directory = Path(directory)
        snapshots: list[tuple[_ArtifactSnapshot, str, str | None]] = []
        manifest: list[dict[str, Any]] = []
        remaining_bytes = max_source_bytes
        for ordinal, artifact in enumerate(normalized_inputs):
            driver, snapshot_suffix = _vector_driver(
                input_driver,
                artifact.source_path,
            )
            if artifact.input_layer is not None and (
                _LAYER_NAME_RE.fullmatch(artifact.input_layer) is None
                or artifact.input_layer.startswith("-")
            ):
                raise GeoIngestError("input vector layer name is invalid")
            if remaining_bytes < 1:
                raise GeoIngestError("vector artifacts exceed their aggregate byte limit")
            snapshot = _snapshot_artifact(
                artifact.source_path,
                destination=(
                    private_directory / f"source-{ordinal:06d}{snapshot_suffix}"
                ),
                max_bytes=remaining_bytes,
                expected_sha256=artifact.input_sha256,
            )
            remaining_bytes -= snapshot.size_bytes
            _validate_vector_signature(snapshot.path, driver)
            snapshots.append((snapshot, driver, artifact.input_layer))
            manifest.append(
                {
                    "ordinal": ordinal,
                    "input_sha256": snapshot.sha256,
                    "input_driver": driver,
                    "input_layer": artifact.input_layer,
                    "size_bytes": snapshot.size_bytes,
                }
            )
        manifest_sha256 = canonical_json_sha256(
            {
                "schema_version": "reference-vector-input-manifest-v1",
                "artifacts": manifest,
            }
        )
        identity_sha256 = manifest_sha256
        table_name = versioned_vector_table_name(
            provider_key=provider_key,
            layer_id=layer_id,
            run_id=run_id,
            input_sha256=identity_sha256,
        )
        storage_key = f"{_VECTOR_DATA_SCHEMA}.{table_name}"
        staging_table_name = _vector_staging_table_name(table_name)
        staging_storage_key = (
            f"{_VECTOR_STAGING_SCHEMA}.{staging_table_name}"
        )
        db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_VECTOR_DATA_SCHEMA}"))
        db.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {_VECTOR_STAGING_SCHEMA}")
        )
        db.execute(
            text(
                f"REVOKE ALL ON SCHEMA {_VECTOR_STAGING_SCHEMA} FROM PUBLIC"
            )
        )
        db.commit()
        discard_unpublished_final = False
        try:
            if not _try_vector_ingest_lock(db, table_name):
                raise GeoIngestError(
                    "an identical vector ingest is already in progress"
                )
            _cleanup_abandoned_vector_staging_tables(
                db,
            )
            table_exists = _table_exists(
                db,
                schema_name=_VECTOR_DATA_SCHEMA,
                table_name=table_name,
            )
            if table_exists and _table_has_immutable_guards(db, table_name):
                result = _recover_vector_ingest_result(
                    db,
                    table_name=table_name,
                    storage_key=storage_key,
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                    minimum_features=minimum_features,
                )
                db.commit()
                return result
            if table_exists:
                discard_unpublished_final = True
                _drop_vector_table_in_transaction(
                    db,
                    schema_name=_VECTOR_DATA_SCHEMA,
                    table_name=table_name,
                )

            environment = database.command_environment()
            environment["CPL_TMPDIR"] = str(private_directory)
            artifact_geometry_evidence: list[dict[str, Any]] = []
            previous_max_source_fid: int | None = None
            for ordinal, (snapshot, driver, input_layer) in enumerate(snapshots):
                argv = _vector_import_command(
                    database=database,
                    storage_key=staging_storage_key,
                    snapshot=snapshot.path,
                    driver=driver,
                    input_layer=input_layer,
                    append=ordinal > 0,
                )
                runner(argv, environment, timeout_seconds)
                if not _table_exists(
                    db,
                    schema_name=_VECTOR_STAGING_SCHEMA,
                    table_name=staging_table_name,
                ):
                    raise GeoIngestError(
                        "vector importer did not create its staging table"
                    )
                artifact_stats = _vector_geometry_stats(
                    db,
                    schema_name=_VECTOR_STAGING_SCHEMA,
                    table_name=staging_table_name,
                    source_fid_after=previous_max_source_fid,
                )
                if artifact_stats.source_fid_count != (
                    artifact_stats.feature_count
                ):
                    raise GeoIngestError(
                        "vector artifact feature identifiers are not unique"
                    )
                if artifact_stats.feature_count > 0:
                    if (
                        artifact_stats.max_source_fid is None
                        or (
                            previous_max_source_fid is not None
                            and artifact_stats.max_source_fid
                            <= previous_max_source_fid
                        )
                    ):
                        raise GeoIngestError(
                            "vector append did not allocate increasing "
                            "feature identifiers"
                        )
                    previous_max_source_fid = (
                        artifact_stats.max_source_fid
                    )
                artifact_geometry_evidence.append(
                    {
                        "ordinal": ordinal,
                        "input_sha256": snapshot.sha256,
                        **artifact_stats.counters(),
                    }
                )

            source_stats = _vector_geometry_stats(
                db,
                schema_name=_VECTOR_STAGING_SCHEMA,
                table_name=staging_table_name,
            )
            _validate_source_vector_stats(source_stats)
            geometry_family, target_geometry_type, collection_dimension = (
                _normalization_geometry_family(source_stats.geometry_types)
            )
            repair_counts = _vector_repair_counts(
                db,
                staging_table_name=staging_table_name,
                collection_dimension=collection_dimension,
                target_geometry_type=target_geometry_type,
                source_stats=source_stats,
            )
            _create_normalized_vector_table(
                db,
                staging_table_name=staging_table_name,
                table_name=table_name,
                collection_dimension=collection_dimension,
                target_geometry_type=target_geometry_type,
            )
            discard_unpublished_final = True
            result_stats = _vector_geometry_stats(
                db,
                schema_name=_VECTOR_DATA_SCHEMA,
                table_name=table_name,
                include_extent=True,
            )
            feature_count = result_stats.feature_count
            if feature_count < minimum_features:
                raise GeoIngestError("vector feature count is below its minimum")
            _validate_result_vector_stats(
                result_stats,
                expected_geometry_type=target_geometry_type,
                expected_feature_count=repair_counts["result_feature_count"],
            )
            bounds = _postgis_extent(result_stats.extent)
            data_schema = _vector_table_schema(db, table_name)
            data_schema_sha256 = canonical_json_sha256(data_schema)
            source_totals = {
                **source_stats.counters(),
                "source_fid_count": source_stats.source_fid_count,
                "max_source_fid": source_stats.max_source_fid,
                "srid_count": source_stats.srid_count,
                "srid": source_stats.srid,
                "geometry_types": list(source_stats.geometry_types),
            }
            if _sum_vector_artifact_evidence(
                artifact_geometry_evidence
            ) != source_stats.counters():
                raise GeoIngestError(
                    "per-artifact geometry evidence is inconsistent"
                )
            geometry_evidence = {
                "schema_version": _VECTOR_GEOMETRY_EVIDENCE_SCHEMA,
                "input_manifest_sha256": manifest_sha256,
                "source": {
                    "artifact_count": len(manifest),
                    "artifacts": artifact_geometry_evidence,
                    "total": source_totals,
                },
                "normalization": {
                    "algorithm": _VECTOR_NORMALIZATION_ALGORITHM,
                    "target_crs": "EPSG:3857",
                    "geometry_family": geometry_family,
                    "target_geometry_type": target_geometry_type,
                    **repair_counts,
                },
                "result": {
                    **result_stats.counters(),
                    "source_fid_count": result_stats.source_fid_count,
                    "max_source_fid": result_stats.max_source_fid,
                    "srid_count": result_stats.srid_count,
                    "srid": result_stats.srid,
                    "crs": "EPSG:3857",
                    "geometry_family": geometry_family,
                    "geometry_type": target_geometry_type,
                    "bounds": bounds,
                    "data_schema_sha256": data_schema_sha256,
                },
            }
            geometry_evidence_sha256 = canonical_json_sha256(
                geometry_evidence
            )
            transform_identity = _vector_transform_identity(
                manifest=manifest,
                storage_key=storage_key,
                geometry_evidence_sha256=geometry_evidence_sha256,
                feature_count=feature_count,
                geometry_type=target_geometry_type,
                data_schema=data_schema,
                bounds=bounds,
            )
            content_sha256 = canonical_json_sha256(transform_identity)
            validation = {
                "schema_version": "reference-delivery-validation/v1",
                "passed": True,
                "kind": "vector",
                "checks": {
                    "input_manifest": manifest,
                    "input_manifest_sha256": manifest_sha256,
                    "table_identity_sha256": identity_sha256,
                    "artifact_count": len(manifest),
                    "artifact_feature_counts": [
                        item["feature_count"]
                        for item in artifact_geometry_evidence
                    ],
                    "aggregate_source_bytes": sum(
                        item["size_bytes"] for item in manifest
                    ),
                    "standalone_snapshot": True,
                    "source_fids_regenerated": True,
                    "idempotent_reuse": True,
                    "geometry_evidence": geometry_evidence,
                    "geometry_evidence_sha256": geometry_evidence_sha256,
                    "normalization_algorithm": _VECTOR_NORMALIZATION_ALGORITHM,
                    "source_feature_count": source_stats.feature_count,
                    "feature_count": feature_count,
                    "minimum_features": minimum_features,
                    "null_geometries": result_stats.null_geometry_count,
                    "empty_geometries": result_stats.empty_geometry_count,
                    "invalid_geometries": result_stats.invalid_geometry_count,
                    "repaired_geometries": repair_counts[
                        "repaired_geometry_count"
                    ],
                    "discarded_geometries": repair_counts[
                        "discarded_geometry_count"
                    ],
                    "srid": result_stats.srid,
                    "geometry_family": geometry_family,
                    "geometry_type": target_geometry_type,
                    "bounds": bounds,
                    "data_schema": data_schema,
                    "immutable_guards": True,
                    "evidence_persisted_with_table": True,
                },
            }
            _install_table_guards(db, table_name)
            _persist_vector_table_attestation(
                db,
                table_name=table_name,
                storage_key=storage_key,
                manifest_sha256=manifest_sha256,
                content_sha256=content_sha256,
                validation=validation,
            )
            _drop_vector_table_in_transaction(
                db,
                schema_name=_VECTOR_STAGING_SCHEMA,
                table_name=staging_table_name,
            )
            db.commit()
            discard_unpublished_final = False
            return VectorIngestResult(
                table_name=table_name,
                storage_key=storage_key,
                content_sha256=content_sha256,
                feature_count=feature_count,
                crs="EPSG:3857",
                bounds_json=bounds,
                geometry_type=target_geometry_type,
                validation_json=validation,
            )
        except Exception:
            db.rollback()
            try:
                _cleanup_failed_vector_ingest(
                    db,
                    table_name=table_name,
                    staging_table_name=staging_table_name,
                    discard_unpublished_final=discard_unpublished_final,
                )
            except Exception as cleanup_error:
                raise GeoIngestError(
                    "vector ingest failed and staging cleanup was incomplete"
                ) from cleanup_error
            raise


def ingest_raster_artifact(
    store: ReferenceBlobStore,
    *,
    source_path: Path,
    input_sha256: str | None = None,
    input_driver: str | None = None,
    timeout_seconds: int = 3600,
    max_source_bytes: int = MAX_RASTER_SOURCE_BYTES,
    max_pixels: int = DEFAULT_MAX_RASTER_PIXELS,
    max_output_bytes: int = DEFAULT_MAX_RASTER_OUTPUT_BYTES,
    runner: CommandRunner = run_geo_command,
) -> RasterIngestResult:
    """Convert a local raster snapshot to a validated content-addressed COG."""

    driver, snapshot_suffix = _raster_driver(input_driver, source_path)
    if input_sha256 is not None and (
        not isinstance(input_sha256, str)
        or _SHA256_RE.fullmatch(input_sha256) is None
    ):
        raise GeoIngestError("raster source digest is invalid")
    _bounded_positive_integer(
        max_source_bytes,
        maximum=MAX_RASTER_SOURCE_BYTES,
        message="raster source byte limit is invalid",
    )
    _bounded_positive_integer(
        max_pixels,
        maximum=MAX_RASTER_PIXELS,
        message="raster pixel limit is invalid",
    )
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1 <= max_output_bytes <= store.max_blob_bytes
    ):
        raise GeoIngestError("normalized raster byte limit is invalid")

    staging_root = store.root / "staging"
    try:
        staging_metadata = staging_root.lstat()
    except OSError as error:
        raise GeoIngestError("reference raster staging is unavailable") from error
    if not stat.S_ISDIR(staging_metadata.st_mode) or staging_root.is_symlink():
        raise GeoIngestError("reference raster staging is unsafe")

    with tempfile.TemporaryDirectory(
        prefix="raster-normalize-",
        dir=staging_root,
    ) as directory:
        private_directory = Path(directory)
        os.chmod(private_directory, 0o700)
        snapshot = _snapshot_artifact(
            source_path,
            destination=private_directory / f"source{snapshot_suffix}",
            max_bytes=max_source_bytes,
            expected_sha256=input_sha256,
            capacity_store=store,
        )
        _validate_raster_signature(snapshot.path, driver)
        source_argument = _raster_source_argument(snapshot.path, driver)
        environment = _strict_gdal_environment(cpl_tmpdir=private_directory)
        original = _inspect_raster(
            source_argument,
            runner=runner,
            environment=environment,
            timeout=timeout_seconds,
            expected_driver="GTiff",
            max_pixels=max_pixels,
        )
        estimated_output_bytes = _estimated_cog_bytes(original)
        if estimated_output_bytes > max_output_bytes:
            raise GeoIngestError(
                "raster worst-case normalized size exceeds its byte limit"
            )
        # COG creation and the content-addressed commit briefly coexist. The
        # reservation therefore covers two worst-case copies on the reference
        # storage filesystem (and never spills them to the host /tmp).
        estimated_staging_bytes = estimated_output_bytes * 2
        _preflight_store_capacity(store, estimated_staging_bytes)

        output = private_directory / "normalized.tif"
        runner(
            [
                _ALLOWED_BINARIES["gdal_translate"],
                "-of",
                "COG",
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "BIGTIFF=IF_SAFER",
                "-co",
                "OVERVIEWS=AUTO",
                "-co",
                "RESAMPLING=AVERAGE",
                source_argument,
                str(output),
            ],
            environment,
            timeout_seconds,
        )
        output_fingerprint = _regular_file_fingerprint(output)
        inspection = _inspect_raster(
            output,
            runner=runner,
            environment=environment,
            timeout=timeout_seconds,
            require_cog=True,
            expected_driver="GTiff",
            max_pixels=max_pixels,
        )
        if (
            inspection.width != original.width
            or inspection.height != original.height
            or inspection.band_count != original.band_count
            or inspection.band_types != original.band_types
            or inspection.nodata_values != original.nodata_values
            or not math.isclose(
                inspection.pixel_size_x,
                original.pixel_size_x,
                rel_tol=1e-12,
                abs_tol=0.0,
            )
            or not math.isclose(
                inspection.pixel_size_y,
                original.pixel_size_y,
                rel_tol=1e-12,
                abs_tol=0.0,
            )
            or inspection.crs != original.crs
            or inspection.bounds_json != original.bounds_json
        ):
            raise GeoIngestError("COG conversion changed raster semantics")
        if _regular_file_fingerprint(output) != output_fingerprint:
            raise GeoIngestError("normalized raster changed during validation")
        output_size = output_fingerprint[3]
        if output_size > max_output_bytes:
            raise GeoIngestError("normalized raster exceeds its byte limit")
        # The source copy is no longer needed and releasing it avoids charging
        # it against the quota while the normalized blob is committed.
        snapshot.path.unlink()
        with output.open("rb") as stream:
            blob = store.put_stream(stream, max_bytes=max_output_bytes)
    validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "raster",
        "checks": {
            "driver": inspection.driver,
            "cloud_optimized": inspection.is_cog,
            "width": inspection.width,
            "height": inspection.height,
            "band_count": inspection.band_count,
            "band_types": list(inspection.band_types),
            "nodata_values": list(inspection.nodata_values),
            "pixel_size": {
                "x": inspection.pixel_size_x,
                "y": inspection.pixel_size_y,
            },
            "overview_count": len(inspection.overview_sizes),
            "overview_sizes": [
                list(size) for size in inspection.overview_sizes
            ],
            "nodata_preserved": True,
            "resolution_preserved": True,
            "overviews_validated": True,
            "crs": inspection.crs,
            "data_schema": {
                "schema_version": "reference-raster-schema/v1",
                "driver": inspection.driver,
                "band_types": list(inspection.band_types),
                "nodata_values": list(inspection.nodata_values),
                "pixel_size": {
                    "x": inspection.pixel_size_x,
                    "y": inspection.pixel_size_y,
                },
            },
            "input_sha256": snapshot.sha256,
            "input_driver": driver,
            "standalone_snapshot": True,
            "uncompressed_bytes": inspection.uncompressed_bytes,
            "estimated_output_bytes": estimated_output_bytes,
            "estimated_staging_bytes": estimated_staging_bytes,
            "staged_on_reference_storage": True,
        },
    }
    return RasterIngestResult(blob, inspection, validation)


def inspect_tile_archive(
    store: ReferenceBlobStore,
    *,
    storage_key: str,
    archive_sha256: str,
    expected_tile_count: int | None = None,
    expected_coordinate_sha256: str | None = None,
    max_tiles: int = MAX_TILE_COUNT,
) -> TileArchiveInspection:
    """Validate every image and coordinate in an immutable MBTiles archive."""

    if _SHA256_RE.fullmatch(archive_sha256) is None:
        raise GeoIngestError("tile archive digest is invalid")
    if (
        isinstance(max_tiles, bool)
        or not isinstance(max_tiles, int)
        or not 1 <= max_tiles <= MAX_TILE_COUNT
    ):
        raise GeoIngestError("tile archive limit is invalid")
    if expected_tile_count is not None and (
        isinstance(expected_tile_count, bool)
        or not isinstance(expected_tile_count, int)
        or not 1 <= expected_tile_count <= max_tiles
    ):
        raise GeoIngestError("expected tile count is invalid")
    if expected_coordinate_sha256 is not None and (
        not isinstance(expected_coordinate_sha256, str)
        or _SHA256_RE.fullmatch(expected_coordinate_sha256) is None
    ):
        raise GeoIngestError("expected tile coordinate digest is invalid")
    path = store.resolve_blob(storage_key)
    archive_fingerprint = _regular_file_fingerprint(path)
    if _sha256_file(path) != archive_sha256:
        raise GeoIngestError("tile archive bytes do not match their digest")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        _require_mbtiles_schema(connection)
        metadata = _validated_mbtiles_metadata(connection)
        image_format = metadata["format"]
        min_zoom = int(metadata["minzoom"])
        max_zoom = int(metadata["maxzoom"])
        bounds = _metadata_bounds(metadata["bounds"])
        total = int(connection.execute("SELECT count(*) FROM tiles").fetchone()[0])
        if not 1 <= total <= max_tiles:
            raise GeoIngestError("tile archive count is outside its limit")
        if expected_tile_count is not None and total != expected_tile_count:
            raise GeoIngestError("tile archive coverage is incomplete")
        _require_indexed_tile_lookup(connection, min_zoom)
        coordinate_digest = hashlib.sha256()
        seen = 0
        seen_min_zoom: int | None = None
        seen_max_zoom: int | None = None
        for zoom, column, row, body in connection.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles "
            "ORDER BY zoom_level ASC, tile_column ASC, tile_row DESC"
        ):
            if (
                not isinstance(zoom, int)
                or not isinstance(column, int)
                or not isinstance(row, int)
                or not min_zoom <= zoom <= max_zoom
                or not 0 <= column < 2**zoom
                or not 0 <= row < 2**zoom
                or not isinstance(body, bytes)
            ):
                raise GeoIngestError("tile archive coordinate is invalid")
            validate_tile_image(body, image_format)
            _decode_tile_image(body, image_format)
            xyz_row = (2**zoom - 1) - row
            coordinate_digest.update(f"{zoom}/{column}/{xyz_row}\n".encode("ascii"))
            seen += 1
            seen_min_zoom = zoom if seen_min_zoom is None else min(seen_min_zoom, zoom)
            seen_max_zoom = zoom if seen_max_zoom is None else max(seen_max_zoom, zoom)
        if seen != total:
            raise GeoIngestError("tile archive changed during validation")
        if seen_min_zoom != min_zoom or seen_max_zoom != max_zoom:
            raise GeoIngestError("tile archive zoom metadata does not match its tiles")
        coordinate_sha256 = coordinate_digest.hexdigest()
        if metadata["coordinate_sha256"] != coordinate_sha256:
            raise GeoIngestError("tile archive coordinate metadata is invalid")
        if (
            expected_coordinate_sha256 is not None
            and coordinate_sha256 != expected_coordinate_sha256
        ):
            raise GeoIngestError("tile archive coordinate coverage is incomplete")
    except (sqlite3.Error, InvalidLocalTileArchiveError) as error:
        raise GeoIngestError("tile archive is invalid") from error
    finally:
        if connection is not None:
            connection.close()
    if (
        _regular_file_fingerprint(path) != archive_fingerprint
        or _sha256_file(path) != archive_sha256
    ):
        raise GeoIngestError("tile archive changed during validation")
    validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "tiles",
        "checks": {
            "tile_count": total,
            "expected_tile_count": expected_tile_count,
            "coordinate_sha256": coordinate_sha256,
            "expected_coordinate_sha256": expected_coordinate_sha256,
            "min_zoom": min_zoom,
            "max_zoom": max_zoom,
            "format": image_format,
            "all_images_valid": True,
            "all_images_fully_decoded": True,
            "unique_coordinates": True,
            "canonical_unique_index": True,
        },
    }
    return TileArchiveInspection(
        tile_count=total,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        image_format=image_format,
        coordinate_sha256=coordinate_sha256,
        bounds_json=bounds,
        validation_json=validation,
    )


def _inspect_raster(
    path: Path | str,
    *,
    runner: CommandRunner,
    environment: Mapping[str, str],
    timeout: int,
    require_cog: bool = False,
    expected_driver: str = "GTiff",
    max_pixels: int = DEFAULT_MAX_RASTER_PIXELS,
) -> RasterInspection:
    result = runner(
        [_ALLOWED_BINARIES["gdalinfo"], "-json", str(path)],
        environment,
        timeout,
    )
    try:
        payload = json.loads(
            result.stdout.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise GeoIngestError("gdalinfo did not return strict JSON") from error
    if not isinstance(payload, dict):
        raise GeoIngestError("gdalinfo response is malformed")
    size = payload.get("size")
    bands = payload.get("bands")
    driver = payload.get("driverShortName")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in size)
        or not 1 <= size[0]
        or not 1 <= size[1]
        or size[0] * size[1] > max_pixels
        or not isinstance(bands, list)
        or not 1 <= len(bands) <= MAX_RASTER_BANDS
        or not isinstance(driver, str)
        or driver != expected_driver
    ):
        raise GeoIngestError("raster dimensions, bands or driver are invalid")
    sample_bytes: list[int] = []
    band_types: list[str] = []
    nodata_values: list[float | None] = []
    band_overviews: list[tuple[tuple[int, int], ...]] = []
    for band in bands:
        band_type = band.get("type") if isinstance(band, dict) else None
        byte_width = _RASTER_SAMPLE_BYTES.get(band_type)
        if byte_width is None:
            raise GeoIngestError("raster band type is unsupported")
        sample_bytes.append(byte_width)
        band_types.append(band_type)
        nodata_values.append(_raster_nodata_value(band))
        band_overviews.append(
            _raster_overview_sizes(
                band,
                width=size[0],
                height=size[1],
            )
        )
    overview_sizes = band_overviews[0]
    if any(item != overview_sizes for item in band_overviews[1:]):
        raise GeoIngestError("raster bands have inconsistent overview pyramids")
    uncompressed_bytes = size[0] * size[1] * sum(sample_bytes)
    if uncompressed_bytes > MAX_RASTER_UNCOMPRESSED_BYTES:
        raise GeoIngestError("raster uncompressed size exceeds its limit")
    pixel_size_x, pixel_size_y = _raster_pixel_size(payload.get("geoTransform"))
    coordinate_system = payload.get("coordinateSystem")
    wkt = coordinate_system.get("wkt") if isinstance(coordinate_system, dict) else None
    epsg_codes = _EPSG_WKT_RE.findall(wkt) if isinstance(wkt, str) else []
    if not epsg_codes:
        raise GeoIngestError("raster CRS has no unambiguous EPSG identifier")
    bounds = _wgs84_extent(payload.get("wgs84Extent"))
    metadata = payload.get("metadata")
    image_structure = metadata.get("IMAGE_STRUCTURE") if isinstance(metadata, dict) else None
    is_cog = (
        isinstance(image_structure, dict)
        and image_structure.get("LAYOUT") == "COG"
    )
    if require_cog and (driver != "GTiff" or not is_cog):
        raise GeoIngestError("normalized raster is not a cloud-optimized GeoTIFF")
    if require_cog and max(size) > 512 and not overview_sizes:
        raise GeoIngestError("normalized raster is missing internal overviews")
    return RasterInspection(
        width=size[0],
        height=size[1],
        band_count=len(bands),
        band_types=tuple(band_types),
        nodata_values=tuple(nodata_values),
        pixel_size_x=pixel_size_x,
        pixel_size_y=pixel_size_y,
        overview_sizes=overview_sizes,
        crs=f"EPSG:{epsg_codes[-1]}",
        bounds_json=bounds,
        driver=driver,
        is_cog=is_cog,
        uncompressed_bytes=uncompressed_bytes,
    )


def _raster_nodata_value(band: dict[str, Any]) -> float | None:
    value = band.get("noDataValue")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeoIngestError("raster nodata value is invalid")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise GeoIngestError("raster nodata value is invalid")
    return normalized


def _raster_pixel_size(value: Any) -> tuple[float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 6
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise GeoIngestError("raster geotransform is invalid")
    pixel_size_x = math.hypot(float(value[1]), float(value[4]))
    pixel_size_y = math.hypot(float(value[2]), float(value[5]))
    if pixel_size_x <= 0 or pixel_size_y <= 0:
        raise GeoIngestError("raster resolution is invalid")
    return pixel_size_x, pixel_size_y


def _raster_overview_sizes(
    band: dict[str, Any],
    *,
    width: int,
    height: int,
) -> tuple[tuple[int, int], ...]:
    value = band.get("overviews", [])
    if not isinstance(value, list) or len(value) > 32:
        raise GeoIngestError("raster overview pyramid is invalid")
    result: list[tuple[int, int]] = []
    previous = (width, height)
    for overview in value:
        size = overview.get("size") if isinstance(overview, dict) else None
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or item <= 0
                for item in size
            )
        ):
            raise GeoIngestError("raster overview pyramid is invalid")
        current = (size[0], size[1])
        if (
            current[0] > previous[0]
            or current[1] > previous[1]
            or current == previous
        ):
            raise GeoIngestError("raster overview pyramid is invalid")
        result.append(current)
        previous = current
    return tuple(result)


def _vector_driver(input_driver: str | None, source_path: Path) -> tuple[str, str]:
    if input_driver is not None:
        if not isinstance(input_driver, str):
            raise GeoIngestError("vector input driver is invalid")
        selected = _VECTOR_DRIVER_ALIASES.get(input_driver.casefold())
    else:
        suffix = Path(source_path).suffix.casefold()
        selected = next(
            (
                (driver, expected_suffix)
                for driver, expected_suffix in _ALLOWED_VECTOR_DRIVERS.items()
                if suffix in {expected_suffix, ".json" if driver == "GeoJSON" else ""}
            ),
            None,
        )
    if selected is None:
        raise GeoIngestError("vector input driver is not allowlisted")
    return selected


def _vector_artifact_inputs(
    artifacts: Sequence[
        VectorArtifactInput | tuple[Path, str] | tuple[Path, str, str | None]
    ],
) -> tuple[VectorArtifactInput, ...]:
    if (
        isinstance(artifacts, (str, bytes, bytearray))
        or not isinstance(artifacts, Sequence)
        or not 1 <= len(artifacts) <= MAX_VECTOR_ARTIFACTS
    ):
        raise GeoIngestError("vector artifact sequence is invalid")
    result: list[VectorArtifactInput] = []
    identities: set[tuple[str, str | None]] = set()
    for value in artifacts:
        if isinstance(value, VectorArtifactInput):
            artifact = value
        elif isinstance(value, tuple) and len(value) in {2, 3}:
            try:
                artifact = VectorArtifactInput(
                    source_path=Path(value[0]),
                    input_sha256=value[1],
                    input_layer=value[2] if len(value) == 3 else None,
                )
            except (TypeError, ValueError) as error:
                raise GeoIngestError("vector artifact path is invalid") from error
        else:
            raise GeoIngestError("vector artifact sequence is invalid")
        try:
            source_path = Path(artifact.source_path)
        except (TypeError, ValueError) as error:
            raise GeoIngestError("vector artifact path is invalid") from error
        if (
            not isinstance(artifact.input_sha256, str)
            or _SHA256_RE.fullmatch(artifact.input_sha256) is None
            or (
                artifact.input_layer is not None
                and not isinstance(artifact.input_layer, str)
            )
        ):
            raise GeoIngestError("vector artifact identity is invalid")
        identity = (artifact.input_sha256, artifact.input_layer)
        if identity in identities:
            raise GeoIngestError("vector artifact sequence contains a duplicate page")
        identities.add(identity)
        result.append(
            VectorArtifactInput(
                source_path=source_path,
                input_sha256=artifact.input_sha256,
                input_layer=artifact.input_layer,
            )
        )
    return tuple(result)


def _vector_import_command(
    *,
    database: GeoDatabaseTarget,
    storage_key: str,
    snapshot: Path,
    driver: str,
    input_layer: str | None,
    append: bool,
) -> list[str]:
    gdal_driver = "GML" if driver == "GMLZIP" else driver
    source_argument = _vector_source_argument(snapshot, driver)
    command = [
        _ALLOWED_BINARIES["ogr2ogr"],
        "-if",
        gdal_driver,
        "-f",
        "PostgreSQL",
    ]
    if append:
        command.extend(["-update", "-append"])
    command.extend(
        [
            "-nln",
            storage_key,
            "-nlt",
            "PROMOTE_TO_MULTI",
            "-t_srs",
            "EPSG:3857",
            # Provider FIDs can restart on each page. Keep OBJECTID and other
            # source attributes, but let PostgreSQL allocate a unique row FID.
            "-unsetFid",
        ]
    )
    if not append:
        command.extend(
            [
                "-lco",
                "GEOMETRY_NAME=geom",
                "-lco",
                "FID=source_fid",
            ]
        )
    command.extend([database.ogr_connection, source_argument])
    if input_layer is not None:
        command.append(input_layer)
    return command


def _vector_staging_table_name(table_name: str) -> str:
    if _VERSIONED_VECTOR_TABLE_RE.fullmatch(table_name) is None:
        raise GeoIngestError("vector table identity is invalid")
    identity = table_name.rsplit("_v_", 1)[1]
    value = f"s_v_{identity}_{os.urandom(8).hex()}"
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise GeoIngestError("vector staging table identity is invalid")
    return value


def _vector_ingest_lock_key_from_identity(identity: str) -> int:
    if re.fullmatch(r"[0-9a-f]{24}", identity, re.ASCII) is None:
        raise GeoIngestError("vector table identity is invalid")
    digest = hashlib.sha256(
        f"reference-vector-ingest/v1:{identity}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _vector_ingest_lock_key(table_name: str) -> int:
    if _VERSIONED_VECTOR_TABLE_RE.fullmatch(table_name) is None:
        raise GeoIngestError("vector table identity is invalid")
    return _vector_ingest_lock_key_from_identity(
        table_name.rsplit("_v_", 1)[1]
    )


def _try_vector_ingest_lock(db: Session, table_name: str) -> bool:
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": _vector_ingest_lock_key(table_name)},
        ).scalar_one()
    )


def _try_vector_ingest_identity_lock(db: Session, identity: str) -> bool:
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": _vector_ingest_lock_key_from_identity(identity)},
        ).scalar_one()
    )


def _table_exists(
    db: Session,
    *,
    schema_name: str,
    table_name: str,
) -> bool:
    if (
        schema_name not in {_VECTOR_DATA_SCHEMA, _VECTOR_STAGING_SCHEMA}
        or _IDENTIFIER_RE.fullmatch(table_name) is None
    ):
        raise GeoIngestError("vector table lookup is invalid")
    return (
        db.execute(
            text("SELECT to_regclass(:key)"),
            {"key": f"{schema_name}.{table_name}"},
        ).scalar_one()
        is not None
    )


def _drop_vector_table_in_transaction(
    db: Session,
    *,
    schema_name: str,
    table_name: str,
) -> None:
    if (
        schema_name not in {_VECTOR_DATA_SCHEMA, _VECTOR_STAGING_SCHEMA}
        or _IDENTIFIER_RE.fullmatch(table_name) is None
    ):
        raise GeoIngestError("vector table cleanup identity is invalid")
    db.execute(text(f"DROP TABLE IF EXISTS {schema_name}.{table_name}"))


def _cleanup_abandoned_vector_staging_tables(
    db: Session,
) -> None:
    """Remove only staging tables whose identity lock has no live owner."""

    rows = db.execute(
        text(
            """
            SELECT relation.relname
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = :schema_name
              AND relation.relkind IN ('r', 'p')
            ORDER BY relation.relname
            """
        ),
        {"schema_name": _VECTOR_STAGING_SCHEMA},
    ).scalars().all()
    for staging_name in rows:
        if not isinstance(staging_name, str):
            continue
        match = _STAGING_VECTOR_TABLE_RE.fullmatch(staging_name)
        if match is None:
            continue
        if _try_vector_ingest_identity_lock(db, match.group("identity")):
            _drop_vector_table_in_transaction(
                db,
                schema_name=_VECTOR_STAGING_SCHEMA,
                table_name=staging_name,
            )


def _cleanup_failed_vector_ingest(
    db: Session,
    *,
    table_name: str,
    staging_table_name: str,
    discard_unpublished_final: bool,
) -> None:
    """Compensate committed GDAL staging writes without racing a retry."""

    if not _try_vector_ingest_lock(db, table_name):
        db.rollback()
        return
    _drop_vector_table_in_transaction(
        db,
        schema_name=_VECTOR_STAGING_SCHEMA,
        table_name=staging_table_name,
    )
    _cleanup_abandoned_vector_staging_tables(db)
    if (
        discard_unpublished_final
        and _table_exists(
            db,
            schema_name=_VECTOR_DATA_SCHEMA,
            table_name=table_name,
        )
        and not _table_has_immutable_guards(db, table_name)
    ):
        _drop_vector_table_in_transaction(
            db,
            schema_name=_VECTOR_DATA_SCHEMA,
            table_name=table_name,
        )
    db.commit()


def _empty_vector_geometry_stats() -> _VectorGeometryStats:
    return _VectorGeometryStats(
        feature_count=0,
        source_fid_count=0,
        max_source_fid=None,
        null_geometry_count=0,
        empty_geometry_count=0,
        invalid_geometry_count=0,
        valid_geometry_count=0,
        multi_geometry_count=0,
        multiple_part_geometry_count=0,
        geometry_part_count=0,
        srid_count=0,
        srid=None,
        geometry_types=(),
        extent=None,
    )


def _vector_geometry_stats(
    db: Session,
    *,
    schema_name: str,
    table_name: str,
    source_fid_after: int | None = None,
    include_extent: bool = False,
) -> _VectorGeometryStats:
    if (
        schema_name not in {_VECTOR_DATA_SCHEMA, _VECTOR_STAGING_SCHEMA}
        or _IDENTIFIER_RE.fullmatch(table_name) is None
    ):
        raise GeoIngestError("vector geometry inspection identity is invalid")
    if source_fid_after is not None and (
        isinstance(source_fid_after, bool)
        or not isinstance(source_fid_after, int)
        or source_fid_after < 1
    ):
        raise GeoIngestError("vector geometry inspection range is invalid")
    if not isinstance(include_extent, bool):
        raise GeoIngestError("vector extent inspection option is invalid")
    row_filter = (
        ""
        if source_fid_after is None
        else "WHERE source_fid > :source_fid_after"
    )
    extent_expression = (
        """
        ST_Extent(ST_Transform(geom, 4326)) FILTER (
            WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        )::text
        """
        if include_extent
        else "NULL::text"
    )
    row = db.execute(
        text(
            f"""
            SELECT
                count(*)::bigint AS feature_count,
                count(DISTINCT source_fid)::bigint AS source_fid_count,
                max(source_fid)::bigint AS max_source_fid,
                count(*) FILTER (
                    WHERE geom IS NULL
                )::bigint AS null_geometry_count,
                count(*) FILTER (
                    WHERE geom IS NOT NULL AND ST_IsEmpty(geom)
                )::bigint AS empty_geometry_count,
                count(*) FILTER (
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                      AND NOT ST_IsValid(geom)
                )::bigint AS invalid_geometry_count,
                count(*) FILTER (
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                      AND ST_IsValid(geom)
                )::bigint AS valid_geometry_count,
                count(*) FILTER (
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                      AND GeometryType(geom) LIKE 'MULTI%'
                )::bigint AS multi_geometry_count,
                count(*) FILTER (
                    WHERE geom IS NOT NULL
                      AND NOT ST_IsEmpty(geom)
                      AND ST_NumGeometries(geom) > 1
                )::bigint AS multiple_part_geometry_count,
                coalesce(sum(ST_NumGeometries(geom)) FILTER (
                    WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                ), 0)::bigint AS geometry_part_count,
                count(DISTINCT ST_SRID(geom)) FILTER (
                    WHERE geom IS NOT NULL
                )::integer AS srid_count,
                min(ST_SRID(geom)) FILTER (
                    WHERE geom IS NOT NULL
                )::integer AS srid,
                coalesce(
                    array_agg(DISTINCT GeometryType(geom)) FILTER (
                        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
                    ),
                    ARRAY[]::text[]
                ) AS geometry_types,
                {extent_expression} AS extent
            FROM {schema_name}.{table_name}
            {row_filter}
            """
        ),
        (
            {}
            if source_fid_after is None
            else {"source_fid_after": source_fid_after}
        ),
    ).mappings().one()
    raw_types = row["geometry_types"]
    if not isinstance(raw_types, (list, tuple)) or any(
        not isinstance(value, str) for value in raw_types
    ):
        raise GeoIngestError("vector geometry types are invalid")
    stats = _VectorGeometryStats(
        feature_count=int(row["feature_count"]),
        source_fid_count=int(row["source_fid_count"]),
        max_source_fid=(
            int(row["max_source_fid"])
            if row["max_source_fid"] is not None
            else None
        ),
        null_geometry_count=int(row["null_geometry_count"]),
        empty_geometry_count=int(row["empty_geometry_count"]),
        invalid_geometry_count=int(row["invalid_geometry_count"]),
        valid_geometry_count=int(row["valid_geometry_count"]),
        multi_geometry_count=int(row["multi_geometry_count"]),
        multiple_part_geometry_count=int(
            row["multiple_part_geometry_count"]
        ),
        geometry_part_count=int(row["geometry_part_count"]),
        srid_count=int(row["srid_count"]),
        srid=int(row["srid"]) if row["srid"] is not None else None,
        geometry_types=tuple(sorted(raw_types)),
        extent=row["extent"],
    )
    _validate_vector_stats_invariants(stats)
    return stats


def _validate_vector_stats_invariants(stats: _VectorGeometryStats) -> None:
    counters = stats.counters()
    if any(value < 0 for value in counters.values()) or (
        stats.feature_count
        != stats.null_geometry_count
        + stats.empty_geometry_count
        + stats.invalid_geometry_count
        + stats.valid_geometry_count
    ):
        raise GeoIngestError("vector geometry counters are inconsistent")
    nonempty = stats.invalid_geometry_count + stats.valid_geometry_count
    if (
        stats.source_fid_count < 0
        or (
            stats.feature_count == 0
            and stats.max_source_fid is not None
        )
        or (
            stats.feature_count > 0
            and (
                stats.max_source_fid is None
                or stats.max_source_fid < 1
            )
        )
        or stats.srid_count < 0
        or stats.multi_geometry_count > nonempty
        or stats.multiple_part_geometry_count > nonempty
        or stats.geometry_part_count < nonempty
        or (stats.srid_count == 0) != (stats.srid is None)
    ):
        raise GeoIngestError("vector geometry statistics are inconsistent")


def _sum_vector_artifact_evidence(
    artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    totals = _empty_vector_geometry_stats().counters()
    for artifact in artifacts:
        for key in totals:
            value = artifact.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GeoIngestError(
                    "per-artifact geometry evidence is invalid"
                )
            totals[key] += value
    return totals


def _validate_source_vector_stats(stats: _VectorGeometryStats) -> None:
    if stats.source_fid_count != stats.feature_count:
        raise GeoIngestError("vector staging feature identifiers are not unique")
    if stats.invalid_geometry_count + stats.valid_geometry_count < 1:
        raise GeoIngestError("vector staging has no usable geometry")
    if stats.srid_count != 1 or stats.srid != 3857:
        raise GeoIngestError("vector staging CRS is inconsistent")


def _normalization_geometry_family(
    geometry_types: Sequence[str],
) -> tuple[str, str, int]:
    family_by_type = {
        "POINT": ("point", "MULTIPOINT", 1),
        "MULTIPOINT": ("point", "MULTIPOINT", 1),
        "LINESTRING": ("line", "MULTILINESTRING", 2),
        "MULTILINESTRING": ("line", "MULTILINESTRING", 2),
        "POLYGON": ("polygon", "MULTIPOLYGON", 3),
        "MULTIPOLYGON": ("polygon", "MULTIPOLYGON", 3),
    }
    selected = {family_by_type.get(value.upper()) for value in geometry_types}
    if None in selected or len(selected) != 1:
        raise GeoIngestError("vector staging mixes geometry families")
    family = selected.pop()
    assert family is not None
    return family


def _normalized_geometry_sql(
    *,
    source_alias: str,
    collection_dimension: int,
    target_geometry_type: str,
) -> str:
    if (
        _IDENTIFIER_RE.fullmatch(source_alias) is None
        or collection_dimension not in {1, 2, 3}
        or target_geometry_type
        not in {"MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON"}
    ):
        raise GeoIngestError("vector normalization identity is invalid")
    geometry = f'{source_alias}."geom"'
    return (
        "CAST(ST_Multi(ST_CollectionExtract("
        f"CASE WHEN ST_IsValid({geometry}) THEN {geometry} "
        f"ELSE ST_MakeValid({geometry}) END, {collection_dimension}"
        f")) AS geometry({target_geometry_type}, 3857))"
    )


def _vector_repair_counts(
    db: Session,
    *,
    staging_table_name: str,
    collection_dimension: int,
    target_geometry_type: str,
    source_stats: _VectorGeometryStats,
) -> dict[str, int]:
    if _IDENTIFIER_RE.fullmatch(staging_table_name) is None:
        raise GeoIngestError("vector staging table identity is invalid")
    normalized = _normalized_geometry_sql(
        source_alias="source",
        collection_dimension=collection_dimension,
        target_geometry_type=target_geometry_type,
    )
    row = db.execute(
        text(
            f"""
            WITH candidates AS (
                SELECT
                    NOT ST_IsValid(source.geom) AS was_invalid,
                    {normalized} AS normalized_geom
                FROM {_VECTOR_STAGING_SCHEMA}.{staging_table_name} AS source
                WHERE source.geom IS NOT NULL
                  AND NOT ST_IsEmpty(source.geom)
            )
            SELECT
                count(*) FILTER (
                    WHERE was_invalid
                      AND normalized_geom IS NOT NULL
                      AND NOT ST_IsEmpty(normalized_geom)
                )::bigint AS repaired_geometry_count,
                count(*) FILTER (
                    WHERE was_invalid
                      AND (
                        normalized_geom IS NULL
                        OR ST_IsEmpty(normalized_geom)
                      )
                )::bigint AS unrepairable_geometry_count,
                count(*) FILTER (
                    WHERE NOT was_invalid
                      AND normalized_geom IS NOT NULL
                      AND NOT ST_IsEmpty(normalized_geom)
                )::bigint AS retained_valid_geometry_count,
                count(*) FILTER (
                    WHERE NOT was_invalid
                      AND (
                        normalized_geom IS NULL
                        OR ST_IsEmpty(normalized_geom)
                      )
                )::bigint AS discarded_valid_geometry_count,
                count(*) FILTER (
                    WHERE normalized_geom IS NOT NULL
                      AND NOT ST_IsEmpty(normalized_geom)
                )::bigint AS result_feature_count
            FROM candidates
            """
        )
    ).mappings().one()
    repaired = int(row["repaired_geometry_count"])
    unrepairable = int(row["unrepairable_geometry_count"])
    retained_valid = int(row["retained_valid_geometry_count"])
    discarded_valid = int(row["discarded_valid_geometry_count"])
    result_feature_count = int(row["result_feature_count"])
    if (
        repaired + unrepairable != source_stats.invalid_geometry_count
        or retained_valid + discarded_valid != source_stats.valid_geometry_count
        or discarded_valid != 0
        or result_feature_count != repaired + retained_valid
    ):
        raise GeoIngestError("vector repair accounting is inconsistent")
    discarded = (
        source_stats.null_geometry_count
        + source_stats.empty_geometry_count
        + unrepairable
    )
    if result_feature_count + discarded != source_stats.feature_count:
        raise GeoIngestError("vector discard accounting is inconsistent")
    return {
        "repaired_geometry_count": repaired,
        "retained_valid_geometry_count": retained_valid,
        "discarded_null_geometry_count": source_stats.null_geometry_count,
        "discarded_empty_geometry_count": source_stats.empty_geometry_count,
        "discarded_unrepairable_geometry_count": unrepairable,
        "discarded_geometry_count": discarded,
        "result_feature_count": result_feature_count,
    }


def _quoted_postgres_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise GeoIngestError("vector column identity is invalid")
    return '"' + value.replace('"', '""') + '"'


def _create_normalized_vector_table(
    db: Session,
    *,
    staging_table_name: str,
    table_name: str,
    collection_dimension: int,
    target_geometry_type: str,
) -> None:
    if (
        _IDENTIFIER_RE.fullmatch(staging_table_name) is None
        or _VERSIONED_VECTOR_TABLE_RE.fullmatch(table_name) is None
    ):
        raise GeoIngestError("vector normalization table identity is invalid")
    staging_schema = _vector_table_schema(
        db,
        staging_table_name,
        schema_name=_VECTOR_STAGING_SCHEMA,
    )
    columns = staging_schema["columns"]
    names = [item["name"] for item in columns]
    if names.count("source_fid") != 1 or names.count("geom") != 1:
        raise GeoIngestError(
            "vector staging requires source_fid and geom columns"
        )
    if any(
        item["name"] != "geom"
        and str(item["data_type"]).casefold().startswith(
            ("geometry", "geography")
        )
        for item in columns
    ):
        raise GeoIngestError(
            "vector staging contains an unsupported extra geometry column"
        )
    normalized = _normalized_geometry_sql(
        source_alias="source",
        collection_dimension=collection_dimension,
        target_geometry_type=target_geometry_type,
    )
    selected_columns = ", ".join(
        (
            "normalized_geometry.value AS "
            f"{_quoted_postgres_identifier(name)}"
            if name == "geom"
            else f"source.{_quoted_postgres_identifier(name)}"
        )
        for name in names
    )
    db.execute(
        text(
            f"""
            CREATE TABLE {_VECTOR_DATA_SCHEMA}.{table_name} AS
            SELECT {selected_columns}
            FROM {_VECTOR_STAGING_SCHEMA}.{staging_table_name} AS source
            CROSS JOIN LATERAL (
                SELECT {normalized} AS value
            ) AS normalized_geometry
            WHERE source.geom IS NOT NULL
              AND NOT ST_IsEmpty(source.geom)
              AND normalized_geometry.value IS NOT NULL
              AND NOT ST_IsEmpty(normalized_geometry.value)
            """
        )
    )
    identity = hashlib.sha256(table_name.encode("ascii")).hexdigest()[:20]
    db.execute(
        text(
            f"ALTER TABLE {_VECTOR_DATA_SCHEMA}.{table_name} "
            f"ADD CONSTRAINT vp_{identity} PRIMARY KEY (source_fid)"
        )
    )
    db.execute(
        text(
            f"ALTER TABLE {_VECTOR_DATA_SCHEMA}.{table_name} "
            "ALTER COLUMN geom SET NOT NULL"
        )
    )
    db.execute(
        text(
            f"CREATE INDEX vg_{identity} "
            f"ON {_VECTOR_DATA_SCHEMA}.{table_name} USING GIST (geom)"
        )
    )


def _validate_result_vector_stats(
    stats: _VectorGeometryStats,
    *,
    expected_geometry_type: str,
    expected_feature_count: int,
) -> None:
    if (
        stats.feature_count != expected_feature_count
        or stats.source_fid_count != stats.feature_count
        or stats.null_geometry_count != 0
        or stats.empty_geometry_count != 0
        or stats.invalid_geometry_count != 0
        or stats.valid_geometry_count != stats.feature_count
        or stats.srid_count != 1
        or stats.srid != 3857
        or stats.geometry_types != (expected_geometry_type,)
        or stats.multi_geometry_count != stats.feature_count
        or stats.extent is None
    ):
        raise GeoIngestError(
            "normalized vector table failed its post-repair validation"
        )


def _vector_transform_identity(
    *,
    manifest: Sequence[Mapping[str, Any]],
    storage_key: str,
    geometry_evidence_sha256: str,
    feature_count: int,
    geometry_type: str,
    data_schema: Mapping[str, Any],
    bounds: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "reference-vector-normalization-v4",
        "input_manifest": list(manifest),
        "table": storage_key,
        "target_crs": "EPSG:3857",
        "normalization_algorithm": _VECTOR_NORMALIZATION_ALGORITHM,
        "geometry_evidence_sha256": geometry_evidence_sha256,
        "feature_count": feature_count,
        "geometry_type": geometry_type,
        "data_schema": dict(data_schema),
        "bounds": dict(bounds),
    }


def _canonical_json_text(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise GeoIngestError("vector evidence is not canonical JSON") from error
    if len(encoded.encode("utf-8")) > 4 * 1024 * 1024:
        raise GeoIngestError("vector evidence is too large")
    return encoded


def _persist_vector_table_attestation(
    db: Session,
    *,
    table_name: str,
    storage_key: str,
    manifest_sha256: str,
    content_sha256: str,
    validation: Mapping[str, Any],
) -> None:
    if (
        _VERSIONED_VECTOR_TABLE_RE.fullmatch(table_name) is None
        or storage_key != f"{_VECTOR_DATA_SCHEMA}.{table_name}"
        or _SHA256_RE.fullmatch(manifest_sha256) is None
        or _SHA256_RE.fullmatch(content_sha256) is None
    ):
        raise GeoIngestError("vector table attestation identity is invalid")
    checks = validation.get("checks")
    geometry_evidence_sha256 = (
        checks.get("geometry_evidence_sha256")
        if isinstance(checks, Mapping)
        else None
    )
    if (
        not isinstance(geometry_evidence_sha256, str)
        or _SHA256_RE.fullmatch(geometry_evidence_sha256) is None
    ):
        raise GeoIngestError("vector geometry evidence digest is invalid")
    validation_json = dict(validation)
    validation_sha256 = canonical_json_sha256(validation_json)
    core = {
        "schema_version": _VECTOR_TABLE_ATTESTATION_SCHEMA,
        "table": storage_key,
        "input_manifest_sha256": manifest_sha256,
        "geometry_evidence_sha256": geometry_evidence_sha256,
        "content_sha256": content_sha256,
        "validation_sha256": validation_sha256,
        "validation": validation_json,
    }
    payload = {
        **core,
        "attestation_sha256": canonical_json_sha256(core),
    }
    canonical = _canonical_json_text(payload)
    escaped = canonical.replace("'", "''")
    db.execute(text("SET LOCAL standard_conforming_strings = on"))
    db.connection().exec_driver_sql(
        f"COMMENT ON TABLE {_VECTOR_DATA_SCHEMA}.{table_name} "
        f"IS '{escaped}'"
    )
    loaded = _load_vector_table_attestation(db, table_name)
    if loaded != payload:
        raise GeoIngestError("vector table attestation did not persist exactly")


def _load_vector_table_attestation(
    db: Session,
    table_name: str,
) -> dict[str, Any]:
    if _VERSIONED_VECTOR_TABLE_RE.fullmatch(table_name) is None:
        raise GeoIngestError("vector table attestation identity is invalid")
    value = db.execute(
        text(
            "SELECT obj_description(to_regclass(:key), 'pg_class')"
        ),
        {"key": f"{_VECTOR_DATA_SCHEMA}.{table_name}"},
    ).scalar_one()
    if not isinstance(value, str) or not value:
        raise GeoIngestError(
            "immutable vector table has no geometry attestation"
        )
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(item)
            ),
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise GeoIngestError("vector table attestation is malformed") from error
    expected_keys = {
        "schema_version",
        "table",
        "input_manifest_sha256",
        "geometry_evidence_sha256",
        "content_sha256",
        "validation_sha256",
        "validation",
        "attestation_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise GeoIngestError("vector table attestation is malformed")
    core = {
        key: value
        for key, value in payload.items()
        if key != "attestation_sha256"
    }
    if (
        payload.get("schema_version") != _VECTOR_TABLE_ATTESTATION_SCHEMA
        or not isinstance(payload.get("attestation_sha256"), str)
        or canonical_json_sha256(core) != payload["attestation_sha256"]
        or not isinstance(payload.get("validation"), dict)
        or canonical_json_sha256(payload["validation"])
        != payload.get("validation_sha256")
    ):
        raise GeoIngestError("vector table attestation hash is invalid")
    return payload


def _recover_vector_ingest_result(
    db: Session,
    *,
    table_name: str,
    storage_key: str,
    manifest: Sequence[Mapping[str, Any]],
    manifest_sha256: str,
    minimum_features: int,
) -> VectorIngestResult:
    attestation = _load_vector_table_attestation(db, table_name)
    validation = attestation["validation"]
    checks = validation.get("checks")
    evidence = (
        checks.get("geometry_evidence")
        if isinstance(checks, dict)
        else None
    )
    evidence_sha256 = (
        checks.get("geometry_evidence_sha256")
        if isinstance(checks, dict)
        else None
    )
    if (
        attestation.get("table") != storage_key
        or attestation.get("input_manifest_sha256") != manifest_sha256
        or attestation.get("geometry_evidence_sha256") != evidence_sha256
        or validation.get("schema_version")
        != "reference-delivery-validation/v1"
        or validation.get("passed") is not True
        or validation.get("kind") != "vector"
        or not isinstance(evidence, dict)
        or evidence.get("schema_version")
        != _VECTOR_GEOMETRY_EVIDENCE_SCHEMA
        or evidence.get("input_manifest_sha256") != manifest_sha256
        or canonical_json_sha256(evidence) != evidence_sha256
        or checks.get("input_manifest") != list(manifest)
    ):
        raise GeoIngestError(
            "immutable vector table geometry evidence is inconsistent"
        )
    result_evidence = evidence.get("result")
    normalization = evidence.get("normalization")
    if not isinstance(result_evidence, dict) or not isinstance(
        normalization, dict
    ):
        raise GeoIngestError("vector geometry evidence is malformed")
    geometry_type = normalization.get("target_geometry_type")
    geometry_family = normalization.get("geometry_family")
    expected_family = {
        "MULTIPOINT": "point",
        "MULTILINESTRING": "line",
        "MULTIPOLYGON": "polygon",
    }.get(geometry_type)
    if expected_family is None or geometry_family != expected_family:
        raise GeoIngestError("vector geometry evidence family is invalid")
    stats = _vector_geometry_stats(
        db,
        schema_name=_VECTOR_DATA_SCHEMA,
        table_name=table_name,
        include_extent=True,
    )
    expected_feature_count = result_evidence.get("feature_count")
    if (
        isinstance(expected_feature_count, bool)
        or not isinstance(expected_feature_count, int)
    ):
        raise GeoIngestError("vector geometry evidence count is invalid")
    _validate_result_vector_stats(
        stats,
        expected_geometry_type=geometry_type,
        expected_feature_count=expected_feature_count,
    )
    if stats.feature_count < minimum_features:
        raise GeoIngestError("vector feature count is below its minimum")
    bounds = _postgis_extent(stats.extent)
    data_schema = _vector_table_schema(db, table_name)
    actual_result_evidence = {
        **stats.counters(),
        "source_fid_count": stats.source_fid_count,
        "max_source_fid": stats.max_source_fid,
        "srid_count": stats.srid_count,
        "srid": stats.srid,
        "crs": "EPSG:3857",
        "geometry_family": geometry_family,
        "geometry_type": geometry_type,
        "bounds": bounds,
        "data_schema_sha256": canonical_json_sha256(data_schema),
    }
    if (
        actual_result_evidence != result_evidence
        or checks.get("data_schema") != data_schema
        or checks.get("feature_count") != stats.feature_count
        or checks.get("bounds") != bounds
        or checks.get("null_geometries") != 0
        or checks.get("empty_geometries") != 0
        or checks.get("invalid_geometries") != 0
    ):
        raise GeoIngestError(
            "immutable vector table no longer matches its geometry evidence"
        )
    transform_identity = _vector_transform_identity(
        manifest=manifest,
        storage_key=storage_key,
        geometry_evidence_sha256=evidence_sha256,
        feature_count=stats.feature_count,
        geometry_type=geometry_type,
        data_schema=data_schema,
        bounds=bounds,
    )
    content_sha256 = canonical_json_sha256(transform_identity)
    if content_sha256 != attestation.get("content_sha256"):
        raise GeoIngestError("immutable vector table content hash is invalid")
    return VectorIngestResult(
        table_name=table_name,
        storage_key=storage_key,
        content_sha256=content_sha256,
        feature_count=stats.feature_count,
        crs="EPSG:3857",
        bounds_json=bounds,
        geometry_type=geometry_type,
        validation_json=validation,
    )


def _raster_driver(input_driver: str | None, source_path: Path) -> tuple[str, str]:
    if input_driver is not None:
        if not isinstance(input_driver, str):
            raise GeoIngestError("raster input driver is invalid")
        selected = _RASTER_DRIVER_ALIASES.get(input_driver.casefold())
    else:
        suffix = Path(source_path).suffix.casefold()
        selected = ("GTiff", ".tif") if suffix in {".tif", ".tiff"} else None
    if selected is None:
        raise GeoIngestError("raster input driver is not allowlisted")
    return selected


def _validate_vector_signature(path: Path, driver: str) -> None:
    if driver == "GMLZIP":
        _cadastral_gml_member(path)
        return
    try:
        with path.open("rb") as source:
            prefix = source.read(4096)
            source.seek(max(0, path.stat().st_size - 4096))
            suffix = source.read(4096)
    except OSError as error:
        raise GeoIngestError("vector snapshot could not be identified") from error
    if driver == "GeoJSON":
        if not prefix.lstrip().startswith(b"{") or not suffix.rstrip().endswith(b"}"):
            raise GeoIngestError("vector snapshot does not match GeoJSON")
    elif driver == "FlatGeobuf":
        if not prefix.startswith(b"fgb\x03fgb\x01"):
            raise GeoIngestError("vector snapshot does not match FlatGeobuf")
    else:  # pragma: no cover - guarded by _vector_driver
        raise GeoIngestError("vector input driver is not allowlisted")


def _vector_source_argument(path: Path, driver: str) -> str:
    if driver != "GMLZIP":
        return str(path)
    member = _cadastral_gml_member(path)
    return f"/vsizip/{path.as_posix()}/{member}"


def _cadastral_gml_member(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 100_000:
                raise GeoIngestError(
                    "cadastral GML archive entry count is invalid"
                )
            matches: list[str] = []
            for entry in entries:
                name = entry.filename
                pure = PurePosixPath(name.replace("\\", "/"))
                if (
                    not name
                    or len(name) > 4096
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or entry.flag_bits & 0x1
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise GeoIngestError(
                        "cadastral GML archive contains an unsafe entry"
                    )
                if entry.is_dir():
                    continue
                if (
                    len(pure.parts) == 1
                    and pure.name.casefold().endswith(
                        ".cadastralparcel.gml"
                    )
                    and re.fullmatch(
                        r"[A-Za-z0-9._-]{1,255}",
                        pure.name,
                        re.ASCII,
                    )
                    is not None
                ):
                    matches.append(pure.name)
            if len(matches) != 1:
                raise GeoIngestError(
                    "cadastral GML archive has no unique parcel member"
                )
            member = matches[0]
            with archive.open(member) as source:
                prefix = source.read(256 * 1024)
            lowered = prefix.lower()
            if (
                b"\x00" in prefix
                or b"<!doctype" in lowered
                or b"<!entity" in lowered
                or b"featurecollection" not in lowered
                or b"cadastralparcel" not in lowered
            ):
                raise GeoIngestError(
                    "cadastral GML archive member is invalid"
                )
            return member
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, GeoIngestError):
            raise
        raise GeoIngestError(
            "cadastral GML archive could not be inspected"
        ) from error


def _validate_raster_signature(path: Path, driver: str) -> None:
    if driver == "GTiffZIP":
        _geotiff_zip_member(path)
        return
    try:
        with path.open("rb") as source:
            prefix = source.read(4)
    except OSError as error:
        raise GeoIngestError("raster snapshot could not be identified") from error
    if driver != "GTiff" or prefix not in {
        b"II*\x00",
        b"MM\x00*",
        b"II+\x00",
        b"MM\x00+",
    }:
        raise GeoIngestError("raster snapshot does not match GeoTIFF")


def _raster_source_argument(path: Path, driver: str) -> str:
    if driver != "GTiffZIP":
        return str(path)
    member = _geotiff_zip_member(path)
    return f"/vsizip/{path.as_posix()}/{member}"


def _geotiff_zip_member(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 100_000:
                raise GeoIngestError(
                    "GeoTIFF archive entry count is invalid"
                )
            matches: list[str] = []
            for entry in entries:
                name = entry.filename
                pure = PurePosixPath(name.replace("\\", "/"))
                if (
                    not name
                    or len(name) > 4096
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or entry.flag_bits & 0x1
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise GeoIngestError(
                        "GeoTIFF archive contains an unsafe entry"
                    )
                if entry.is_dir():
                    continue
                if (
                    len(pure.parts) == 1
                    and pure.suffix.casefold() in {".tif", ".tiff"}
                ):
                    matches.append(pure.name)
            if len(matches) != 1:
                raise GeoIngestError(
                    "GeoTIFF archive has no unique raster member"
                )
            member = matches[0]
            with archive.open(member) as source:
                prefix = source.read(4)
            if prefix not in {
                b"II*\x00",
                b"MM\x00*",
                b"II+\x00",
                b"MM\x00+",
            }:
                raise GeoIngestError(
                    "GeoTIFF archive raster signature is invalid"
                )
            return member
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, GeoIngestError):
            raise
        raise GeoIngestError(
            "GeoTIFF archive could not be inspected"
        ) from error


def _bounded_positive_integer(value: Any, *, maximum: int, message: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise GeoIngestError(message)
    return value


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result or "0"


def _estimated_cog_bytes(inspection: RasterInspection) -> int:
    overhead = max(256 * 1024, inspection.uncompressed_bytes // 100)
    return math.ceil(inspection.uncompressed_bytes * 1.5) + overhead


def _preflight_store_capacity(store: ReferenceBlobStore, additional_bytes: int) -> None:
    try:
        with store._exclusive_lock():
            store._ensure_write_capacity(additional_bytes)
    except Exception as error:
        if isinstance(error, GeoIngestError):
            raise
        raise GeoIngestError("reference storage cannot stage normalized raster") from error


def _regular_file_fingerprint(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        metadata = Path(path).lstat()
    except OSError as error:
        raise GeoIngestError("local geodata artifact is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_size <= 0
    ):
        raise GeoIngestError("local geodata artifact is not a regular file")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _descriptor_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _snapshot_artifact(
    source_path: Path,
    *,
    destination: Path,
    max_bytes: int,
    expected_sha256: str | None,
    capacity_store: ReferenceBlobStore | None = None,
) -> _ArtifactSnapshot:
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise GeoIngestError("local geodata digest is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(source_path), flags)
    except OSError as error:
        raise GeoIngestError("local geodata artifact is unavailable") from error
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_size <= 0
            or initial.st_size > max_bytes
        ):
            raise GeoIngestError("local geodata artifact exceeds its safe limits")
        destination_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            destination_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            destination_flags |= os.O_NOFOLLOW
        try:
            output_descriptor = os.open(destination, destination_flags, 0o400)
        except OSError as error:
            raise GeoIngestError("private geodata snapshot could not be created") from error
        digest = hashlib.sha256()
        copied = 0
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise GeoIngestError("local geodata artifact exceeds its safe limits")
                digest.update(chunk)
                if capacity_store is not None:
                    try:
                        destination.relative_to(capacity_store.root)
                        with capacity_store._exclusive_lock():
                            capacity_store._ensure_write_capacity(len(chunk))
                    except Exception as error:
                        raise GeoIngestError(
                            "reference storage cannot stage source raster"
                        ) from error
                written = 0
                while written < len(chunk):
                    count = os.write(output_descriptor, chunk[written:])
                    if count <= 0:
                        raise OSError("snapshot write made no progress")
                    written += count
            os.fchmod(output_descriptor, 0o400)
            os.fsync(output_descriptor)
        except GeoIngestError:
            raise
        except OSError as error:
            raise GeoIngestError("private geodata snapshot could not be written") from error
        finally:
            os.close(output_descriptor)
        final = os.fstat(descriptor)
        if (
            _descriptor_fingerprint(initial) != _descriptor_fingerprint(final)
            or copied != initial.st_size
        ):
            raise GeoIngestError("local geodata artifact changed while being copied")
        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise GeoIngestError("local geodata bytes do not match their digest")
        return _ArtifactSnapshot(destination, actual_sha256, copied)
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def _install_table_guards(db: Session, table_name: str) -> None:
    row_trigger, truncate_trigger = _immutable_trigger_names(table_name)
    db.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION reference_data.reject_immutable_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'immutable reference data table'
                    USING ERRCODE = '55000';
            END;
            $$
            """
        )
    )
    db.execute(
        text(
            f"""
            CREATE TRIGGER {row_trigger}
            BEFORE INSERT OR UPDATE OR DELETE ON reference_data.{table_name}
            FOR EACH STATEMENT
            EXECUTE FUNCTION reference_data.reject_immutable_mutation()
            """
        )
    )
    db.execute(
        text(
            f"""
            CREATE TRIGGER {truncate_trigger}
            BEFORE TRUNCATE ON reference_data.{table_name}
            FOR EACH STATEMENT
            EXECUTE FUNCTION reference_data.reject_immutable_mutation()
            """
        )
    )


def _immutable_trigger_names(table_name: str) -> tuple[str, str]:
    identity = hashlib.sha256(table_name.encode("ascii")).hexdigest()[:20]
    return f"ri_{identity}_rows", f"ri_{identity}_truncate"


def _table_has_immutable_guards(db: Session, table_name: str) -> bool:
    if _IDENTIFIER_RE.fullmatch(table_name) is None:
        return False
    row_trigger, truncate_trigger = _immutable_trigger_names(table_name)
    count = db.execute(
        text(
            """
            SELECT count(*)::integer
            FROM pg_trigger AS trigger
            JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
            JOIN pg_namespace AS relation_namespace
              ON relation_namespace.oid = relation.relnamespace
            JOIN pg_proc AS trigger_function
              ON trigger_function.oid = trigger.tgfoid
            JOIN pg_namespace AS function_namespace
              ON function_namespace.oid = trigger_function.pronamespace
            WHERE relation_namespace.nspname = 'reference_data'
              AND relation.relname = :table_name
              AND NOT trigger.tgisinternal
              AND trigger.tgname IN (:row_trigger, :truncate_trigger)
              AND function_namespace.nspname = 'reference_data'
              AND trigger_function.proname = 'reject_immutable_mutation'
            """
        ),
        {
            "table_name": table_name,
            "row_trigger": row_trigger,
            "truncate_trigger": truncate_trigger,
        },
    ).scalar_one()
    return count == 2


def _vector_table_schema(
    db: Session,
    table_name: str,
    *,
    schema_name: str = _VECTOR_DATA_SCHEMA,
) -> dict[str, Any]:
    """Return a bounded semantic fingerprint of the normalized table."""

    if (
        schema_name not in {_VECTOR_DATA_SCHEMA, _VECTOR_STAGING_SCHEMA}
        or _IDENTIFIER_RE.fullmatch(table_name) is None
    ):
        raise GeoIngestError("vector table schema identity is invalid")
    rows = db.execute(
        text(
            """
            SELECT
                attribute.attname AS name,
                pg_catalog.format_type(
                    attribute.atttypid,
                    attribute.atttypmod
                ) AS data_type,
                attribute.attnotnull AS not_null
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS relation
              ON relation.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = :schema_name
              AND relation.relname = :table_name
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            ORDER BY attribute.attnum
            """
        ),
        {"schema_name": schema_name, "table_name": table_name},
    ).mappings().all()
    if not 2 <= len(rows) <= 512:
        raise GeoIngestError("vector table schema is outside its safe limits")
    columns: list[dict[str, Any]] = []
    for row in rows:
        name = row["name"]
        data_type = row["data_type"]
        not_null = row["not_null"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 255
            or not isinstance(data_type, str)
            or not 1 <= len(data_type) <= 255
            or not isinstance(not_null, bool)
        ):
            raise GeoIngestError("vector table schema is invalid")
        columns.append(
            {
                "name": name,
                "data_type": data_type,
                "not_null": not_null,
            }
        )
    return {
        "schema_version": "reference-vector-schema/v1",
        "columns": columns,
    }


def _require_mbtiles_schema(connection: sqlite3.Connection) -> None:
    objects = connection.execute(
        "SELECT name, type, tbl_name FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    allowed_objects = {
        ("metadata", "table", "metadata"),
        ("tiles", "table", "tiles"),
        ("tile_index", "index", "tiles"),
    }
    required_tables = {
        ("metadata", "table", "metadata"),
        ("tiles", "table", "tiles"),
    }
    if any(
        row not in allowed_objects
        for row in objects
    ) or not required_tables <= set(objects):
        raise GeoIngestError("tile archive schema is unsupported")
    metadata_columns = connection.execute("PRAGMA table_info(metadata)").fetchall()
    tile_columns = connection.execute("PRAGMA table_info(tiles)").fetchall()
    if [row[1:3] for row in metadata_columns] != [
        ("name", "TEXT"),
        ("value", "TEXT"),
    ] or [row[1:3] for row in tile_columns] != [
        ("zoom_level", "INTEGER"),
        ("tile_column", "INTEGER"),
        ("tile_row", "INTEGER"),
        ("tile_data", "BLOB"),
    ]:
        raise GeoIngestError("tile archive columns are incomplete")
    indexes = connection.execute("PRAGMA index_list(tiles)").fetchall()
    canonical = next((row for row in indexes if row[1] == "tile_index"), None)
    if canonical is None or canonical[2] != 1 or canonical[4] != 0:
        raise GeoIngestError("tile archive lacks its canonical unique index")
    index_columns = connection.execute("PRAGMA index_xinfo(tile_index)").fetchall()
    key_columns = [row for row in index_columns if row[5] == 1]
    if (
        [(row[2], row[3], row[4]) for row in key_columns]
        != [
            ("zoom_level", 0, "BINARY"),
            ("tile_column", 0, "BINARY"),
            ("tile_row", 0, "BINARY"),
        ]
    ):
        raise GeoIngestError("tile archive canonical index is malformed")


_REQUIRED_MBTILES_METADATA = {
    "name",
    "type",
    "version",
    "description",
    "format",
    "bounds",
    "minzoom",
    "maxzoom",
    "scheme",
    "coordinate_sha256",
}
_OPTIONAL_MBTILES_METADATA = {
    "attribution",
    "center",
    "json",
    "coordinate_hash_schema",
    "source_definition_sha256",
}


def _validated_mbtiles_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT name, value FROM metadata ORDER BY name").fetchall()
    metadata: dict[str, str] = {}
    for name, value in rows:
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or name in metadata
            or name not in _REQUIRED_MBTILES_METADATA | _OPTIONAL_MBTILES_METADATA
            or not 1 <= len(name) <= 128
            or len(value) > 4096
            or "\x00" in value
        ):
            raise GeoIngestError("tile archive metadata is malformed")
        metadata[name] = value
    if not _REQUIRED_MBTILES_METADATA <= metadata.keys():
        raise GeoIngestError("tile archive metadata is incomplete")
    if (
        not 1 <= len(metadata["name"].strip()) <= 255
        or not 1 <= len(metadata["description"].strip()) <= 1024
        or metadata["type"] not in {"baselayer", "overlay"}
        or metadata["version"] != "1.3"
        or metadata["format"] not in {"png", "jpg"}
        or metadata["scheme"] != "tms"
        or _SHA256_RE.fullmatch(metadata["coordinate_sha256"]) is None
    ):
        raise GeoIngestError("tile archive metadata is invalid")
    coordinate_hash_schema = metadata.get("coordinate_hash_schema")
    if coordinate_hash_schema is not None and (
        coordinate_hash_schema != "xyz-z-x-y-newline-v1"
    ):
        raise GeoIngestError("tile archive coordinate hash schema is invalid")
    source_definition_sha256 = metadata.get("source_definition_sha256")
    if source_definition_sha256 is not None and (
        _SHA256_RE.fullmatch(source_definition_sha256) is None
    ):
        raise GeoIngestError("tile archive source definition digest is invalid")
    try:
        min_zoom = int(metadata["minzoom"])
        max_zoom = int(metadata["maxzoom"])
    except ValueError as error:
        raise GeoIngestError("tile archive zoom metadata is invalid") from error
    if (
        metadata["minzoom"] != str(min_zoom)
        or metadata["maxzoom"] != str(max_zoom)
        or not 0 <= min_zoom <= max_zoom <= 22
    ):
        raise GeoIngestError("tile archive zoom metadata is invalid")
    _metadata_bounds(metadata["bounds"])
    return metadata


def _require_indexed_tile_lookup(connection: sqlite3.Connection, zoom: int) -> None:
    plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT tile_data FROM tiles "
        "WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
        (zoom, 0, 0),
    ).fetchall()
    descriptions = [str(row[3]) for row in plan if len(row) > 3]
    if not any("USING INDEX tile_index" in item for item in descriptions):
        raise GeoIngestError("tile archive lookup does not use its canonical index")


def _decode_tile_image(body: bytes, image_format: str) -> None:
    expected_format = "PNG" if image_format == "png" else "JPEG"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                if image.format != expected_format or image.size != (256, 256):
                    raise GeoIngestError("tile archive decoded image is inconsistent")
                image.verify()
            with Image.open(BytesIO(body)) as image:
                if image.format != expected_format or image.size != (256, 256):
                    raise GeoIngestError("tile archive decoded image is inconsistent")
                image.load()
    except GeoIngestError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise GeoIngestError("tile archive image could not be decoded safely") from error


def _unique_object(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise GeoIngestError("could not verify local geodata bytes") from error
    return digest.hexdigest()


def _postgis_extent(value: Any) -> dict[str, float]:
    if not isinstance(value, str):
        raise GeoIngestError("vector extent is unavailable")
    match = re.fullmatch(
        r"BOX\((-?[0-9]+(?:\.[0-9]+)?) (-?[0-9]+(?:\.[0-9]+)?),"
        r"(-?[0-9]+(?:\.[0-9]+)?) (-?[0-9]+(?:\.[0-9]+)?)\)",
        value,
        re.ASCII,
    )
    if match is None:
        raise GeoIngestError("vector extent is malformed")
    return _validated_bounds(*(float(item) for item in match.groups()))


def _metadata_bounds(value: Any) -> dict[str, float]:
    if not isinstance(value, str):
        raise GeoIngestError("tile archive bounds are unavailable")
    parts = value.split(",")
    if len(parts) != 4:
        raise GeoIngestError("tile archive bounds are invalid")
    try:
        numbers = [float(item) for item in parts]
    except ValueError as error:
        raise GeoIngestError("tile archive bounds are invalid") from error
    return _validated_bounds(*numbers)


def _wgs84_extent(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise GeoIngestError("raster WGS84 extent is unavailable")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise GeoIngestError("raster WGS84 extent is malformed")
    points: list[tuple[float, float]] = []
    for ring in coordinates:
        if not isinstance(ring, list):
            raise GeoIngestError("raster WGS84 extent is malformed")
        for point in ring:
            if (
                not isinstance(point, list)
                or len(point) < 2
                or isinstance(point[0], bool)
                or isinstance(point[1], bool)
                or not isinstance(point[0], (int, float))
                or not isinstance(point[1], (int, float))
            ):
                raise GeoIngestError("raster WGS84 extent is malformed")
            points.append((float(point[0]), float(point[1])))
    if not points or len(points) > 10_000:
        raise GeoIngestError("raster WGS84 extent is malformed")
    return _validated_bounds(
        min(item[0] for item in points),
        min(item[1] for item in points),
        max(item[0] for item in points),
        max(item[1] for item in points),
    )


def _validated_bounds(
    west: float,
    south: float,
    east: float,
    north: float,
) -> dict[str, float]:
    if (
        not -180 <= west < east <= 180
        or not -90 <= south < north <= 90
    ):
        raise GeoIngestError("geodata bounds are outside WGS84")
    return {"west": west, "south": south, "east": east, "north": north}
