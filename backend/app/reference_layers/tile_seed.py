"""Build complete, immutable MBTiles snapshots from reviewed tile sources."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from io import BytesIO
from itertools import islice
import math
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    StoredReferenceBlob,
)
from app.reference_layers.local_tile_archive import validate_tile_image
from app.reference_layers.safe_download import (
    HTTPSDownloadPolicy,
    SafeHTTPSDownloader,
)
from app.reference_layers.wms_proxy import TILE_SIZE, tile_bbox


TILE_SOURCE_SCHEMA = "reference-tile-source/v1"
COORDINATE_HASH_SCHEMA = "xyz-z-x-y-newline-v1"
MAX_SEED_TILES = 25_000_000
MAX_TILE_BYTES = 1024 * 1024
MAX_ZOOM = 22
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,500}$", re.ASCII)
_MATRIX_TOKEN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")


class TileSeedError(RuntimeError):
    """A tile source cannot produce a complete bounded local archive."""


@dataclass(frozen=True, order=True)
class TileCoordinate:
    z: int
    x: int
    y: int
    matrix_identifier: str | None = None


@dataclass(frozen=True)
class TileMatrix:
    identifier: str
    zoom: int
    matrix_width: int
    matrix_height: int


@dataclass(frozen=True)
class TileSourceDescriptor:
    protocol: Literal["xyz", "wms_tiles", "wmts"]
    definition_sha256: str
    layer: str
    bounds: dict[str, float]
    min_zoom: int
    max_zoom: int
    image_format: Literal["png", "jpg"]
    media_type: str
    estimated_tile_count: int
    max_tile_count: int
    payload: Mapping[str, Any]
    matrices: Mapping[int, TileMatrix]
    matrix_limits: Mapping[str, tuple[int, int, int, int]]


@dataclass(frozen=True)
class TileSeedResult:
    blob: StoredReferenceBlob
    tile_count: int
    coordinate_sha256: str
    min_zoom: int
    max_zoom: int
    image_format: str
    bounds_json: dict[str, float]
    validation_json: dict[str, Any]


TileFetcher = Callable[[str, str], bytes]
Heartbeat = Callable[[int, int], None]


def parse_tile_source_document(value: object) -> TileSourceDescriptor:
    """Validate the acquisition adapter's versioned tile-source document."""

    document = _mapping(value, "tile source document")
    _exact_keys(
        document,
        {"schema", "protocol", "definition_sha256", "descriptor"},
        "tile source document",
    )
    if document["schema"] != TILE_SOURCE_SCHEMA:
        raise TileSeedError("tile source schema is unsupported")
    protocol = document["protocol"]
    if protocol not in {"xyz", "wms_tiles", "wmts"}:
        raise TileSeedError("tile source protocol is unsupported")
    definition_sha256 = document["definition_sha256"]
    if not isinstance(definition_sha256, str) or _SHA256_RE.fullmatch(
        definition_sha256
    ) is None:
        raise TileSeedError("tile source definition hash is invalid")
    descriptor = _mapping(document["descriptor"], "tile source descriptor")
    common = {
        "bounds",
        "min_zoom",
        "max_zoom",
        "format",
        "coverage_required",
        "estimated_tile_count",
        "max_tile_count",
        "layer",
    }
    protocol_keys = {
        "xyz": {"url_template", "scheme"},
        "wms_tiles": {"style", "crs", "kvp"},
        "wmts": {
            "style",
            "tile_matrix_sets",
            "selected_tile_matrix_set",
            "tile_matrix_set",
            "tile_matrix_limits",
            "resource_urls",
            "kvp",
        },
    }[protocol]
    _exact_keys(descriptor, common | protocol_keys, "tile source descriptor")
    if descriptor["coverage_required"] is not True:
        raise TileSeedError("a local tile snapshot requires complete coverage")
    bounds = _bounds(descriptor["bounds"])
    min_zoom = _integer(descriptor["min_zoom"], "minimum zoom", 0, MAX_ZOOM)
    max_zoom = _integer(descriptor["max_zoom"], "maximum zoom", min_zoom, MAX_ZOOM)
    estimated = _integer(
        descriptor["estimated_tile_count"],
        "estimated tile count",
        1,
        MAX_SEED_TILES,
    )
    maximum = _integer(
        descriptor["max_tile_count"],
        "maximum tile count",
        1,
        MAX_SEED_TILES,
    )
    if estimated > maximum:
        raise TileSeedError("estimated tile count exceeds the reviewed limit")
    layer = _token(descriptor["layer"], "tile layer")
    image_format, media_type = _image_format(descriptor["format"])
    matrices: dict[int, TileMatrix] = {}
    limits: dict[str, tuple[int, int, int, int]] = {}
    if protocol == "xyz":
        _validate_xyz_descriptor(descriptor)
    elif protocol == "wms_tiles":
        _validate_wms_descriptor(descriptor, media_type)
    else:
        matrices, limits = _validate_wmts_descriptor(
            descriptor,
            media_type,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
        )
    parsed = TileSourceDescriptor(
        protocol=protocol,
        definition_sha256=definition_sha256,
        layer=layer,
        bounds=bounds,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        image_format=image_format,
        media_type=media_type,
        estimated_tile_count=estimated,
        max_tile_count=maximum,
        payload=descriptor,
        matrices=matrices,
        matrix_limits=limits,
    )
    actual_count = sum(1 for _ in iter_tile_coordinates(parsed))
    if actual_count != estimated:
        raise TileSeedError(
            "reviewed tile estimate does not match the exact coverage"
        )
    return parsed


