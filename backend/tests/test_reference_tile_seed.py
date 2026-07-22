import hashlib
from pathlib import Path
import sqlite3
import struct
from urllib.parse import parse_qs, urlsplit
import zlib

import pytest

from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.tile_seed import (
    TileCoordinate,
    TileSeedError,
    coordinate_sha256,
    iter_tile_coordinates,
    parse_tile_source_document,
    seed_tile_archive,
    tile_url,
)


DEFINITION_SHA256 = "a" * 64


def _png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 256, 256, 8, 6, 0, 0, 0)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\x00\x00\x00\xff" * 256 for _ in range(256))
    return (
        signature
        + chunk(b"IHDR", ihdr_data)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _common(*, estimated: int, minimum: int = 0, maximum: int = 0):
    return {
        "bounds": {"west": -180.0, "south": -85.0, "east": 180.0, "north": 85.0},
        "min_zoom": minimum,
        "max_zoom": maximum,
        "format": "image/png",
        "coverage_required": True,
        "estimated_tile_count": estimated,
        "max_tile_count": 100,
        "layer": "planning:zones",
    }


def _xyz_document(*, estimated: int = 1, minimum: int = 0, maximum: int = 0):
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "xyz",
        "definition_sha256": DEFINITION_SHA256,
        "descriptor": {
            **_common(estimated=estimated, minimum=minimum, maximum=maximum),
            "url_template": "https://tiles.example.test/base/{z}/{x}/{y}.png",
            "scheme": "xyz",
        },
    }


def _wms_document():
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "wms_tiles",
        "definition_sha256": DEFINITION_SHA256,
        "descriptor": {
            **_common(estimated=1),
            "style": "approved",
            "crs": "EPSG:3857",
            "kvp": {
                "endpoint_url": "https://maps.example.test/geoserver/ows",
                "service": "WMS",
                "request": "GetMap",
                "version": "1.3.0",
                "layers": "planning:zones",
                "styles": "approved",
                "format": "image/png",
                "transparent": "TRUE",
                "CRS": "EPSG:3857",
                "bbox_placeholder": "{bbox}",
                "width_placeholder": "{width}",
                "height_placeholder": "{height}",
            },
        },
    }


def _wmts_document():
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "wmts",
        "definition_sha256": DEFINITION_SHA256,
        "descriptor": {
            **_common(estimated=1, minimum=1, maximum=1),
            "style": "default",
            "tile_matrix_sets": ["google-grid"],
            "selected_tile_matrix_set": "google-grid",
            "tile_matrix_set": {
                "identifier": "google-grid",
                "supported_crs": "urn:ogc:def:crs:EPSG::3857",
                "well_known_scale_set": "urn:ogc:def:wkss:OGC:1.0:GoogleMapsCompatible",
                "tile_matrices": [
                    {
                        "identifier": "EPSG:3857:1",
                        "zoom": 1,
                        "scale_denominator": 279541132.014,
                        "top_left_corner": [-20037508.342789244, 20037508.342789244],
                        "tile_width": 256,
                        "tile_height": 256,
                        "matrix_width": 2,
                        "matrix_height": 2,
                    }
                ],
            },
            "tile_matrix_limits": [
                {
                    "tile_matrix": "EPSG:3857:1",
                    "min_tile_row": 0,
                    "max_tile_row": 0,
                    "min_tile_col": 0,
                    "max_tile_col": 0,
                }
            ],
            "resource_urls": [
                {
                    "template": (
                        "https://wmts.example.test/tiles/{TileMatrixSet}/"
                        "{TileMatrix}/{TileRow}/{TileCol}.png"
                    ),
                    "resource_type": "tile",
                    "format": "image/png",
                }
            ],
            "kvp": {
                "endpoint_url": "https://wmts.example.test/service",
                "service": "WMTS",
                "request": "GetTile",
                "version": "1.0.0",
                "layer": "planning:zones",
                "style": "default",
                "format": "image/png",
                "tile_matrix_set": "google-grid",
                "tile_matrix_placeholder": "{TileMatrix}",
                "tile_row_placeholder": "{TileRow}",
                "tile_col_placeholder": "{TileCol}",
            },
        },
    }


