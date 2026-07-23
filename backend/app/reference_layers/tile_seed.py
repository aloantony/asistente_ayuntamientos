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
import warnings

from PIL import Image, UnidentifiedImageError

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    StoredReferenceBlob,
)
from app.reference_layers.local_tile_archive import validate_tile_image
from app.reference_layers.mirror_coverage import (
    SIUR_ORTHO_TILE_PROFILE,
    SIUR_TILE_PROFILE,
)
from app.reference_layers.safe_download import (
    DownloadHTTPError,
    DownloadIntegrityError,
    DownloadLimitError,
    HTTPSDownloadPolicy,
    SafeHTTPSDownloader,
)
from app.reference_layers.wms_proxy import TILE_SIZE, tile_bbox


TILE_SOURCE_SCHEMA = "reference-tile-source/v1"
COORDINATE_HASH_SCHEMA = "xyz-z-x-y-newline-v1"
MAX_SEED_TILES = 25_000_000
MAX_TILE_BYTES = 1024 * 1024
MAX_WMS_SUPERTILE_SIZE = 8
MAX_WMS_SUPERTILE_BYTES = (
    MAX_TILE_BYTES * MAX_WMS_SUPERTILE_SIZE * MAX_WMS_SUPERTILE_SIZE
)
MAX_INFLIGHT_WMS_BYTES = 128 * 1024 * 1024
MAX_ZOOM = 22
DEFAULT_PREFLIGHT_SAMPLES = 64
PREFLIGHT_FIXED_BYTES = 64 * 1024
PREFLIGHT_TILE_OVERHEAD_BYTES = 128
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
    wms_supertile_size: int


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


@dataclass(frozen=True)
class TileSeedPreflight:
    tile_count: int
    sample_count: int
    sample_bytes: int
    largest_tile_bytes: int
    projected_archive_bytes: int


@dataclass(frozen=True)
class _TileWindow:
    zoom: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    matrix_identifier: str | None

    @property
    def tile_count(self) -> int:
        return (self.max_x - self.min_x + 1) * (self.max_y - self.min_y + 1)


@dataclass(frozen=True, order=True)
class _WMSSupertile:
    zoom: int
    min_x: int
    max_x: int
    min_y: int
    max_y: int

    @property
    def width(self) -> int:
        return self.max_x - self.min_x + 1

    @property
    def height(self) -> int:
        return self.max_y - self.min_y + 1

    @property
    def tile_count(self) -> int:
        return self.width * self.height


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
    expected_keys = common | protocol_keys
    if protocol == "wms_tiles":
        actual_keys = frozenset(descriptor)
        if actual_keys not in {
            frozenset(expected_keys),
            frozenset(
                expected_keys | {"coverage_profile", "wms_supertile_size"}
            ),
        }:
            raise TileSeedError("tile source descriptor keys are invalid")
    else:
        _exact_keys(descriptor, expected_keys, "tile source descriptor")
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
    wms_supertile_size = 1
    if protocol == "xyz":
        _validate_xyz_descriptor(descriptor)
    elif protocol == "wms_tiles":
        wms_supertile_size = _validate_wms_descriptor(descriptor, media_type)
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
        wms_supertile_size=wms_supertile_size,
    )
    actual_count = sum(window.tile_count for window in _tile_windows(parsed))
    if actual_count != estimated:
        raise TileSeedError(
            "reviewed tile estimate does not match the exact coverage"
        )
    return parsed


def iter_tile_coordinates(
    descriptor: TileSourceDescriptor,
) -> Iterator[TileCoordinate]:
    """Yield the exact XYZ coverage in stable zoom/column/row order."""

    for window in _tile_windows(descriptor):
        for x in range(window.min_x, window.max_x + 1):
            for y in range(window.min_y, window.max_y + 1):
                yield TileCoordinate(
                    window.zoom,
                    x,
                    y,
                    window.matrix_identifier,
                )


def _tile_windows(descriptor: TileSourceDescriptor) -> tuple[_TileWindow, ...]:
    """Return exact non-empty coverage windows without enumerating every tile."""

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
    windows: list[_TileWindow] = []
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
        windows.append(
            _TileWindow(
                zoom=zoom,
                min_x=min_x,
                max_x=max_x,
                min_y=min_y,
                max_y=max_y,
                matrix_identifier=matrix_identifier,
            )
        )
    return tuple(windows)


