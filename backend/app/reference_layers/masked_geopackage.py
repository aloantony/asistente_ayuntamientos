"""Deterministic whole-feature selection from a reviewed GeoPackage and mask."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from typing import Any
from uuid import uuid4

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    StoredReferenceBlob,
)
from app.reference_layers.geo_ingest import (
    GeoCommandResult,
    GeoIngestError,
    run_geo_command,
    strict_geo_command_environment,
)
from app.reference_layers.safe_download import normalize_https_url


TRANSFORM_SCHEMA = "reference-masked-geopackage/v1"
MASK_IDENTITY_SCHEMA = "ign-administrative-mask-geometry/v1"
DERIVATION_SCHEMA = "reference-masked-geopackage-derivation/v1"
DERIVATION_ALGORITHM = (
    "gdal-sqlite-gpkg-rtree-prefilter-st-intersects-"
    "preserve-whole-source-features-"
    "deterministic-gpkg/v2"
)
_DETERMINISTIC_LAST_CHANGE = "1970-01-01T00:00:00.000Z"
_OGR2OGR = "/usr/bin/ogr2ogr"
_OGRINFO = "/usr/bin/ogrinfo"
_MASK_LAYER = "__reference_spatial_mask"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LAYER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,254}$", re.ASCII)
_GRID_SOURCE_LAYER_RE = re.compile(
    r"^grid_(1|2|5|20|50|100)km_surf$",
    re.ASCII,
)
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$", re.ASCII)
_SQL_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.:-]{0,254}$",
    re.ASCII,
)
_MAX_MASK_BYTES = 64 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_COORDINATE_PAIRS = 1_000_000
_WORKSPACE_NAME_RE = re.compile(
    r"^masked-geopackage-[0-9a-f]{32}$",
    re.ASCII,
)

GeoRunner = Callable[
    [Sequence[str], Mapping[str, str], int],
    GeoCommandResult,
]


class MaskedGeoPackageError(ValueError):
    """A reviewed spatial derivation is malformed or no longer equivalent."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MaskedGeoPackageSpec:
    mask_url: str
    mask_media_type: str
    mask_max_bytes: int
    mask_identity_sha256: str
    source_layer: str
    output_layer: str
    selected_fields: tuple[str, ...]
    identifier_field: str
    cell_size_meters: int
    expected_feature_count: int
    expected_identifier_sha256: str
    source_crs: str
    mask_target_crs: str
    predicate: str
    geometry_mode: str

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": TRANSFORM_SCHEMA,
            "mask_url": self.mask_url,
            "mask_media_type": self.mask_media_type,
            "mask_max_bytes": self.mask_max_bytes,
            "mask_identity_sha256": self.mask_identity_sha256,
            "source_layer": self.source_layer,
            "output_layer": self.output_layer,
            "selected_fields": list(self.selected_fields),
            "identifier_field": self.identifier_field,
            "cell_size_meters": self.cell_size_meters,
            "expected_feature_count": self.expected_feature_count,
            "expected_identifier_sha256": (
                self.expected_identifier_sha256
            ),
            "source_crs": self.source_crs,
            "mask_target_crs": self.mask_target_crs,
            "predicate": self.predicate,
            "geometry_mode": self.geometry_mode,
        }


@dataclass(frozen=True)
class MaskIdentity:
    sha256: str
    feature_id: str
    properties: dict[str, Any]
    coordinate_pairs: int

    def metadata(self) -> dict[str, Any]:
        return {
            "schema": MASK_IDENTITY_SCHEMA,
            "sha256": self.sha256,
            "feature_id": self.feature_id,
            "properties": dict(self.properties),
            "geometry_type": "MultiPolygon",
            "coordinate_pairs": self.coordinate_pairs,
        }


@dataclass(frozen=True)
class MaskedGeoPackageResult:
    blob: StoredReferenceBlob
    feature_count: int
    identifier_sha256: str
    validation: dict[str, Any]