def iter_tile_coordinates(
    descriptor: TileSourceDescriptor,
) -> Iterator[TileCoordinate]:
    """Yield the exact XYZ coverage in stable zoom/column/row order."""

    west = descriptor.bounds["west"]
    south = max(
        -WEB_MERCATOR_MAX_LATITUDE,
        descriptor.bounds["south"],
    )
    east = descriptor.bounds["east"]
    north = min(
        WEB_MERCATOR_MAX_LATITUDE,
        descriptor.bounds["north"],
    )
    if south >= north:
        raise TileSeedError("tile bounds do not intersect Web Mercator")
    for zoom in range(descriptor.min_zoom, descriptor.max_zoom + 1):
        count = 2**zoom
        min_x = _clamp_tile(math.floor(_longitude_position(west, count)), count)
        max_x = _clamp_tile(
            math.ceil(_longitude_position(east, count)) - 1,
            count,
        )
        min_y = _clamp_tile(math.floor(_latitude_position(north, count)), count)
        max_y = _clamp_tile(
            math.ceil(_latitude_position(south, count)) - 1,
            count,
        )
        matrix_identifier = None
        limit = None
        if descriptor.protocol == "wmts":
            matrix = descriptor.matrices.get(zoom)
            if matrix is None:
                raise TileSeedError("WMTS matrix coverage has a zoom gap")
            matrix_identifier = matrix.identifier
            limit = descriptor.matrix_limits.get(matrix_identifier)
        if limit is not None:
            limit_min_row, limit_max_row, limit_min_col, limit_max_col = limit
            min_x = max(min_x, limit_min_col)
            max_x = min(max_x, limit_max_col)
            min_y = max(min_y, limit_min_row)
            max_y = min(max_y, limit_max_row)
        if min_x > max_x or min_y > max_y:
            continue
        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                yield TileCoordinate(zoom, x, y, matrix_identifier)


def coordinate_sha256(coordinates: Iterator[TileCoordinate]) -> str:
    digest = hashlib.sha256()
    for coordinate in coordinates:
        digest.update(
            f"{coordinate.z}/{coordinate.x}/{coordinate.y}\n".encode("ascii")
        )
    return digest.hexdigest()