def coordinate_sha256(coordinates: Iterator[TileCoordinate]) -> str:
    digest = hashlib.sha256()
    for coordinate in coordinates:
        digest.update(
            f"{coordinate.z}/{coordinate.x}/{coordinate.y}\n".encode("ascii")
        )
    return digest.hexdigest()


def preflight_tile_archive(
    store: ReferenceBlobStore,
    *,
    source_document: object,
    max_archive_bytes: int,
    fetcher: TileFetcher | None = None,
    sample_limit: int = DEFAULT_PREFLIGHT_SAMPLES,
    concurrency: int = 4,
) -> TileSeedPreflight:
    """Sample a reviewed pyramid and reject infeasible seeds before bulk I/O."""

    descriptor = parse_tile_source_document(source_document)
    maximum = _archive_byte_limit(store, max_archive_bytes)
    workers = _integer(concurrency, "tile concurrency", 1, 16)
    samples = _integer(sample_limit, "preflight sample limit", 1, 256)
    result, _ = _run_preflight(
        store,
        descriptor,
        max_archive_bytes=maximum,
        fetcher=fetcher or _https_fetcher(descriptor),
        sample_limit=samples,
        concurrency=workers,
    )
    return result


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
    max_archive_bytes = _archive_byte_limit(store, max_archive_bytes)
    concurrency = _integer(concurrency, "tile concurrency", 1, 16)
    batch_size = _integer(batch_size, "tile batch size", concurrency, 64)
    expected_coordinate_sha256 = coordinate_sha256(
        iter_tile_coordinates(descriptor)
    )
    tile_fetcher = fetcher or _https_fetcher(descriptor)
    preflight, prefetched = _run_preflight(
        store,
        descriptor,
        max_archive_bytes=max_archive_bytes,
        fetcher=tile_fetcher,
        sample_limit=DEFAULT_PREFLIGHT_SAMPLES,
        concurrency=concurrency,
    )
    staging_root = store.root / "staging"
    try:
        with tempfile.TemporaryDirectory(
            prefix="tile-seed-",
            dir=staging_root,
        ) as directory:
            archive = Path(directory, "snapshot.mbtiles")
            _write_archive(
                store,
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
                prefetched=prefetched,
            )
            blob = store.commit_staged_file(
                archive,
                max_bytes=max_archive_bytes,
            )
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
            "capacity_preflight": {
                "sample_count": preflight.sample_count,
                "sample_bytes": preflight.sample_bytes,
                "largest_tile_bytes": preflight.largest_tile_bytes,
                "projected_archive_bytes": preflight.projected_archive_bytes,
            },
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


def _archive_byte_limit(store: ReferenceBlobStore, value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= store.max_blob_bytes
    ):
        raise TileSeedError("tile archive byte limit is invalid")
    return value


def _run_preflight(
    store: ReferenceBlobStore,
    descriptor: TileSourceDescriptor,
    *,
    max_archive_bytes: int,
    fetcher: TileFetcher,
    sample_limit: int,
    concurrency: int,
) -> tuple[TileSeedPreflight, dict[TileCoordinate, bytes]]:
    coordinates = _sample_coordinates(descriptor, sample_limit)
    if descriptor.protocol == "wms_tiles" and descriptor.wms_supertile_size > 1:
        prefetched = _fetch_wms_preflight_samples(
            descriptor,
            coordinates,
            fetcher,
            concurrency=concurrency,
        )
        bodies = [prefetched[coordinate] for coordinate in coordinates]
    else:
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="reference-tile-preflight",
        ) as executor:
            bodies = list(
                executor.map(
                    lambda item: _fetch_validated_tile(descriptor, item, fetcher),
                    coordinates,
                )
            )
        prefetched = dict(zip(coordinates, bodies, strict=True))
    if not bodies:
        raise TileSeedError("tile preflight did not select any coverage")
    sizes = [len(body) for body in bodies]
    if descriptor.estimated_tile_count <= len(coordinates):
        projected = (
            PREFLIGHT_FIXED_BYTES
            + sum(sizes)
            + descriptor.estimated_tile_count * PREFLIGHT_TILE_OVERHEAD_BYTES
        )
    else:
        conservative_payload = max(
            max(sizes) * 2,
            math.ceil(sum(sizes) / len(sizes) * 2),
        )
        projected = (
            PREFLIGHT_FIXED_BYTES
            + descriptor.estimated_tile_count
            * (conservative_payload + PREFLIGHT_TILE_OVERHEAD_BYTES)
        )
    result = TileSeedPreflight(
        tile_count=descriptor.estimated_tile_count,
        sample_count=len(coordinates),
        sample_bytes=sum(sizes),
        largest_tile_bytes=max(sizes),
        projected_archive_bytes=projected,
    )
    if projected > max_archive_bytes:
        raise TileSeedError(
            "tile archive capacity projection exceeds its byte limit"
        )
    store.ensure_capacity(projected)
    return result, prefetched