def parse_masked_geopackage_spec(
    config: Mapping[str, Any],
) -> MaskedGeoPackageSpec | None:
    raw = config.get("vector_transform")
    if raw is None:
        return None
    expected_keys = {
        "schema",
        "mask_url",
        "mask_media_type",
        "mask_max_bytes",
        "mask_identity_sha256",
        "source_layer",
        "output_layer",
        "selected_fields",
        "identifier_field",
        "cell_size_meters",
        "expected_feature_count",
        "expected_identifier_sha256",
        "source_crs",
        "mask_target_crs",
        "predicate",
        "geometry_mode",
    }
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise MaskedGeoPackageError(
            "masked GeoPackage transform is incomplete",
            code="masked_geopackage_config_invalid",
        )
    if raw.get("schema") != TRANSFORM_SCHEMA:
        raise MaskedGeoPackageError(
            "masked GeoPackage transform schema is unsupported",
            code="masked_geopackage_config_invalid",
        )
    try:
        mask_url = normalize_https_url(raw["mask_url"])
    except (TypeError, ValueError) as error:
        raise MaskedGeoPackageError(
            "masked GeoPackage mask URL is unsafe",
            code="masked_geopackage_config_invalid",
        ) from error
    mask_media_type = raw.get("mask_media_type")
    mask_max_bytes = raw.get("mask_max_bytes")
    mask_identity_sha256 = raw.get("mask_identity_sha256")
    source_layer = raw.get("source_layer")
    output_layer = raw.get("output_layer")
    selected_fields = raw.get("selected_fields")
    identifier_field = raw.get("identifier_field")
    cell_size_meters = raw.get("cell_size_meters")
    expected_feature_count = raw.get("expected_feature_count")
    expected_identifier_sha256 = raw.get("expected_identifier_sha256")
    source_layer_match = (
        _GRID_SOURCE_LAYER_RE.fullmatch(source_layer)
        if isinstance(source_layer, str)
        else None
    )
    if (
        mask_media_type != "application/json"
        or isinstance(mask_max_bytes, bool)
        or not isinstance(mask_max_bytes, int)
        or not 1024 <= mask_max_bytes <= _MAX_MASK_BYTES
        or not isinstance(mask_identity_sha256, str)
        or _SHA256_RE.fullmatch(mask_identity_sha256) is None
        or not isinstance(source_layer, str)
        or _LAYER_RE.fullmatch(source_layer) is None
        or source_layer_match is None
        or source_layer == _MASK_LAYER
        or not isinstance(output_layer, str)
        or _LAYER_RE.fullmatch(output_layer) is None
        or output_layer != f"{source_layer}_cyl"
        or not isinstance(selected_fields, list)
        or not 1 <= len(selected_fields) <= 32
        or any(
            not isinstance(item, str)
            or _FIELD_RE.fullmatch(item) is None
            for item in selected_fields
        )
        or len(selected_fields) != len(set(selected_fields))
        or selected_fields != ["GRD_ID", "X_LLC", "Y_LLC"]
        or not isinstance(identifier_field, str)
        or identifier_field != "GRD_ID"
        or isinstance(cell_size_meters, bool)
        or cell_size_meters
        not in {1_000, 2_000, 5_000, 20_000, 50_000, 100_000}
        or source_layer_match is None
        or int(source_layer_match.group(1)) * 1_000
        != cell_size_meters
        or isinstance(expected_feature_count, bool)
        or not isinstance(expected_feature_count, int)
        or not 1 <= expected_feature_count <= 1_000_000
        or not isinstance(expected_identifier_sha256, str)
        or _SHA256_RE.fullmatch(expected_identifier_sha256) is None
        or raw.get("source_crs") != "EPSG:3035"
        or raw.get("mask_target_crs") != "EPSG:3035"
        or raw.get("predicate") != "intersects"
        or raw.get("geometry_mode")
        != "preserve-whole-source-features"
    ):
        raise MaskedGeoPackageError(
            "masked GeoPackage transform values are invalid",
            code="masked_geopackage_config_invalid",
        )
    return MaskedGeoPackageSpec(
        mask_url=mask_url,
        mask_media_type=mask_media_type,
        mask_max_bytes=mask_max_bytes,
        mask_identity_sha256=mask_identity_sha256,
        source_layer=source_layer,
        output_layer=output_layer,
        selected_fields=tuple(selected_fields),
        identifier_field=identifier_field,
        cell_size_meters=cell_size_meters,
        expected_feature_count=expected_feature_count,
        expected_identifier_sha256=expected_identifier_sha256,
        source_crs="EPSG:3035",
        mask_target_crs="EPSG:3035",
        predicate="intersects",
        geometry_mode="preserve-whole-source-features",
    )


def validate_reviewed_mask(
    path: Path,
    size_bytes: int,
    spec: MaskedGeoPackageSpec,
) -> MaskIdentity:
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 1 <= size_bytes <= spec.mask_max_bytes
    ):
        raise MaskedGeoPackageError(
            "reviewed spatial mask size is invalid",
            code="spatial_mask_invalid",
        )
    try:
        body = path.read_bytes()
    except OSError as error:
        raise MaskedGeoPackageError(
            "reviewed spatial mask could not be read",
            code="spatial_mask_invalid",
        ) from error
    if len(body) != size_bytes:
        raise MaskedGeoPackageError(
            "reviewed spatial mask size changed",
            code="spatial_mask_invalid",
        )
    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(value)
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise MaskedGeoPackageError(
            "reviewed spatial mask is not strict JSON",
            code="spatial_mask_invalid",
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("type") != "FeatureCollection"
        or document.get("numberMatched") != 1
        or document.get("numberReturned") != 1
        or not isinstance(document.get("features"), list)
        or len(document["features"]) != 1
    ):
        raise MaskedGeoPackageError(
            "reviewed spatial mask is not one exact feature",
            code="spatial_mask_invalid",
        )
    feature = document["features"][0]
    if (
        not isinstance(feature, dict)
        or set(feature) != {"type", "properties", "id", "geometry"}
        or feature.get("type") != "Feature"
        or not isinstance(feature.get("properties"), dict)
        or not isinstance(feature.get("geometry"), dict)
        or set(feature["geometry"]) != {"type", "coordinates"}
        or feature["geometry"].get("type") != "MultiPolygon"
        or isinstance(feature.get("id"), bool)
        or not isinstance(feature.get("id"), (str, int))
    ):
        raise MaskedGeoPackageError(
            "reviewed spatial mask feature is malformed",
            code="spatial_mask_invalid",
        )
    properties = feature["properties"]
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, (str, int, float, type(None)))
        or isinstance(value, bool)
        or (isinstance(value, float) and not math.isfinite(value))
        for key, value in properties.items()
    ):
        raise MaskedGeoPackageError(
            "reviewed spatial mask properties are invalid",
            code="spatial_mask_invalid",
        )
    coordinate_pairs = _validate_multipolygon_coordinates(
        feature["geometry"]["coordinates"]
    )
    identity = {
        "schema": MASK_IDENTITY_SCHEMA,
        "type": "Feature",
        "id": str(feature["id"]),
        "properties": {
            key: properties[key] for key in sorted(properties)
        },
        "geometry": feature["geometry"],
    }
    digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    if digest != spec.mask_identity_sha256:
        raise MaskedGeoPackageError(
            "reviewed spatial mask identity changed",
            code="spatial_mask_changed",
        )
    return MaskIdentity(
        sha256=digest,
        feature_id=str(feature["id"]),
        properties={
            key: properties[key] for key in sorted(properties)
        },
        coordinate_pairs=coordinate_pairs,
    )