def seed_tile_archive(
    store: ReferenceBlobStore,
    *,
    source_document: object,
    max_archive_bytes: int,
    fetcher: TileFetcher | None = None,
    heartbeat: Heartbeat | None = None,
    concurrency: int = 4,
    batch_size: int = 64,
) -> TileSeedResult:
    """Download and atomically store a complete reviewed tile pyramid."""

    descriptor = parse_tile_source_document(source_document)
    if (
        isinstance(max_archive_bytes, bool)
        or not isinstance(max_archive_bytes, int)
        or not 1 <= max_archive_bytes <= store.max_blob_bytes
    ):
        raise TileSeedError("tile archive byte limit is invalid")
    concurrency = _integer(concurrency, "tile concurrency", 1, 16)
    batch_size = _integer(batch_size, "tile batch size", concurrency, 64)
    expected_coordinate_sha256 = coordinate_sha256(
        iter_tile_coordinates(descriptor)
    )
    tile_fetcher = fetcher or _https_fetcher(descriptor)
    staging_root = store.root / "staging"
    try:
        with tempfile.TemporaryDirectory(
            prefix="tile-seed-",
            dir=staging_root,
        ) as directory:
            archive = Path(directory, "snapshot.mbtiles")
            _write_archive(
                archive,
                descriptor,
                iter_tile_coordinates(descriptor),
                tile_fetcher,
                total_tiles=descriptor.estimated_tile_count,
                expected_coordinate_sha256=expected_coordinate_sha256,
                max_archive_bytes=max_archive_bytes,
                heartbeat=heartbeat,
                concurrency=concurrency,
                batch_size=batch_size,
            )
            with archive.open("rb") as stream:
                blob = store.put_stream(stream, max_bytes=max_archive_bytes)
    except TileSeedError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise TileSeedError("tile archive could not be built locally") from error
    validation = {
        "passed": True,
        "kind": "tiles",
        "checks": {
            "complete_coverage": True,
            "coordinate_hash_schema": COORDINATE_HASH_SCHEMA,
            "coordinate_sha256": expected_coordinate_sha256,
            "tile_count": descriptor.estimated_tile_count,
            "all_images_valid": True,
            "unique_coordinate_index": True,
            "source_definition_sha256": descriptor.definition_sha256,
        },
    }
    return TileSeedResult(
        blob=blob,
        tile_count=descriptor.estimated_tile_count,
        coordinate_sha256=expected_coordinate_sha256,
        min_zoom=descriptor.min_zoom,
        max_zoom=descriptor.max_zoom,
        image_format=descriptor.image_format,
        bounds_json=descriptor.bounds,
        validation_json=validation,
    )


def tile_url(
    descriptor: TileSourceDescriptor,
    coordinate: TileCoordinate,
) -> str:
    """Build one exact upstream request without accepting caller parameters."""

    payload = descriptor.payload
    if descriptor.protocol == "xyz":
        template = str(payload["url_template"])
        remote_y = (
            (2**coordinate.z - 1) - coordinate.y
            if payload["scheme"] == "tms"
            else coordinate.y
        )
        result = (
            template.replace("{z}", str(coordinate.z))
            .replace("{x}", str(coordinate.x))
            .replace("{y}", str(remote_y))
            .replace("{-y}", str(remote_y))
        )
        if "{" in result or "}" in result:
            raise TileSeedError("XYZ template contains an unsupported placeholder")
        _https_url(result, "XYZ tile URL")
        return result
    if descriptor.protocol == "wms_tiles":
        kvp = _mapping(payload["kvp"], "WMS KVP descriptor")
        version = str(kvp["version"])
        bbox = tile_bbox(coordinate.z, coordinate.x, coordinate.y)
        parameters = {
            "SERVICE": "WMS",
            "REQUEST": "GetMap",
            "VERSION": version,
            "LAYERS": descriptor.layer,
            "STYLES": str(payload["style"]),
            "FORMAT": descriptor.media_type,
            "TRANSPARENT": str(kvp["transparent"]),
            "BBOX": ",".join(format(item, ".12f") for item in bbox),
            "WIDTH": str(TILE_SIZE),
            "HEIGHT": str(TILE_SIZE),
            "CRS" if version == "1.3.0" else "SRS": "EPSG:3857",
        }
        return _merge_query(str(kvp["endpoint_url"]), parameters)
    matrix_identifier = coordinate.matrix_identifier
    if matrix_identifier is None:
        raise TileSeedError("WMTS coordinate has no matrix identifier")
    resource = _selected_resource_template(payload, descriptor.media_type)
    if resource is not None:
        replacements = {
            "{TileMatrixSet}": str(payload["selected_tile_matrix_set"]),
            "{TileMatrix}": matrix_identifier,
            "{TileRow}": str(coordinate.y),
            "{TileCol}": str(coordinate.x),
            "{Style}": str(payload["style"]),
        }
        result = resource
        for token, replacement in replacements.items():
            result = result.replace(token, replacement)
        if "{" in result or "}" in result:
            raise TileSeedError("WMTS template contains an unsupported placeholder")
        _https_url(result, "WMTS tile URL")
        return result
    kvp = _mapping(payload["kvp"], "WMTS KVP descriptor")
    parameters = {
        "SERVICE": "WMTS",
        "REQUEST": "GetTile",
        "VERSION": str(kvp["version"]),
        "LAYER": descriptor.layer,
        "STYLE": str(payload["style"]),
        "FORMAT": descriptor.media_type,
        "TILEMATRIXSET": str(payload["selected_tile_matrix_set"]),
        "TILEMATRIX": matrix_identifier,
        "TILEROW": str(coordinate.y),
        "TILECOL": str(coordinate.x),
    }
    return _merge_query(str(kvp["endpoint_url"]), parameters)