def _fetch_wms_preflight_samples(
    descriptor: TileSourceDescriptor,
    coordinates: tuple[TileCoordinate, ...],
    fetcher: TileFetcher,
    *,
    concurrency: int,
) -> dict[TileCoordinate, bytes]:
    samples_by_block: dict[_WMSSupertile, list[TileCoordinate]] = {}
    for coordinate in coordinates:
        block = _wms_supertile_for_coordinate(descriptor, coordinate)
        samples_by_block.setdefault(block, []).append(coordinate)
    blocks = tuple(sorted(samples_by_block))
    workers = _wms_worker_count(descriptor, concurrency)
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="reference-wms-preflight",
    ) as executor:
        results = list(
            executor.map(
                lambda block: _fetch_wms_supertile(descriptor, block, fetcher),
                blocks,
            )
        )
    prefetched: dict[TileCoordinate, bytes] = {}
    for block, block_result in zip(blocks, results, strict=True):
        expected = set(_iter_wms_supertile_coordinates(block))
        if set(block_result) != expected:
            raise TileSeedError("WMS preflight supertile coverage is incomplete")
        for coordinate in samples_by_block[block]:
            prefetched[coordinate] = block_result[coordinate]
    if set(prefetched) != set(coordinates):
        raise TileSeedError("WMS preflight sample coverage is incomplete")
    return prefetched