def derive_masked_geopackage(
    store: ReferenceBlobStore,
    *,
    workspace_store: ReferenceBlobStore,
    source_blob: StoredReferenceBlob,
    mask_blob: StoredReferenceBlob,
    mask_identity: MaskIdentity,
    spec: MaskedGeoPackageSpec,
    max_output_bytes: int,
    timeout_seconds: int,
    runner: GeoRunner = run_geo_command,
) -> MaskedGeoPackageResult:
    if (
        source_blob.storage_backend != "filesystem"
        or mask_blob.storage_backend != "filesystem"
    ):
        raise MaskedGeoPackageError(
            "masked GeoPackage derivation inputs are invalid",
            code="masked_geopackage_derivation_invalid",
        )
    return derive_masked_geopackage_files(
        store,
        workspace_store=workspace_store,
        source_path=store.resolve_blob(source_blob.storage_key),
        source_sha256=source_blob.sha256,
        source_size_bytes=source_blob.size_bytes,
        mask_path=store.resolve_blob(mask_blob.storage_key),
        mask_sha256=mask_blob.sha256,
        mask_size_bytes=mask_blob.size_bytes,
        mask_identity=mask_identity,
        spec=spec,
        max_output_bytes=max_output_bytes,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )


def derive_masked_geopackage_files(
    store: ReferenceBlobStore,
    *,
    workspace_store: ReferenceBlobStore,
    source_path: Path,
    source_sha256: str,
    source_size_bytes: int,
    mask_path: Path,
    mask_sha256: str,
    mask_size_bytes: int,
    mask_identity: MaskIdentity,
    spec: MaskedGeoPackageSpec,
    max_output_bytes: int,
    timeout_seconds: int,
    runner: GeoRunner = run_geo_command,
) -> MaskedGeoPackageResult:
    if (
        not isinstance(source_path, Path)
        or not isinstance(mask_path, Path)
        or not isinstance(source_sha256, str)
        or _SHA256_RE.fullmatch(source_sha256) is None
        or not isinstance(mask_sha256, str)
        or _SHA256_RE.fullmatch(mask_sha256) is None
        or isinstance(source_size_bytes, bool)
        or not isinstance(source_size_bytes, int)
        or not 1 <= source_size_bytes <= store.max_blob_bytes
        or isinstance(mask_size_bytes, bool)
        or not isinstance(mask_size_bytes, int)
        or not 1 <= mask_size_bytes <= store.max_blob_bytes
        or mask_identity.sha256 != spec.mask_identity_sha256
        or isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1024 <= max_output_bytes <= store.max_blob_bytes
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 86_400
        or not isinstance(workspace_store, ReferenceBlobStore)
        or workspace_store.read_only
        or workspace_store.root == store.root
        or store.root in workspace_store.root.parents
        or workspace_store.root in store.root.parents
    ):
        raise MaskedGeoPackageError(
            "masked GeoPackage derivation inputs are invalid",
            code="masked_geopackage_derivation_invalid",
        )
    workspace_additional = (
        source_size_bytes
        + mask_size_bytes
        + max_output_bytes
    )
    workspace_store.ensure_capacity(workspace_additional)
    store.ensure_capacity(max_output_bytes)
    try:
        with _masked_geopackage_workspace(
            workspace_store
        ) as private:
            source_copy = private / "source.gpkg"
            mask_copy = private / "mask.geojson"
            output = private / "selected.gpkg"
            _copy_regular_file(
                source_path,
                source_copy,
                expected_sha256=source_sha256,
                expected_size=source_size_bytes,
            )
            _copy_regular_file(
                mask_path,
                mask_copy,
                expected_sha256=mask_sha256,
                expected_size=mask_size_bytes,
            )
            source_geometry = _require_geopackage_layer(
                source_copy,
                spec.source_layer,
                expected_srs_id=3035,
            )
            source_spatial_index = _require_geopackage_spatial_index(
                source_copy,
                layer_name=spec.source_layer,
                geometry_name=source_geometry,
            )
            _require_feature_layer_absent(source_copy, _MASK_LAYER)
            environment = strict_geo_command_environment(
                cpl_tmpdir=private,
            )
            runner(
                [
                    _OGR2OGR,
                    "-if",
                    "GeoJSON",
                    "-f",
                    "GPKG",
                    "-update",
                    "-nln",
                    _MASK_LAYER,
                    "-t_srs",
                    spec.mask_target_crs,
                    str(source_copy),
                    str(mask_copy),
                ],
                environment,
                timeout_seconds,
            )
            mask_geometry = _require_geopackage_layer(
                source_copy,
                _MASK_LAYER,
                expected_srs_id=3035,
            )
            _require_exact_feature_count(
                source_copy,
                _MASK_LAYER,
                expected=1,
            )
            sql = _selection_sql(
                spec,
                source_geometry=source_geometry,
                mask_geometry=mask_geometry,
                source_spatial_index=source_spatial_index,
            )
            runner(
                [
                    _OGR2OGR,
                    "-if",
                    "GPKG",
                    "-f",
                    "GPKG",
                    "-unsetFid",
                    "-a_srs",
                    "EPSG:3035",
                    "-nln",
                    spec.output_layer,
                    "-lco",
                    "GEOMETRY_NAME=geom",
                    "-lco",
                    "FID=fid",
                    "-dialect",
                    "SQLite",
                    "-sql",
                    sql,
                    str(output),
                    str(source_copy),
                ],
                environment,
                timeout_seconds,
            )
            _normalize_derived_geopackage(
                output,
                layer_name=spec.output_layer,
            )
            geometry_validation = _validate_canonical_grid_geometry(
                output,
                spec=spec,
                environment=environment,
                timeout_seconds=timeout_seconds,
                runner=runner,
            )
            try:
                output_size = output.stat().st_size
            except OSError as error:
                raise MaskedGeoPackageError(
                    "masked GeoPackage output is missing",
                    code="masked_geopackage_derivation_invalid",
                ) from error
            if not 100 <= output_size <= max_output_bytes:
                raise MaskedGeoPackageError(
                    "masked GeoPackage output exceeds its bound",
                    code="masked_geopackage_derivation_limit",
                )
            inspection = _inspect_derived_geopackage(output, spec)
            blob = _commit_derived_output(
                store,
                output,
                max_bytes=max_output_bytes,
                expected_size=output_size,
            )
    except MaskedGeoPackageError:
        raise
    except (GeoIngestError, OSError, sqlite3.Error) as error:
        raise MaskedGeoPackageError(
            "masked GeoPackage derivation failed closed",
            code="masked_geopackage_derivation_invalid",
        ) from error
    validation = {
        "schema": DERIVATION_SCHEMA,
        "passed": True,
        "algorithm": DERIVATION_ALGORITHM,
        "source_sha256": source_sha256,
        "source_size_bytes": source_size_bytes,
        "mask_blob_sha256": mask_sha256,
        "mask_size_bytes": mask_size_bytes,
        "mask_identity": mask_identity.metadata(),
        "transform": spec.evidence(),
        "output_sha256": blob.sha256,
        "output_size_bytes": blob.size_bytes,
        "normalized_last_change": _DETERMINISTIC_LAST_CHANGE,
        "source_spatial_index": source_spatial_index,
        **inspection,
        **geometry_validation,
    }
    return MaskedGeoPackageResult(
        blob=blob,
        feature_count=inspection["feature_count"],
        identifier_sha256=inspection["identifier_sha256"],
        validation=validation,
    )