def _write_archive(
    path: Path,
    descriptor: TileSourceDescriptor,
    coordinates: Iterator[TileCoordinate],
    fetcher: TileFetcher,
    *,
    total_tiles: int,
    expected_coordinate_sha256: str,
    max_archive_bytes: int,
    heartbeat: Heartbeat | None,
    concurrency: int,
    batch_size: int,
) -> None:
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute(
            "CREATE TABLE metadata ("
            "name TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE tiles ("
            "zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL, "
            "tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX tile_index ON tiles "
            "(zoom_level, tile_column, tile_row)"
        )
        connection.executemany(
            "INSERT INTO metadata (name, value) VALUES (?, ?)",
            _archive_metadata(descriptor, expected_coordinate_sha256),
        )
        completed = 0
        if heartbeat is not None:
            heartbeat(completed, total_tiles)
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="reference-tile",
        ) as executor:
            while True:
                batch = list(islice(coordinates, batch_size))
                if not batch:
                    break
                bodies = list(
                    executor.map(
                        lambda item: _fetch_validated_tile(
                            descriptor,
                            item,
                            fetcher,
                        ),
                        batch,
                    )
                )
                if (
                    _sqlite_size(connection) + sum(map(len, bodies))
                    > max_archive_bytes
                ):
                    raise TileSeedError("tile archive exceeds its byte limit")
                connection.executemany(
                    "INSERT INTO tiles "
                    "(zoom_level, tile_column, tile_row, tile_data) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (
                            coordinate.z,
                            coordinate.x,
                            (2**coordinate.z - 1) - coordinate.y,
                            body,
                        )
                        for coordinate, body in zip(batch, bodies, strict=True)
                    ],
                )
                connection.commit()
                if _sqlite_size(connection) > max_archive_bytes:
                    raise TileSeedError("tile archive exceeds its byte limit")
                completed += len(batch)
                if heartbeat is not None:
                    heartbeat(completed, total_tiles)
        stored_count = int(
            connection.execute("SELECT count(*) FROM tiles").fetchone()[0]
        )
        if stored_count != total_tiles or completed != total_tiles:
            raise TileSeedError("tile archive coverage is incomplete")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise TileSeedError("tile archive integrity check failed")
        connection.commit()
    finally:
        connection.close()
    try:
        if path.stat().st_size > max_archive_bytes:
            raise TileSeedError("tile archive exceeds its byte limit")
    except OSError as error:
        raise TileSeedError("tile archive size is unavailable") from error


def _fetch_validated_tile(
    descriptor: TileSourceDescriptor,
    coordinate: TileCoordinate,
    fetcher: TileFetcher,
) -> bytes:
    body = fetcher(tile_url(descriptor, coordinate), descriptor.media_type)
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_TILE_BYTES:
        raise TileSeedError("tile source returned invalid image bytes")
    try:
        validate_tile_image(body, descriptor.image_format)
    except Exception as error:
        raise TileSeedError("tile source returned an invalid image") from error
    return body


def _https_fetcher(descriptor: TileSourceDescriptor) -> TileFetcher:
    sample = tile_url(descriptor, next(iter_tile_coordinates(descriptor)))
    origin = _origin(sample)
    downloader = SafeHTTPSDownloader(
        HTTPSDownloadPolicy(
            allowed_origins=(origin,),
            max_response_bytes=MAX_TILE_BYTES,
            timeout_seconds=90,
            connect_timeout_seconds=10,
            idle_timeout_seconds=30,
            allowed_content_types=frozenset(
                {"image/png", "image/jpeg", "image/jpg"}
            ),
        )
    )

    def fetch(url: str, media_type: str) -> bytes:
        if _origin(url) != origin:
            raise TileSeedError("tile URL changed its reviewed origin")
        sink = BytesIO()
        result = downloader.download(url, sink, accept=media_type)
        if result.content_type not in {
            media_type,
            "image/jpeg" if media_type == "image/jpg" else media_type,
        }:
            raise TileSeedError("tile response media type is inconsistent")
        return sink.getvalue()

    return fetch


