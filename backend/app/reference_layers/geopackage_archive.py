"""Closed inspection of a GeoPackage stored in an untrusted ZIP archive."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import struct
import tempfile
from typing import Any
import zipfile


INSPECTION_SCHEMA = "reference-geopackage-zip-inspection/v1"
SAMPLE_SCHEMA = "reference-geopackage-content-sample/v1"
MAX_ARCHIVE_ENTRIES = 4_096
MAX_ARCHIVE_MEMBER_NAME_BYTES = 4_096
MAX_COMPRESSION_RATIO = 1_000
_GPKG_APPLICATION_ID = 0x47504B47
_SQLITE_HEADER = b"SQLite format 3\x00"
_ALLOWED_COMPRESSION = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
}


class GeoPackageArchiveError(RuntimeError):
    """The ZIP or its selected GeoPackage is unsafe or malformed."""


def inspect_geopackage_zip(
    path: Path,
    *,
    expected_member: str,
    expected_layer: str,
    maximum_uncompressed_bytes: int,
) -> dict[str, Any]:
    """Inspect one explicit member without trusting names or ZIP metadata."""

    source_path = Path(path)
    _expected_name(expected_member, "GeoPackage archive member")
    _expected_name(expected_layer, "GeoPackage feature layer")
    if (
        isinstance(maximum_uncompressed_bytes, bool)
        or not isinstance(maximum_uncompressed_bytes, int)
        or maximum_uncompressed_bytes < 100
    ):
        raise GeoPackageArchiveError(
            "GeoPackage archive byte limit is invalid"
        )
    try:
        if source_path.stat().st_size < 22:
            raise GeoPackageArchiveError("GeoPackage ZIP is too small")
        with zipfile.ZipFile(source_path) as archive:
            entries = archive.infolist()
            selected, total_uncompressed = _validated_entries(
                entries,
                expected_member=expected_member,
                maximum_uncompressed_bytes=maximum_uncompressed_bytes,
            )
            with tempfile.TemporaryDirectory(
                prefix="reference-gpkgzip-"
            ) as directory:
                extracted = Path(directory, "selected.gpkg")
                member_sha256 = _extract_member(
                    archive,
                    selected,
                    extracted,
                    maximum_bytes=maximum_uncompressed_bytes,
                )
                package = _inspect_geopackage(
                    extracted,
                    expected_layer=expected_layer,
                )
    except GeoPackageArchiveError:
        raise
    except (
        OSError,
        RuntimeError,
        sqlite3.Error,
        struct.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP could not be inspected safely"
        ) from error

    inspection = {
        "schema_version": INSPECTION_SCHEMA,
        "archive_sha256": _file_sha256(source_path),
        "archive_size_bytes": source_path.stat().st_size,
        "archive_entry_count": len(entries),
        "archive_uncompressed_bytes": total_uncompressed,
        "archive_member": selected.filename,
        "archive_member_crc32": f"{selected.CRC:08x}",
        "archive_member_size_bytes": selected.file_size,
        "archive_member_sha256": member_sha256,
        **package,
    }
    inspection["content_identity_sha256"] = canonical_json_sha256(
        {
            key: value
            for key, value in inspection.items()
            if key
            not in {
                "archive_sha256",
                "archive_size_bytes",
                "archive_entry_count",
                "archive_uncompressed_bytes",
            }
        }
    )
    return inspection


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP could not be fingerprinted"
        ) from error
    return digest.hexdigest()


def geopackage_vsi_path(path: Path, *, expected_member: str) -> str:
    """Return a GDAL /vsizip path after revalidating the archive identity."""

    _expected_name(expected_member, "GeoPackage archive member")
    source_path = Path(path)
    try:
        with zipfile.ZipFile(source_path) as archive:
            selected, _ = _validated_entries(
                archive.infolist(),
                expected_member=expected_member,
                maximum_uncompressed_bytes=2**63 - 1,
            )
    except GeoPackageArchiveError:
        raise
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP could not be inspected safely"
        ) from error
    return f"/vsizip/{source_path.as_posix()}/{selected.filename}"


def canonical_json_sha256(value: Any) -> str:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise GeoPackageArchiveError(
            "GeoPackage inspection evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(body).hexdigest()


def _validated_entries(
    entries: list[zipfile.ZipInfo],
    *,
    expected_member: str,
    maximum_uncompressed_bytes: int,
) -> tuple[zipfile.ZipInfo, int]:
    if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP entry count is invalid"
        )
    names: set[str] = set()
    folded_names: set[str] = set()
    package_members: list[zipfile.ZipInfo] = []
    total_uncompressed = 0
    for entry in entries:
        name = entry.filename
        pure = PurePosixPath(name)
        encoded_name = name.encode("utf-8", errors="strict")
        unix_mode = (entry.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if (
            not name
            or len(encoded_name) > MAX_ARCHIVE_MEMBER_NAME_BYTES
            or "\\" in name
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or name in names
            or name.casefold() in folded_names
            or entry.flag_bits & 0x1
            or entry.compress_type not in _ALLOWED_COMPRESSION
            or file_type == stat.S_IFLNK
            or (
                file_type
                and file_type
                not in {
                    stat.S_IFREG,
                    stat.S_IFDIR,
                }
            )
            or entry.file_size < 0
            or entry.compress_size < 0
        ):
            raise GeoPackageArchiveError(
                "GeoPackage ZIP contains an unsafe entry"
            )
        names.add(name)
        folded_names.add(name.casefold())
        if entry.is_dir():
            continue
        total_uncompressed += entry.file_size
        if total_uncompressed > maximum_uncompressed_bytes:
            raise GeoPackageArchiveError(
                "GeoPackage ZIP exceeds its expansion limit"
            )
        if (
            entry.file_size > 0
            and (
                entry.compress_size == 0
                or entry.file_size
                > max(entry.compress_size, 1) * MAX_COMPRESSION_RATIO
            )
        ):
            raise GeoPackageArchiveError(
                "GeoPackage ZIP has an unsafe compression ratio"
            )
        if pure.suffix.casefold() == ".gpkg":
            package_members.append(entry)
    matches = [
        entry
        for entry in package_members
        if entry.filename == expected_member
    ]
    if len(package_members) != 1 or len(matches) != 1:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP does not contain its unique reviewed member"
        )
    selected = matches[0]
    if not 100 <= selected.file_size <= maximum_uncompressed_bytes:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP member size is invalid"
        )
    return selected, total_uncompressed


def _extract_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: Path,
    *,
    maximum_bytes: int,
) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with (
            archive.open(member, "r") as source,
            os.fdopen(descriptor, "wb") as target,
        ):
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes or total > member.file_size:
                    raise GeoPackageArchiveError(
                        "GeoPackage ZIP member exceeds its declared bounds"
                    )
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
    except GeoPackageArchiveError:
        raise
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP member could not be extracted safely"
        ) from error
    if total != member.file_size:
        raise GeoPackageArchiveError(
            "GeoPackage ZIP member size changed during extraction"
        )
    return digest.hexdigest()


def _inspect_geopackage(
    path: Path,
    *,
    expected_layer: str,
) -> dict[str, Any]:
    if path.stat().st_size < 100:
        raise GeoPackageArchiveError("GeoPackage member is too small")
    with path.open("rb") as source:
        if source.read(16) != _SQLITE_HEADER:
            raise GeoPackageArchiveError(
                "GeoPackage member has no SQLite header"
            )
    uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        if connection.execute("PRAGMA application_id").fetchone() != (
            _GPKG_APPLICATION_ID,
        ):
            raise GeoPackageArchiveError(
                "GeoPackage member has no GeoPackage application id"
            )
        if connection.execute("PRAGMA quick_check(1)").fetchone() != (
            "ok",
        ):
            raise GeoPackageArchiveError(
                "GeoPackage member failed its integrity check"
            )
        required_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "name IN ('gpkg_spatial_ref_sys', 'gpkg_contents', "
                "'gpkg_geometry_columns')"
            )
        }
        if required_tables != {
            "gpkg_spatial_ref_sys",
            "gpkg_contents",
            "gpkg_geometry_columns",
        }:
            raise GeoPackageArchiveError(
                "GeoPackage member metadata is incomplete"
            )
        feature_layers = [
            row[0]
            for row in connection.execute(
                "SELECT contents.table_name "
                "FROM gpkg_contents AS contents "
                "JOIN gpkg_geometry_columns AS geometry "
                "ON geometry.table_name = contents.table_name "
                "WHERE contents.data_type = 'features' "
                "ORDER BY contents.table_name"
            )
        ]
        if expected_layer not in feature_layers:
            raise GeoPackageArchiveError(
                "GeoPackage member lacks its reviewed feature layer"
            )
        geometry_rows = connection.execute(
            "SELECT geometry.column_name, geometry.geometry_type_name, "
            "geometry.srs_id, reference.organization, "
            "reference.organization_coordsys_id, "
            "contents.min_x, contents.min_y, contents.max_x, "
            "contents.max_y "
            "FROM gpkg_geometry_columns AS geometry "
            "JOIN gpkg_contents AS contents "
            "ON contents.table_name = geometry.table_name "
            "JOIN gpkg_spatial_ref_sys AS reference "
            "ON reference.srs_id = geometry.srs_id "
            "WHERE geometry.table_name = ?",
            (expected_layer,),
        ).fetchall()
        if len(geometry_rows) != 1:
            raise GeoPackageArchiveError(
                "GeoPackage feature-layer geometry metadata is ambiguous"
            )
        (
            geometry_column,
            geometry_type,
            srs_id,
            organization,
            organization_srs_id,
            min_x,
            min_y,
            max_x,
            max_y,
        ) = geometry_rows[0]
        if (
            not isinstance(geometry_column, str)
            or not geometry_column
            or not isinstance(geometry_type, str)
            or not geometry_type
            or not isinstance(srs_id, int)
            or str(organization).casefold() != "epsg"
            or organization_srs_id != srs_id
            or srs_id < 100
        ):
            raise GeoPackageArchiveError(
                "GeoPackage feature-layer CRS metadata is invalid"
            )
        declared_bounds = _bounds(
            min_x,
            min_y,
            max_x,
            max_y,
            message="GeoPackage declared bounds are invalid",
        )
        quoted_layer = _quote_identifier(expected_layer)
        quoted_geometry = _quote_identifier(geometry_column)
        feature_count = connection.execute(
            f"SELECT count(*) FROM {quoted_layer}"
        ).fetchone()[0]
        if (
            isinstance(feature_count, bool)
            or not isinstance(feature_count, int)
            or feature_count < 1
        ):
            raise GeoPackageArchiveError(
                "GeoPackage feature count is invalid"
            )
        schema = _table_schema(connection, expected_layer)
        schema_sha256 = canonical_json_sha256(schema)
        actual_bounds, empty_geometry_count = _geometry_bounds(
            connection,
            table=quoted_layer,
            geometry=quoted_geometry,
            expected_srs_id=srs_id,
        )
        sample = _content_sample(
            connection,
            table_name=expected_layer,
            schema=schema,
            feature_count=feature_count,
        )
    return {
        "feature_layer": expected_layer,
        "feature_layers": feature_layers,
        "feature_count": feature_count,
        "geometry_column": geometry_column,
        "geometry_type": geometry_type,
        "crs": f"EPSG:{srs_id}",
        "declared_bounds": declared_bounds,
        "geometry_bounds": actual_bounds,
        "empty_geometry_count": empty_geometry_count,
        "data_schema": schema,
        "data_schema_sha256": schema_sha256,
        "sample": sample,
    }


def _table_schema(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"PRAGMA table_xinfo({_quote_identifier(table_name)})"
    ).fetchall()
    if not rows:
        raise GeoPackageArchiveError(
            "GeoPackage feature-layer schema is unavailable"
        )
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for row in rows:
        if len(row) != 7:
            raise GeoPackageArchiveError(
                "GeoPackage feature-layer schema is malformed"
            )
        cid, name, declared_type, not_null, default, primary_key, hidden = row
        if (
            not isinstance(cid, int)
            or not isinstance(name, str)
            or not name
            or name in names
            or not isinstance(declared_type, str)
            or not isinstance(not_null, int)
            or not_null not in {0, 1}
            or not isinstance(primary_key, int)
            or primary_key < 0
            or not isinstance(hidden, int)
            or hidden not in {0, 1, 2, 3}
            or (default is not None and not isinstance(default, str))
        ):
            raise GeoPackageArchiveError(
                "GeoPackage feature-layer schema is malformed"
            )
        names.add(name)
        result.append(
            {
                "ordinal": cid,
                "name": name,
                "declared_type": declared_type,
                "not_null": bool(not_null),
                "default": default,
                "primary_key_ordinal": primary_key,
                "hidden": hidden,
            }
        )
    return result


def _geometry_bounds(
    connection: sqlite3.Connection,
    *,
    table: str,
    geometry: str,
    expected_srs_id: int,
) -> tuple[dict[str, float], int]:
    west = math.inf
    south = math.inf
    east = -math.inf
    north = -math.inf
    empty_count = 0
    for (value,) in connection.execute(f"SELECT {geometry} FROM {table}"):
        if not isinstance(value, bytes) or len(value) < 8:
            raise GeoPackageArchiveError(
                "GeoPackage geometry header is invalid"
            )
        if value[:2] != b"GP" or value[2] != 0:
            raise GeoPackageArchiveError(
                "GeoPackage geometry header is invalid"
            )
        flags = value[3]
        if flags & 0xC0:
            raise GeoPackageArchiveError(
                "GeoPackage geometry flags are invalid"
            )
        byte_order = "<" if flags & 0x01 else ">"
        envelope_kind = (flags >> 1) & 0x07
        empty = bool(flags & 0x10)
        if envelope_kind not in {1, 2, 3, 4}:
            raise GeoPackageArchiveError(
                "GeoPackage geometry lacks a verifiable envelope"
            )
        srs_id = struct.unpack_from(f"{byte_order}i", value, 4)[0]
        if srs_id != expected_srs_id:
            raise GeoPackageArchiveError(
                "GeoPackage geometry CRS differs from layer metadata"
            )
        envelope_size = {1: 4, 2: 6, 3: 6, 4: 8}[envelope_kind]
        if len(value) < 8 + envelope_size * 8:
            raise GeoPackageArchiveError(
                "GeoPackage geometry envelope is truncated"
            )
        envelope = struct.unpack_from(
            f"{byte_order}{envelope_size}d",
            value,
            8,
        )
        min_x, max_x, min_y, max_y = envelope[:4]
        if empty:
            empty_count += 1
            continue
        current = _bounds(
            min_x,
            min_y,
            max_x,
            max_y,
            message="GeoPackage geometry envelope is invalid",
        )
        west = min(west, current["west"])
        south = min(south, current["south"])
        east = max(east, current["east"])
        north = max(north, current["north"])
    if not all(math.isfinite(item) for item in (west, south, east, north)):
        raise GeoPackageArchiveError(
            "GeoPackage has no non-empty geometry bounds"
        )
    return _bounds(
        west,
        south,
        east,
        north,
        message="GeoPackage aggregate geometry bounds are invalid",
    ), empty_count


def _content_sample(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    schema: list[dict[str, Any]],
    feature_count: int,
) -> dict[str, Any]:
    primary = [
        item
        for item in schema
        if item["primary_key_ordinal"] > 0
    ]
    if (
        len(primary) != 1
        or "INT" not in primary[0]["declared_type"].upper()
    ):
        raise GeoPackageArchiveError(
            "GeoPackage feature layer lacks one stable integer key"
        )
    positions = sorted(
        {
            0,
            min(1, feature_count - 1),
            feature_count // 4,
            feature_count // 2,
            (feature_count * 3) // 4,
            max(0, feature_count - 2),
            feature_count - 1,
        }
    )
    table = _quote_identifier(table_name)
    key = _quote_identifier(primary[0]["name"])
    rows: list[dict[str, Any]] = []
    for position in positions:
        row = connection.execute(
            f"SELECT * FROM {table} ORDER BY {key} LIMIT 1 OFFSET ?",
            (position,),
        ).fetchone()
        if row is None or len(row) != len(schema):
            raise GeoPackageArchiveError(
                "GeoPackage deterministic content sample is incomplete"
            )
        rows.append(
            {
                "position": position,
                "cells": [
                    _sample_cell(value)
                    for value in row
                ],
            }
        )
    evidence = {
        "schema_version": SAMPLE_SCHEMA,
        "order_by": primary[0]["name"],
        "positions": positions,
        "rows": rows,
    }
    return {
        "schema_version": SAMPLE_SCHEMA,
        "order_by": primary[0]["name"],
        "positions": positions,
        "sha256": canonical_json_sha256(evidence),
    }


def _sample_cell(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer", "value": value}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GeoPackageArchiveError(
                "GeoPackage sample contains a non-finite number"
            )
        return {"type": "real", "value": value}
    if isinstance(value, str):
        body = value.encode("utf-8")
        kind = "text"
    elif isinstance(value, bytes):
        body = value
        kind = "blob"
    else:
        raise GeoPackageArchiveError(
            "GeoPackage sample contains an unsupported SQLite value"
        )
    return {
        "type": kind,
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _bounds(
    west: Any,
    south: Any,
    east: Any,
    north: Any,
    *,
    message: str,
) -> dict[str, float]:
    values = (west, south, east, north)
    if (
        any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values)
        or not all(math.isfinite(float(item)) for item in values)
        or float(west) >= float(east)
        or float(south) >= float(north)
    ):
        raise GeoPackageArchiveError(message)
    return {
        "west": round(float(west), 9),
        "south": round(float(south), 9),
        "east": round(float(east), 9),
        "north": round(float(north), 9),
    }


def _quote_identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_000
        or "\x00" in value
    ):
        raise GeoPackageArchiveError(
            "GeoPackage SQLite identifier is invalid"
        )
    return '"' + value.replace('"', '""') + '"'


def _expected_name(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_ARCHIVE_MEMBER_NAME_BYTES
        or any(ord(character) < 32 for character in value)
    ):
        raise GeoPackageArchiveError(f"{label} is invalid")