def _sample_coordinates(
    descriptor: TileSourceDescriptor,
    sample_limit: int,
) -> tuple[TileCoordinate, ...]:
    windows = _tile_windows(descriptor)
    counts = [window.tile_count for window in windows]
    total = sum(counts)
    if total != descriptor.estimated_tile_count or total <= 0:
        raise TileSeedError("tile sampling coverage differs from its estimate")
    if total <= sample_limit:
        return tuple(iter_tile_coordinates(descriptor))

    cumulative: list[tuple[int, _TileWindow]] = []
    cursor = 0
    for window in windows:
        cumulative.append((cursor, window))
        cursor += window.tile_count
    if sample_limit <= len(cumulative):
        selected_windows = (
            [cumulative[len(cumulative) // 2]]
            if sample_limit == 1
            else [
                cumulative[index * (len(cumulative) - 1) // (sample_limit - 1)]
                for index in range(sample_limit)
            ]
        )
        ordinals = {
            start + window.tile_count // 2
            for start, window in selected_windows
        }
    else:
        ordinals = {
            start + window.tile_count // 2
            for start, window in cumulative
        }
        if sample_limit == 1:
            candidates = [total // 2]
        else:
            candidates = [
                index * (total - 1) // (sample_limit - 1)
                for index in range(sample_limit)
            ]
        for ordinal in candidates:
            if len(ordinals) >= sample_limit:
                break
            ordinals.add(ordinal)
    selected: list[TileCoordinate] = []
    window_index = 0
    for ordinal in sorted(ordinals):
        while (
            window_index + 1 < len(cumulative)
            and ordinal >= cumulative[window_index + 1][0]
        ):
            window_index += 1
        start, window = cumulative[window_index]
        local = ordinal - start
        height = window.max_y - window.min_y + 1
        selected.append(
            TileCoordinate(
                z=window.zoom,
                x=window.min_x + local // height,
                y=window.min_y + local % height,
                matrix_identifier=window.matrix_identifier,
            )
        )
    return tuple(selected)


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
        return _wms_getmap_url(
            descriptor,
            _WMSSupertile(
                zoom=coordinate.z,
                min_x=coordinate.x,
                max_x=coordinate.x,
                min_y=coordinate.y,
                max_y=coordinate.y,
            ),
        )
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


def _wms_getmap_url(
    descriptor: TileSourceDescriptor,
    block: _WMSSupertile,
) -> str:
    if descriptor.protocol != "wms_tiles":
        raise TileSeedError("WMS supertile request requires a WMS source")
    world_size = 2**block.zoom
    if (
        block.width > descriptor.wms_supertile_size
        or block.height > descriptor.wms_supertile_size
        or not 0 <= block.min_x <= block.max_x < world_size
        or not 0 <= block.min_y <= block.max_y < world_size
    ):
        raise TileSeedError("WMS supertile coordinates are invalid")
    top_left = tile_bbox(block.zoom, block.min_x, block.min_y)
    bottom_right = tile_bbox(block.zoom, block.max_x, block.max_y)
    bbox = (top_left[0], bottom_right[1], bottom_right[2], top_left[3])
    payload = descriptor.payload
    kvp = _mapping(payload["kvp"], "WMS KVP descriptor")
    version = str(kvp["version"])
    parameters = {
        "SERVICE": "WMS",
        "REQUEST": "GetMap",
        "VERSION": version,
        "LAYERS": descriptor.layer,
        "STYLES": str(payload["style"]),
        "FORMAT": descriptor.media_type,
        "TRANSPARENT": str(kvp["transparent"]),
        "BBOX": ",".join(format(item, ".12f") for item in bbox),
        "WIDTH": str(block.width * TILE_SIZE),
        "HEIGHT": str(block.height * TILE_SIZE),
        "CRS" if version == "1.3.0" else "SRS": "EPSG:3857",
    }
    return _merge_query(str(kvp["endpoint_url"]), parameters)


def _iter_wms_supertile_coordinates(
    block: _WMSSupertile,
) -> Iterator[TileCoordinate]:
    for x in range(block.min_x, block.max_x + 1):
        for y in range(block.min_y, block.max_y + 1):
            yield TileCoordinate(block.zoom, x, y)


def _iter_wms_supertile_blocks(
    descriptor: TileSourceDescriptor,
) -> Iterator[_WMSSupertile]:
    size = descriptor.wms_supertile_size
    for window in _tile_windows(descriptor):
        for min_x in range(window.min_x, window.max_x + 1, size):
            max_x = min(window.max_x, min_x + size - 1)
            for min_y in range(window.min_y, window.max_y + 1, size):
                yield _WMSSupertile(
                    zoom=window.zoom,
                    min_x=min_x,
                    max_x=max_x,
                    min_y=min_y,
                    max_y=min(window.max_y, min_y + size - 1),
                )


def _wms_supertile_for_coordinate(
    descriptor: TileSourceDescriptor,
    coordinate: TileCoordinate,
) -> _WMSSupertile:
    size = descriptor.wms_supertile_size
    for window in _tile_windows(descriptor):
        if (
            window.zoom == coordinate.z
            and window.min_x <= coordinate.x <= window.max_x
            and window.min_y <= coordinate.y <= window.max_y
        ):
            min_x = window.min_x + ((coordinate.x - window.min_x) // size) * size
            min_y = window.min_y + ((coordinate.y - window.min_y) // size) * size
            return _WMSSupertile(
                zoom=coordinate.z,
                min_x=min_x,
                max_x=min(window.max_x, min_x + size - 1),
                min_y=min_y,
                max_y=min(window.max_y, min_y + size - 1),
            )
    raise TileSeedError("WMS sample coordinate is outside reviewed coverage")


def _write_archive(
    store: ReferenceBlobStore,
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
    prefetched: Mapping[TileCoordinate, bytes],
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
        if descriptor.protocol == "wms_tiles" and descriptor.wms_supertile_size > 1:
            completed = _write_wms_supertile_rows(
                store,
                connection,
                descriptor,
                fetcher,
                total_tiles=total_tiles,
                max_archive_bytes=max_archive_bytes,
                heartbeat=heartbeat,
                concurrency=concurrency,
                batch_size=batch_size,
                prefetched=prefetched,
            )
        else:
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
                            ) if item not in prefetched else prefetched[item],
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
                    if completed == total_tiles or completed % 8192 < len(batch):
                        store.ensure_capacity(0)
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


def _write_wms_supertile_rows(
    store: ReferenceBlobStore,
    connection: sqlite3.Connection,
    descriptor: TileSourceDescriptor,
    fetcher: TileFetcher,
    *,
    total_tiles: int,
    max_archive_bytes: int,
    heartbeat: Heartbeat | None,
    concurrency: int,
    batch_size: int,
    prefetched: Mapping[TileCoordinate, bytes],
) -> int:
    blocks = _iter_wms_supertile_blocks(descriptor)
    workers = _wms_worker_count(descriptor, concurrency)
    blocks_per_batch = max(1, min(workers, batch_size))
    completed = 0

    def load(block: _WMSSupertile) -> dict[TileCoordinate, bytes]:
        coordinates = tuple(_iter_wms_supertile_coordinates(block))
        if all(coordinate in prefetched for coordinate in coordinates):
            return {coordinate: prefetched[coordinate] for coordinate in coordinates}
        return _fetch_wms_supertile(descriptor, block, fetcher)

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="reference-wms-supertile",
    ) as executor:
        while True:
            batch = list(islice(blocks, blocks_per_batch))
            if not batch:
                break
            results = list(executor.map(load, batch))
            rows: list[tuple[int, int, int, bytes]] = []
            for block, result in zip(batch, results, strict=True):
                coordinates = tuple(_iter_wms_supertile_coordinates(block))
                if set(result) != set(coordinates):
                    raise TileSeedError("WMS supertile coverage is incomplete")
                rows.extend(
                    (
                        coordinate.z,
                        coordinate.x,
                        (2**coordinate.z - 1) - coordinate.y,
                        result[coordinate],
                    )
                    for coordinate in coordinates
                )
            if _sqlite_size(connection) + sum(len(row[3]) for row in rows) > max_archive_bytes:
                raise TileSeedError("tile archive exceeds its byte limit")
            connection.executemany(
                "INSERT INTO tiles "
                "(zoom_level, tile_column, tile_row, tile_data) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            connection.commit()
            if _sqlite_size(connection) > max_archive_bytes:
                raise TileSeedError("tile archive exceeds its byte limit")
            completed += len(rows)
            if completed > total_tiles:
                raise TileSeedError("WMS supertile coverage exceeds its estimate")
            if completed == total_tiles or completed % 8192 < len(rows):
                store.ensure_capacity(0)
            if heartbeat is not None:
                heartbeat(completed, total_tiles)
    return completed


def _wms_worker_count(
    descriptor: TileSourceDescriptor,
    concurrency: int,
) -> int:
    maximum_block_bytes = (
        MAX_TILE_BYTES
        * descriptor.wms_supertile_size
        * descriptor.wms_supertile_size
    )
    memory_bound = max(1, MAX_INFLIGHT_WMS_BYTES // maximum_block_bytes)
    return max(1, min(concurrency, memory_bound))


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


def _fetch_wms_supertile(
    descriptor: TileSourceDescriptor,
    block: _WMSSupertile,
    fetcher: TileFetcher,
) -> dict[TileCoordinate, bytes]:
    """Fetch one bounded GetMap, reducing its dimensions only when classified.

    A rejected or malformed large GetMap is split along the reviewed power-of-two
    ladder.  Retryable transport failures are never disguised as a dimension
    limitation, and a failure at 256px is returned to the caller unchanged.
    """

    try:
        body = fetcher(_wms_getmap_url(descriptor, block), descriptor.media_type)
        return _crop_wms_supertile(descriptor, block, body)
    except (
        TileSeedError,
        DownloadHTTPError,
        DownloadIntegrityError,
        DownloadLimitError,
    ) as error:
        if block.tile_count == 1 or not _can_reduce_wms_getmap(error):
            raise
        next_size = max(
            size
            for size in (4, 2, 1)
            if size < max(block.width, block.height)
        )
        result: dict[TileCoordinate, bytes] = {}
        for child in _split_wms_supertile(block, next_size):
            child_result = _fetch_wms_supertile(descriptor, child, fetcher)
            overlap = set(result) & set(child_result)
            if overlap:
                raise TileSeedError("WMS supertile fallback overlaps coordinates")
            result.update(child_result)
        expected = set(_iter_wms_supertile_coordinates(block))
        if set(result) != expected:
            raise TileSeedError("WMS supertile fallback is incomplete") from error
        return result


def _can_reduce_wms_getmap(error: BaseException) -> bool:
    if isinstance(error, TileSeedError):
        return True
    if isinstance(error, DownloadHTTPError):
        return error.status_code in {400, 413, 414, 422}
    if isinstance(error, DownloadLimitError):
        return error.code == "response_too_large"
    if isinstance(error, DownloadIntegrityError):
        return error.code == "content_type"
    return False


def _split_wms_supertile(
    block: _WMSSupertile,
    size: int,
) -> Iterator[_WMSSupertile]:
    for min_x in range(block.min_x, block.max_x + 1, size):
        for min_y in range(block.min_y, block.max_y + 1, size):
            yield _WMSSupertile(
                zoom=block.zoom,
                min_x=min_x,
                max_x=min(block.max_x, min_x + size - 1),
                min_y=min_y,
                max_y=min(block.max_y, min_y + size - 1),
            )


def _crop_wms_supertile(
    descriptor: TileSourceDescriptor,
    block: _WMSSupertile,
    body: bytes,
) -> dict[TileCoordinate, bytes]:
    maximum_bytes = min(
        MAX_WMS_SUPERTILE_BYTES,
        MAX_TILE_BYTES * block.tile_count,
    )
    if not isinstance(body, bytes) or not 0 < len(body) <= maximum_bytes:
        raise TileSeedError("WMS supertile returned invalid image bytes")
    expected_format = "PNG" if descriptor.image_format == "png" else "JPEG"
    expected_size = (block.width * TILE_SIZE, block.height * TILE_SIZE)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                if image.format != expected_format or image.size != expected_size:
                    raise TileSeedError(
                        "WMS supertile image format or dimensions are inconsistent"
                    )
                image.verify()
            with Image.open(BytesIO(body)) as source:
                if source.format != expected_format or source.size != expected_size:
                    raise TileSeedError(
                        "WMS supertile image format or dimensions are inconsistent"
                    )
                source.load()
                result = {
                    coordinate: _encode_wms_tile(
                        descriptor,
                        source.crop(
                            (
                                (coordinate.x - block.min_x) * TILE_SIZE,
                                (coordinate.y - block.min_y) * TILE_SIZE,
                                (coordinate.x - block.min_x + 1) * TILE_SIZE,
                                (coordinate.y - block.min_y + 1) * TILE_SIZE,
                            )
                        ),
                    )
                    for coordinate in _iter_wms_supertile_coordinates(block)
                }
    except TileSeedError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as error:
        raise TileSeedError("WMS supertile image could not be decoded safely") from error
    if len(result) != block.tile_count:
        raise TileSeedError("WMS supertile crop is incomplete")
    return result


def _encode_wms_tile(
    descriptor: TileSourceDescriptor,
    image: Image.Image,
) -> bytes:
    output = BytesIO()
    if descriptor.image_format == "png":
        transparent = _mapping(
            descriptor.payload["kvp"],
            "WMS KVP descriptor",
        )["transparent"] == "TRUE"
        if transparent:
            rendered = image.convert("RGBA")
        elif image.mode not in {"RGB", "RGBA"}:
            rendered = image.convert("RGB")
        else:
            rendered = image
        rendered.save(output, format="PNG", optimize=False, compress_level=6)
    else:
        image.convert("RGB").save(
            output,
            format="JPEG",
            quality=85,
            optimize=False,
            progressive=False,
        )
    payload = output.getvalue()
    try:
        validate_tile_image(payload, descriptor.image_format)
    except Exception as error:
        raise TileSeedError("WMS supertile crop produced an invalid tile") from error
    return payload


def _https_fetcher(descriptor: TileSourceDescriptor) -> TileFetcher:
    sample = tile_url(descriptor, next(iter_tile_coordinates(descriptor)))
    origin = _origin(sample)
    downloader = SafeHTTPSDownloader(
        HTTPSDownloadPolicy(
            allowed_origins=(origin,),
            max_response_bytes=(
                MAX_TILE_BYTES
                * descriptor.wms_supertile_size
                * descriptor.wms_supertile_size
                if descriptor.protocol == "wms_tiles"
                else MAX_TILE_BYTES
            ),
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
        accepted_types = (
            {"image/jpeg", "image/jpg"}
            if media_type in {"image/jpeg", "image/jpg"}
            else {media_type}
        )
        if result.content_type not in accepted_types:
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
) -> int:
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
    supertile_size = descriptor.get("wms_supertile_size", 1)
    if (
        isinstance(supertile_size, bool)
        or not isinstance(supertile_size, int)
        or supertile_size not in {1, 2, 4, 8}
    ):
        raise TileSeedError("WMS supertile size is invalid")
    if supertile_size > 1 and descriptor.get("coverage_profile") not in {
        SIUR_TILE_PROFILE,
        SIUR_ORTHO_TILE_PROFILE,
    }:
        raise TileSeedError(
            "WMS supertiles require a reviewed SIUR coverage profile"
        )
    return supertile_size


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