def _archive_metadata(
    descriptor: TileSourceDescriptor,
    coordinate_digest: str,
) -> list[tuple[str, str]]:
    bounds = ",".join(
        format(descriptor.bounds[key], ".12g")
        for key in ("west", "south", "east", "north")
    )
    return [
        ("name", descriptor.layer),
        ("type", "baselayer"),
        ("version", "1.3"),
        ("description", "Immutable complete local reference snapshot"),
        ("format", descriptor.image_format),
        ("bounds", bounds),
        ("minzoom", str(descriptor.min_zoom)),
        ("maxzoom", str(descriptor.max_zoom)),
        ("scheme", "tms"),
        ("coordinate_hash_schema", COORDINATE_HASH_SCHEMA),
        ("coordinate_sha256", coordinate_digest),
        ("source_definition_sha256", descriptor.definition_sha256),
    ]


def _validate_xyz_descriptor(descriptor: Mapping[str, Any]) -> None:
    template = descriptor["url_template"]
    if not isinstance(template, str) or len(template) > 8192:
        raise TileSeedError("XYZ template is invalid")
    scheme = descriptor["scheme"]
    expected_y = "{-y}" if scheme == "tms" else "{y}"
    other_y = "{y}" if scheme == "tms" else "{-y}"
    if (
        template.count("{z}") != 1
        or template.count("{x}") != 1
        or template.count(expected_y) != 1
        or other_y in template
    ):
        raise TileSeedError("XYZ template must contain z, x and y once")
    sample = (
        template.replace("{z}", "0")
        .replace("{x}", "0")
        .replace(expected_y, "0")
    )
    _https_url(sample, "XYZ template")
    if scheme not in {"xyz", "tms"}:
        raise TileSeedError("XYZ tile scheme is invalid")


def _validate_wms_descriptor(
    descriptor: Mapping[str, Any],
    media_type: str,
) -> None:
    if descriptor["crs"] != "EPSG:3857":
        raise TileSeedError("WMS tile source must use EPSG:3857")
    _token(descriptor["style"], "WMS style", allow_empty=True)
    kvp = _mapping(descriptor["kvp"], "WMS KVP descriptor")
    required = {
        "endpoint_url",
        "service",
        "request",
        "version",
        "layers",
        "styles",
        "format",
        "transparent",
        "bbox_placeholder",
        "width_placeholder",
        "height_placeholder",
    }
    coordinate_keys = {"CRS", "SRS"} & set(kvp)
    if set(kvp) != required | coordinate_keys or len(coordinate_keys) != 1:
        raise TileSeedError("WMS KVP descriptor keys are invalid")
    _https_url(kvp["endpoint_url"], "WMS endpoint")
    if (
        kvp["service"] != "WMS"
        or kvp["request"] != "GetMap"
        or kvp["version"] not in {"1.1.1", "1.3.0"}
        or kvp["layers"] != descriptor["layer"]
        or kvp["styles"] != descriptor["style"]
        or kvp["format"] != media_type
        or kvp["transparent"] not in {"TRUE", "FALSE"}
        or kvp[next(iter(coordinate_keys))] != "EPSG:3857"
    ):
        raise TileSeedError("WMS KVP descriptor is inconsistent")