@contextmanager
def _masked_geopackage_workspace(
    workspace_store: ReferenceBlobStore,
):
    parent = workspace_store.root / "workspaces"
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink():
            raise OSError("transient workspace root is unsafe")
        os.chmod(parent, 0o700)
        if parent.resolve(strict=True).parent != workspace_store.root:
            raise OSError("transient workspace root escaped its store")
    except OSError as error:
        raise MaskedGeoPackageError(
            "transient derivation workspace is invalid",
            code="masked_geopackage_derivation_invalid",
        ) from error

    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_fd = -1
    workspace_fd = -1
    cleanup_error: OSError | None = None
    name = f"masked-geopackage-{uuid4().hex}"
    try:
        parent_fd = os.open(parent, directory_flags)
        _cleanup_masked_geopackage_workspaces(
            parent_fd,
            directory_flags=directory_flags,
        )
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        workspace_fd = os.open(
            name,
            directory_flags,
            dir_fd=parent_fd,
        )
        os.fchmod(workspace_fd, 0o700)
        fcntl.flock(
            workspace_fd,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
        yield parent / name
    except MaskedGeoPackageError:
        raise
    except OSError as error:
        raise MaskedGeoPackageError(
            "transient derivation workspace failed closed",
            code="masked_geopackage_derivation_invalid",
        ) from error
    finally:
        if parent_fd >= 0:
            try:
                shutil.rmtree(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
            except OSError as error:
                cleanup_error = error
        if workspace_fd >= 0:
            try:
                fcntl.flock(workspace_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(workspace_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        if cleanup_error is not None:
            raise MaskedGeoPackageError(
                "transient derivation workspace could not be removed",
                code="masked_geopackage_derivation_invalid",
            ) from cleanup_error


def cleanup_masked_geopackage_workspaces(
    workspace_store: ReferenceBlobStore,
) -> None:
    """Purge crash-orphaned workspaces without touching active derivations."""

    with _masked_geopackage_workspace(workspace_store):
        pass


def _cleanup_masked_geopackage_workspaces(
    parent_fd: int,
    *,
    directory_flags: int,
) -> None:
    for name in os.listdir(parent_fd):
        if _WORKSPACE_NAME_RE.fullmatch(name) is None:
            continue
        candidate_fd = -1
        try:
            candidate_fd = os.open(
                name,
                directory_flags,
                dir_fd=parent_fd,
            )
            try:
                fcntl.flock(
                    candidate_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                continue
            candidate = os.fstat(candidate_fd)
            current = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(candidate.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or candidate.st_dev != current.st_dev
                or candidate.st_ino != current.st_ino
            ):
                raise OSError("transient workspace identity changed")
            shutil.rmtree(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise MaskedGeoPackageError(
                "orphan transient workspace could not be purged",
                code="masked_geopackage_derivation_invalid",
            ) from error
        finally:
            if candidate_fd >= 0:
                os.close(candidate_fd)


def _commit_derived_output(
    store: ReferenceBlobStore,
    output: Path,
    *,
    max_bytes: int,
    expected_size: int,
) -> StoredReferenceBlob:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(output, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_size
        ):
            raise OSError("derived output identity is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            with store.stage(max_bytes=max_bytes) as staging:
                while True:
                    chunk = source.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    staging.write(chunk)
                after = os.fstat(descriptor)
                if (
                    before.st_dev != after.st_dev
                    or before.st_ino != after.st_ino
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise OSError(
                        "derived output changed while being persisted"
                    )
                return staging.commit(expected_size=expected_size)
    finally:
        os.close(descriptor)


def _unique_object(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_multipolygon_coordinates(value: Any) -> int:
    if not isinstance(value, list) or not value:
        raise MaskedGeoPackageError(
            "reviewed spatial mask geometry is empty",
            code="spatial_mask_invalid",
        )
    pairs = 0
    for polygon in value:
        if not isinstance(polygon, list) or not polygon:
            raise MaskedGeoPackageError(
                "reviewed spatial mask polygon is invalid",
                code="spatial_mask_invalid",
            )
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise MaskedGeoPackageError(
                    "reviewed spatial mask ring is invalid",
                    code="spatial_mask_invalid",
                )
            for point in ring:
                if (
                    not isinstance(point, list)
                    or len(point) != 2
                    or any(
                        isinstance(item, bool)
                        or not isinstance(item, (int, float))
                        or not math.isfinite(float(item))
                        for item in point
                    )
                    or not -180 <= float(point[0]) <= 180
                    or not -90 <= float(point[1]) <= 90
                ):
                    raise MaskedGeoPackageError(
                        "reviewed spatial mask coordinate is invalid",
                        code="spatial_mask_invalid",
                    )
                pairs += 1
                if pairs > _MAX_COORDINATE_PAIRS:
                    raise MaskedGeoPackageError(
                        "reviewed spatial mask has too many coordinates",
                        code="spatial_mask_invalid",
                    )
            if ring[0] != ring[-1]:
                raise MaskedGeoPackageError(
                    "reviewed spatial mask ring is not closed",
                    code="spatial_mask_invalid",
                )
    return pairs


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    try:
        source_metadata = source.lstat()
    except OSError as error:
        raise MaskedGeoPackageError(
            "derivation source artifact is unavailable",
            code="masked_geopackage_derivation_invalid",
        ) from error
    if (
        not stat.S_ISREG(source_metadata.st_mode)
        or source.is_symlink()
        or source_metadata.st_size != expected_size
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise MaskedGeoPackageError(
            "derivation source artifact is unsafe",
            code="masked_geopackage_derivation_invalid",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as reader:
            descriptor = os.open(destination, flags, 0o600)
            try:
                while True:
                    chunk = reader.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    written = 0
                    while written < len(view):
                        count = os.write(descriptor, view[written:])
                        if count <= 0:
                            raise OSError("copy made no progress")
                        written += count
                    digest.update(chunk)
                    copied += len(chunk)
                    if copied > expected_size:
                        raise OSError("copy exceeded expected size")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise MaskedGeoPackageError(
            "derivation source artifact could not be copied",
            code="masked_geopackage_derivation_invalid",
        ) from error
    if copied != expected_size or digest.hexdigest() != expected_sha256:
        raise MaskedGeoPackageError(
            "derivation source artifact failed revalidation",
            code="masked_geopackage_derivation_invalid",
        )


def _require_geopackage_layer(
    path: Path,
    layer_name: str,
    *,
    expected_srs_id: int,
) -> str:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        if connection.execute("PRAGMA application_id").fetchone() != (
            0x47504B47,
        ) or connection.execute("PRAGMA quick_check(1)").fetchone() != (
            "ok",
        ):
            raise MaskedGeoPackageError(
                "derivation GeoPackage failed integrity validation",
                code="masked_geopackage_derivation_invalid",
            )
        row = connection.execute(
            "SELECT contents.data_type, geometry.column_name, "
            "geometry.srs_id, srs.organization, "
            "srs.organization_coordsys_id, srs.definition "
            "FROM gpkg_contents AS contents "
            "JOIN gpkg_geometry_columns AS geometry "
            "ON geometry.table_name = contents.table_name "
            "JOIN gpkg_spatial_ref_sys AS srs "
            "ON srs.srs_id = geometry.srs_id "
            "WHERE contents.table_name = ?",
            (layer_name,),
        ).fetchone()
    if (
        row is None
        or row[0] != "features"
        or not isinstance(row[1], str)
        or _FIELD_RE.fullmatch(row[1]) is None
        or row[2] != expected_srs_id
        or row[3] != "EPSG"
        or row[4] != expected_srs_id
        or not _definition_identifies_epsg_3035(row[5])
    ):
        raise MaskedGeoPackageError(
            "derivation GeoPackage layer identity changed",
            code="masked_geopackage_derivation_invalid",
        )
    return row[1]


def _definition_identifies_epsg_3035(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 100_000:
        return False
    normalized = re.sub(r"\s+", "", value)
    return normalized.endswith(
        'AUTHORITY["EPSG","3035"]]'
    ) or normalized.endswith('ID["EPSG",3035]]')


def _require_feature_layer_absent(path: Path, layer_name: str) -> None:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        present = connection.execute(
            "SELECT 1 FROM gpkg_contents WHERE table_name = ?",
            (layer_name,),
        ).fetchone()
    if present is not None:
        raise MaskedGeoPackageError(
            "source GeoPackage uses the reserved mask layer name",
            code="masked_geopackage_derivation_invalid",
        )


def _require_geopackage_spatial_index(
    path: Path,
    *,
    layer_name: str,
    geometry_name: str,
) -> str:
    """Require a complete persisted GeoPackage RTree before a large mask."""

    spatial_index = f"rtree_{layer_name}_{geometry_name}"
    quoted_layer = _quote_identifier(layer_name)
    quoted_index = _quote_identifier(spatial_index)
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            extension = connection.execute(
                "SELECT 1 FROM gpkg_extensions "
                "WHERE table_name = ? AND column_name = ? "
                "AND extension_name = 'gpkg_rtree_index'",
                (layer_name, geometry_name),
            ).fetchone()
            index_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ? "
                "AND sql LIKE 'CREATE VIRTUAL TABLE%USING rtree%'",
                (spatial_index,),
            ).fetchone()
            source_count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_layer}"
            ).fetchone()
            index_count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_index}"
            ).fetchone()
            integrity = connection.execute(
                "SELECT rtreecheck(?)",
                (spatial_index,),
            ).fetchone()
    except (OSError, sqlite3.Error) as error:
        raise MaskedGeoPackageError(
            "source GeoPackage spatial index cannot be verified",
            code="masked_geopackage_spatial_index_invalid",
        ) from error
    if (
        extension != (1,)
        or index_table != (1,)
        or source_count is None
        or not isinstance(source_count[0], int)
        or source_count[0] < 1
        or index_count != source_count
        or integrity != ("ok",)
    ):
        raise MaskedGeoPackageError(
            "source GeoPackage spatial index is missing or incomplete",
            code="masked_geopackage_spatial_index_invalid",
        )
    return spatial_index


def _require_exact_feature_count(
    path: Path,
    layer_name: str,
    *,
    expected: int,
) -> None:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        count = connection.execute(
            f"SELECT count(*) FROM {_quote_identifier(layer_name)}"
        ).fetchone()
    if count != (expected,):
        raise MaskedGeoPackageError(
            "derived mask feature count is invalid",
            code="masked_geopackage_derivation_invalid",
        )


def _selection_sql(
    spec: MaskedGeoPackageSpec,
    *,
    source_geometry: str,
    mask_geometry: str,
    source_spatial_index: str,
) -> str:
    selected = ", ".join(
        f"source.{_quote_identifier(field)}" for field in spec.selected_fields
    )
    return (
        f"SELECT {selected}, "
        f"source.{_quote_identifier(source_geometry)} AS geom "
        f"FROM {_quote_identifier(spec.source_layer)} AS source, "
        f"{_quote_identifier(_MASK_LAYER)} AS mask "
        "WHERE source.ROWID IN ("
        f"SELECT spatial.id FROM {_quote_identifier(source_spatial_index)} "
        "AS spatial WHERE "
        f"spatial.maxx >= ST_MinX(mask.{_quote_identifier(mask_geometry)}) "
        f"AND spatial.minx <= ST_MaxX(mask.{_quote_identifier(mask_geometry)}) "
        f"AND spatial.maxy >= ST_MinY(mask.{_quote_identifier(mask_geometry)}) "
        f"AND spatial.miny <= ST_MaxY(mask.{_quote_identifier(mask_geometry)})"
        ") AND ST_Intersects("
        f"source.{_quote_identifier(source_geometry)}, "
        f"mask.{_quote_identifier(mask_geometry)}) "
        f"ORDER BY source.{_quote_identifier(spec.identifier_field)}"
    )


def _quote_identifier(value: str) -> str:
    if _SQL_IDENTIFIER_RE.fullmatch(value) is None:
        raise MaskedGeoPackageError(
            "derivation SQL identifier is invalid",
            code="masked_geopackage_config_invalid",
        )
    return '"' + value.replace('"', '""') + '"'


def _normalize_derived_geopackage(
    path: Path,
    *,
    layer_name: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise MaskedGeoPackageError(
            "derived GeoPackage output is missing",
            code="masked_geopackage_derivation_invalid",
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise MaskedGeoPackageError(
            "derived GeoPackage output is not a private regular file",
            code="masked_geopackage_derivation_invalid",
        )
    try:
        uri = path.absolute().as_uri() + "?mode=rw"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            updated = connection.execute(
                "UPDATE gpkg_contents SET last_change = ? "
                "WHERE table_name = ?",
                (_DETERMINISTIC_LAST_CHANGE, layer_name),
            )
            if updated.rowcount != 1:
                raise MaskedGeoPackageError(
                    "derived GeoPackage layer timestamp is ambiguous",
                    code="masked_geopackage_derivation_invalid",
                )
            connection.commit()
            if connection.execute(
                "PRAGMA quick_check(1)"
            ).fetchone() != ("ok",):
                raise MaskedGeoPackageError(
                    "normalized derived GeoPackage is corrupt",
                    code="masked_geopackage_derivation_invalid",
                )
    except MaskedGeoPackageError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise MaskedGeoPackageError(
            "derived GeoPackage metadata cannot be normalized",
            code="masked_geopackage_derivation_invalid",
        ) from error


def _validate_canonical_grid_geometry(
    path: Path,
    *,
    spec: MaskedGeoPackageSpec,
    environment: Mapping[str, str],
    timeout_seconds: int,
    runner: GeoRunner,
) -> dict[str, Any]:
    cell_size = spec.cell_size_meters
    expected_area = cell_size * cell_size
    layer = _quote_identifier(spec.output_layer)
    geometry = _quote_identifier("geom")
    identifier = _quote_identifier(spec.identifier_field)
    x_field = _quote_identifier("X_LLC")
    y_field = _quote_identifier("Y_LLC")
    expected_identifier = (
        f"'CRS3035RES{cell_size}mN' || "
        f"CAST({y_field} AS INTEGER) || 'E' || "
        f"CAST({x_field} AS INTEGER)"
    )
    sql = (
        "SELECT COUNT(*) AS invalid_geometry_count "
        f"FROM {layer} WHERE "
        f"{geometry} IS NULL OR COALESCE(ST_IsValid({geometry}), 0) <> 1 OR "
        f"GeometryType({geometry}) <> 'POLYGON' OR "
        f"{identifier} IS NULL OR typeof({identifier}) <> 'text' OR "
        f"{x_field} IS NULL OR "
        f"typeof({x_field}) NOT IN ('integer', 'real') OR "
        f"{x_field} <> CAST({x_field} AS INTEGER) OR "
        f"{y_field} IS NULL OR "
        f"typeof({y_field}) NOT IN ('integer', 'real') OR "
        f"{y_field} <> CAST({y_field} AS INTEGER) OR "
        f"{identifier} <> ({expected_identifier}) OR "
        f"ST_MinX({geometry}) IS NULL OR "
        f"ST_MinY({geometry}) IS NULL OR "
        f"ST_MaxX({geometry}) IS NULL OR "
        f"ST_MaxY({geometry}) IS NULL OR "
        f"ST_Area({geometry}) IS NULL OR "
        f"ABS(ST_MinX({geometry}) - {x_field}) > 0.001 OR "
        f"ABS(ST_MinY({geometry}) - {y_field}) > 0.001 OR "
        f"ABS(ST_MaxX({geometry}) - ({x_field} + {cell_size})) "
        "> 0.001 OR "
        f"ABS(ST_MaxY({geometry}) - ({y_field} + {cell_size})) "
        "> 0.001 OR "
        f"ABS(ST_Area({geometry}) - {expected_area}) > 1"
    )
    result = runner(
        [
            _OGRINFO,
            "-ro",
            "-json",
            "-features",
            str(path),
            "-dialect",
            "SQLite",
            "-sql",
            sql,
        ],
        environment,
        timeout_seconds,
    )
    try:
        document = json.loads(
            result.stdout.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(value)
            ),
        )
        layers = document["layers"]
        features = layers[0]["features"]
        properties = features[0]["properties"]
    except (
        KeyError,
        IndexError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise MaskedGeoPackageError(
            "canonical grid geometry evidence is malformed",
            code="masked_geopackage_derivation_invalid",
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("driverShortName") != "GPKG"
        or not isinstance(layers, list)
        or len(layers) != 1
        or layers[0].get("name") != "SELECT"
        or layers[0].get("featureCount") != 1
        or not isinstance(features, list)
        or len(features) != 1
        or not isinstance(properties, dict)
        or set(properties) != {"invalid_geometry_count"}
        or isinstance(properties["invalid_geometry_count"], bool)
        or properties["invalid_geometry_count"] != 0
    ):
        raise MaskedGeoPackageError(
            "derived grid geometry differs from its canonical cells",
            code="masked_geopackage_parity_failed",
        )
    return {
        "canonical_grid_geometry": True,
        "cell_size_meters": cell_size,
        "invalid_geometry_count": 0,
        "identifier_coordinate_binding": True,
    }


def _inspect_derived_geopackage(
    path: Path,
    spec: MaskedGeoPackageSpec,
) -> dict[str, Any]:
    geometry_column = _require_geopackage_layer(
        path,
        spec.output_layer,
        expected_srs_id=3035,
    )
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        feature_layers = connection.execute(
            "SELECT table_name FROM gpkg_contents "
            "WHERE data_type = 'features' ORDER BY table_name"
        ).fetchall()
        if feature_layers != [(spec.output_layer,)]:
            raise MaskedGeoPackageError(
                "derived GeoPackage contains unexpected feature layers",
                code="masked_geopackage_derivation_invalid",
            )
        columns = [
            row[1]
            for row in connection.execute(
                f"PRAGMA table_info({_quote_identifier(spec.output_layer)})"
            )
        ]
        expected_columns = {"fid", geometry_column, *spec.selected_fields}
        if set(columns) != expected_columns or len(columns) != len(
            expected_columns
        ):
            raise MaskedGeoPackageError(
                "derived GeoPackage fields do not match the allowlist",
                code="masked_geopackage_derivation_invalid",
            )
        feature_identities = [
            row
            for row in connection.execute(
                f"SELECT {_quote_identifier(spec.identifier_field)}, "
                f"{_quote_identifier('X_LLC')}, "
                f"{_quote_identifier('Y_LLC')} "
                f"FROM {_quote_identifier(spec.output_layer)} "
                f"ORDER BY {_quote_identifier(spec.identifier_field)} "
                "COLLATE BINARY"
            )
        ]
        bounds = connection.execute(
            "SELECT min_x, min_y, max_x, max_y FROM gpkg_contents "
            "WHERE table_name = ?",
            (spec.output_layer,),
        ).fetchone()
    if (
        len(feature_identities) != spec.expected_feature_count
        or any(
            not _valid_grid_identity_row(
                row,
                cell_size=spec.cell_size_meters,
            )
            for row in feature_identities
        )
    ):
        raise MaskedGeoPackageError(
            "derived GeoPackage feature identity count changed",
            code="masked_geopackage_parity_failed",
        )
    identifiers = [row[0] for row in feature_identities]
    if len(set(identifiers)) != len(identifiers):
        raise MaskedGeoPackageError(
            "derived GeoPackage feature identities are duplicated",
            code="masked_geopackage_parity_failed",
        )
    identifier_sha256 = hashlib.sha256(
        ("\n".join(identifiers) + "\n").encode("utf-8")
    ).hexdigest()
    if identifier_sha256 != spec.expected_identifier_sha256:
        raise MaskedGeoPackageError(
            "derived GeoPackage identifiers differ from reviewed parity",
            code="masked_geopackage_parity_failed",
        )
    if (
        bounds is None
        or len(bounds) != 4
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in bounds
        )
        or not float(bounds[0]) < float(bounds[2])
        or not float(bounds[1]) < float(bounds[3])
    ):
        raise MaskedGeoPackageError(
            "derived GeoPackage bounds are invalid",
            code="masked_geopackage_derivation_invalid",
        )
    return {
        "feature_count": len(identifiers),
        "identifier_field": spec.identifier_field,
        "identifier_sha256": identifier_sha256,
        "selected_fields": list(spec.selected_fields),
        "source_crs": spec.source_crs,
        "bounds": {
            "min_x": float(bounds[0]),
            "min_y": float(bounds[1]),
            "max_x": float(bounds[2]),
            "max_y": float(bounds[3]),
        },
        "whole_source_features_preserved": True,
        "population_fields_excluded": True,
    }


def _valid_grid_identity_row(
    row: tuple[Any, ...],
    *,
    cell_size: int,
) -> bool:
    if len(row) != 3:
        return False
    identifier, x_value, y_value = row
    if (
        not isinstance(identifier, str)
        or not identifier
        or "\n" in identifier
        or "\r" in identifier
    ):
        return False
    coordinates: list[int] = []
    for value in (x_value, y_value):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) != int(value)
        ):
            return False
        coordinates.append(int(value))
    x_coordinate, y_coordinate = coordinates
    return identifier == (
        f"CRS3035RES{cell_size}mN"
        f"{y_coordinate}E{x_coordinate}"
    )


__all__ = [
    "DERIVATION_ALGORITHM",
    "DERIVATION_SCHEMA",
    "MASK_IDENTITY_SCHEMA",
    "TRANSFORM_SCHEMA",
    "MaskIdentity",
    "MaskedGeoPackageError",
    "MaskedGeoPackageResult",
    "MaskedGeoPackageSpec",
    "cleanup_masked_geopackage_workspaces",
    "derive_masked_geopackage",
    "derive_masked_geopackage_files",
    "parse_masked_geopackage_spec",
    "validate_reviewed_mask",
]