def test_xyz_descriptor_has_exact_stable_coverage_and_hash() -> None:
    descriptor = parse_tile_source_document(
        _xyz_document(estimated=5, minimum=0, maximum=1)
    )
    coordinates = list(iter_tile_coordinates(descriptor))

    assert coordinates == [
        TileCoordinate(0, 0, 0, None),
        TileCoordinate(1, 0, 0, None),
        TileCoordinate(1, 0, 1, None),
        TileCoordinate(1, 1, 0, None),
        TileCoordinate(1, 1, 1, None),
    ]
    expected = hashlib.sha256(
        b"0/0/0\n1/0/0\n1/0/1\n1/1/0\n1/1/1\n"
    ).hexdigest()
    assert coordinate_sha256(iter(coordinates)) == expected


def test_descriptor_rejects_estimate_drift_and_unsafe_templates() -> None:
    with pytest.raises(TileSeedError, match="estimate"):
        parse_tile_source_document(
            _xyz_document(estimated=4, minimum=0, maximum=1)
        )

    unsafe = _xyz_document()
    unsafe["descriptor"]["url_template"] = (
        "https://user:secret@tiles.example.test/{z}/{x}/{y}.png"
    )
    with pytest.raises(TileSeedError, match="credential-free"):
        parse_tile_source_document(unsafe)

    tms = _xyz_document()
    tms["descriptor"]["scheme"] = "tms"
    tms["descriptor"]["url_template"] = (
        "https://tiles.example.test/base/{z}/{x}/{-y}.png"
    )
    descriptor = parse_tile_source_document(tms)
    assert tile_url(descriptor, TileCoordinate(0, 0, 0)) == (
        "https://tiles.example.test/base/0/0/0.png"
    )


def test_wms_url_is_built_only_from_reviewed_fields() -> None:
    descriptor = parse_tile_source_document(_wms_document())
    url = tile_url(descriptor, next(iter_tile_coordinates(descriptor)))
    query = parse_qs(urlsplit(url).query)

    assert urlsplit(url).netloc == "maps.example.test"
    assert query["SERVICE"] == ["WMS"]
    assert query["LAYERS"] == ["planning:zones"]
    assert query["STYLES"] == ["approved"]
    assert query["CRS"] == ["EPSG:3857"]
    assert query["WIDTH"] == ["256"]
    assert query["HEIGHT"] == ["256"]


def test_wmts_uses_arbitrary_matrix_identifier_and_reviewed_limits() -> None:
    descriptor = parse_tile_source_document(_wmts_document())
    coordinates = list(iter_tile_coordinates(descriptor))

    assert coordinates == [TileCoordinate(1, 0, 0, "EPSG:3857:1")]
    assert tile_url(descriptor, coordinates[0]) == (
        "https://wmts.example.test/tiles/google-grid/"
        "EPSG:3857:1/0/0.png"
    )


def test_seed_builds_complete_indexed_content_addressed_archive(tmp_path) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=4 * 1024 * 1024,
    )
    requested: list[str] = []
    heartbeats: list[tuple[int, int]] = []

    def fetcher(url: str, media_type: str) -> bytes:
        requested.append(url)
        assert media_type == "image/png"
        return _png()

    result = seed_tile_archive(
        store,
        source_document=_xyz_document(estimated=4, minimum=1, maximum=1),
        max_archive_bytes=3 * 1024 * 1024,
        fetcher=fetcher,
        heartbeat=lambda complete, total: heartbeats.append((complete, total)),
        concurrency=2,
        batch_size=2,
    )

    assert result.tile_count == 4
    assert len(requested) == 4
    assert heartbeats == [(0, 4), (2, 4), (4, 4)]
    archive = store.resolve_blob(result.blob.storage_key)
    connection = sqlite3.connect(archive)
    try:
        assert connection.execute("SELECT count(*) FROM tiles").fetchone() == (4,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE name='coordinate_sha256'"
        ).fetchone() == (result.coordinate_sha256,)
        indexes = connection.execute("PRAGMA index_list(tiles)").fetchall()
        assert any(row[1] == "tile_index" and row[2] == 1 for row in indexes)
        rows = connection.execute(
            "SELECT zoom_level, tile_column, tile_row FROM tiles "
            "ORDER BY zoom_level, tile_column, tile_row"
        ).fetchall()
        assert rows == [(1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1)]
    finally:
        connection.close()
        store.close()


def test_seed_rejects_invalid_image_without_publishing_a_blob(tmp_path) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=1024 * 1024,
    )
    with pytest.raises(TileSeedError, match="invalid image"):
        seed_tile_archive(
            store,
            source_document=_xyz_document(),
            max_archive_bytes=512 * 1024,
            fetcher=lambda _url, _media_type: b"not-an-image",
        )
    assert not list((store.root / "blobs" / "sha256").rglob("?" * 64))
    store.close()