def _validate_wmts_descriptor(
    descriptor: Mapping[str, Any],
    media_type: str,
    *,
    min_zoom: int,
    max_zoom: int,
) -> tuple[dict[int, TileMatrix], dict[str, tuple[int, int, int, int]]]:
    _token(descriptor["style"], "WMTS style", allow_empty=True)
    matrix_set_ids = descriptor["tile_matrix_sets"]
    if (
        not isinstance(matrix_set_ids, list)
        or not matrix_set_ids
        or len(matrix_set_ids) > 100
        or any(
            _MATRIX_TOKEN_RE.fullmatch(item) is None
            for item in matrix_set_ids
            if isinstance(item, str)
        )
        or any(not isinstance(item, str) for item in matrix_set_ids)
        or len(set(matrix_set_ids)) != len(matrix_set_ids)
    ):
        raise TileSeedError("WMTS matrix-set list is invalid")
    selected = descriptor["selected_tile_matrix_set"]
    if selected not in matrix_set_ids:
        raise TileSeedError("selected WMTS matrix set is not advertised")
    matrix_set = _mapping(descriptor["tile_matrix_set"], "WMTS matrix set")
    allowed_matrix_set_keys = {
        "identifier",
        "supported_crs",
        "tile_matrices",
        "well_known_scale_set",
    }
    if not set(matrix_set) <= allowed_matrix_set_keys or not {
        "identifier",
        "supported_crs",
        "tile_matrices",
    } <= set(matrix_set):
        raise TileSeedError("WMTS matrix-set keys are invalid")
    if matrix_set["identifier"] != selected or "3857" not in str(
        matrix_set["supported_crs"]
    ):
        raise TileSeedError("WMTS matrix set is not Web Mercator")
    raw_matrices = matrix_set["tile_matrices"]
    if not isinstance(raw_matrices, list) or not raw_matrices:
        raise TileSeedError("WMTS matrix list is invalid")
    matrices: dict[int, TileMatrix] = {}
    identifiers: set[str] = set()
    for raw in raw_matrices:
        item = _mapping(raw, "WMTS tile matrix")
        _exact_keys(
            item,
            {
                "identifier",
                "zoom",
                "scale_denominator",
                "top_left_corner",
                "tile_width",
                "tile_height",
                "matrix_width",
                "matrix_height",
            },
            "WMTS tile matrix",
        )
        identifier = item["identifier"]
        if (
            not isinstance(identifier, str)
            or _MATRIX_TOKEN_RE.fullmatch(identifier) is None
        ):
            raise TileSeedError("WMTS matrix identifier is invalid")
        zoom = _integer(item["zoom"], "WMTS matrix zoom", 0, MAX_ZOOM)
        width = _integer(item["matrix_width"], "WMTS matrix width", 1, 2**MAX_ZOOM)
        height = _integer(item["matrix_height"], "WMTS matrix height", 1, 2**MAX_ZOOM)
        scale = item["scale_denominator"]
        corner = item["top_left_corner"]
        if (
            identifier in identifiers
            or zoom in matrices
            or item["tile_width"] != TILE_SIZE
            or item["tile_height"] != TILE_SIZE
            or width != 2**zoom
            or height != 2**zoom
            or isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or scale <= 0
            or not isinstance(corner, list)
            or len(corner) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in corner
            )
        ):
            raise TileSeedError("WMTS tile matrix is not a Google-compatible grid")
        identifiers.add(identifier)
        matrices[zoom] = TileMatrix(identifier, zoom, width, height)
    if any(zoom not in matrices for zoom in range(min_zoom, max_zoom + 1)):
        raise TileSeedError("WMTS matrix list does not cover the zoom range")
    limits: dict[str, tuple[int, int, int, int]] = {}
    raw_limits = descriptor["tile_matrix_limits"]
    if not isinstance(raw_limits, list) or len(raw_limits) > len(matrices):
        raise TileSeedError("WMTS matrix limits are invalid")
    for raw in raw_limits:
        item = _mapping(raw, "WMTS matrix limit")
        _exact_keys(
            item,
            {
                "tile_matrix",
                "min_tile_row",
                "max_tile_row",
                "min_tile_col",
                "max_tile_col",
            },
            "WMTS matrix limit",
        )
        identifier = item["tile_matrix"]
        if identifier not in identifiers or identifier in limits:
            raise TileSeedError("WMTS matrix limit refers to an unknown matrix")
        matrix = next(
            value
            for value in matrices.values()
            if value.identifier == identifier
        )
        min_row = _integer(
            item["min_tile_row"],
            "minimum WMTS tile row",
            0,
            matrix.matrix_height - 1,
        )
        max_row = _integer(
            item["max_tile_row"],
            "maximum WMTS tile row",
            min_row,
            matrix.matrix_height - 1,
        )
        min_col = _integer(
            item["min_tile_col"],
            "minimum WMTS tile column",
            0,
            matrix.matrix_width - 1,
        )
        max_col = _integer(
            item["max_tile_col"],
            "maximum WMTS tile column",
            min_col,
            matrix.matrix_width - 1,
        )
        limits[identifier] = (min_row, max_row, min_col, max_col)
    resources = descriptor["resource_urls"]
    if not isinstance(resources, list) or len(resources) > 100:
        raise TileSeedError("WMTS resource URL list is invalid")
    for raw in resources:
        item = _mapping(raw, "WMTS resource URL")
        if not set(item) <= {"template", "resource_type", "format"} or not {
            "template",
            "resource_type",
        } <= set(item):
            raise TileSeedError("WMTS resource URL keys are invalid")
        template = item["template"]
        if not isinstance(template, str) or len(template) > 8192:
            raise TileSeedError("WMTS resource template is invalid")
        sample = template
        for token in (
            "{TileMatrixSet}",
            "{TileMatrix}",
            "{TileRow}",
            "{TileCol}",
            "{Style}",
        ):
            sample = sample.replace(token, "0")
        if "{" not in sample and "}" not in sample:
            _https_url(sample, "WMTS resource template")
    kvp = _mapping(descriptor["kvp"], "WMTS KVP descriptor")
    _exact_keys(
        kvp,
        {
            "endpoint_url",
            "service",
            "request",
            "version",
            "layer",
            "style",
            "format",
            "tile_matrix_set",
            "tile_matrix_placeholder",
            "tile_row_placeholder",
            "tile_col_placeholder",
        },
        "WMTS KVP descriptor",
    )
    _https_url(kvp["endpoint_url"], "WMTS endpoint")
    if (
        kvp["service"] != "WMTS"
        or kvp["request"] != "GetTile"
        or kvp["version"] != "1.0.0"
        or kvp["layer"] != descriptor["layer"]
        or kvp["style"] != descriptor["style"]
        or kvp["format"] != media_type
        or kvp["tile_matrix_set"] != selected
    ):
        raise TileSeedError("WMTS KVP descriptor is inconsistent")
    return matrices, limits


def _selected_resource_template(
    descriptor: Mapping[str, Any],
    media_type: str,
) -> str | None:
    for raw in descriptor["resource_urls"]:
        item = _mapping(raw, "WMTS resource URL")
        if item["resource_type"].casefold() != "tile":
            continue
        if item.get("format") not in {None, media_type}:
            continue
        return str(item["template"])
    return None


def _merge_query(endpoint: str, parameters: Mapping[str, str]) -> str:
    parts = _https_url(endpoint, "tile endpoint")
    try:
        existing_pairs = parse_qsl(
            parts.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=50,
        )
    except ValueError as error:
        raise TileSeedError("tile endpoint query is invalid") from error
    reserved = {key.casefold() for key in parameters}
    if any(key.casefold() in reserved for key, _value in existing_pairs):
        raise TileSeedError("tile endpoint duplicates a controlled parameter")
    query = urlencode([*existing_pairs, *parameters.items()])
    result = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    if len(result) > 8192:
        raise TileSeedError("tile request URL is too long")
    return result


def _https_url(value: object, label: str):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise TileSeedError(f"{label} is invalid")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise TileSeedError(f"{label} is invalid") from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
        or parts.fragment
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise TileSeedError(f"{label} must be credential-free HTTPS")
    return parts


def _origin(value: str) -> str:
    parts = _https_url(value, "tile URL")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    return f"https://{host}"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TileSeedError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise TileSeedError(f"{label} keys are invalid")


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise TileSeedError(f"{label} is invalid")
    return value


def _token(value: object, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise TileSeedError(f"{label} is invalid")
    return value


def _image_format(value: object) -> tuple[Literal["png", "jpg"], str]:
    if not isinstance(value, str):
        raise TileSeedError("tile image format is invalid")
    normalized = value.casefold()
    if normalized == "image/png":
        return "png", "image/png"
    if normalized in {"image/jpeg", "image/jpg"}:
        return "jpg", "image/jpeg"
    raise TileSeedError("tile image format is unsupported")


def _bounds(value: object) -> dict[str, float]:
    item = _mapping(value, "tile bounds")
    _exact_keys(item, {"west", "south", "east", "north"}, "tile bounds")
    numbers: dict[str, float] = {}
    for key in ("west", "south", "east", "north"):
        raw = item[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TileSeedError("tile bounds are invalid")
        number = float(raw)
        if not math.isfinite(number):
            raise TileSeedError("tile bounds are invalid")
        numbers[key] = number
    if not (
        -180 <= numbers["west"] < numbers["east"] <= 180
        and -90 <= numbers["south"] < numbers["north"] <= 90
    ):
        raise TileSeedError("tile bounds are outside WGS84")
    return numbers


def _longitude_position(longitude: float, count: int) -> float:
    return (longitude + 180.0) / 360.0 * count


def _latitude_position(latitude: float, count: int) -> float:
    radians = math.radians(latitude)
    return (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * count


def _clamp_tile(value: int, count: int) -> int:
    return min(count - 1, max(0, value))


def _sqlite_size(connection: sqlite3.Connection) -> int:
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    return page_count * page_size
