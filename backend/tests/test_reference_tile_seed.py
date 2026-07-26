import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
import struct
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import zlib

import pytest
from PIL import Image

import app.reference_layers.blob_store as blob_store_module
import app.reference_layers.tile_seed as tile_seed_module
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceStorageQuotaError,
    ReferenceStorageSpaceError,
)
from app.reference_layers.tile_seed import (
    PREFLIGHT_PROJECTION_SCHEMA,
    TileCoordinate,
    TileSeedError,
    coordinate_sha256,
    iter_tile_coordinates,
    parse_tile_source_document,
    preflight_tile_archive,
    seed_tile_archive,
    tile_url,
)
from app.reference_layers.wms_proxy import tile_bbox


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


def _siur_xyz_document():
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "xyz",
        "definition_sha256": DEFINITION_SHA256,
        "descriptor": {
            "bounds": {
                "west": -7.6,
                "south": 39.9,
                "east": -1.3,
                "north": 43.4,
            },
            "min_zoom": 0,
            "max_zoom": 16,
            "format": "image/png",
            "coverage_required": True,
            "estimated_tile_count": 1_308_502,
            "max_tile_count": 2_000_000,
            "layer": "IGNBaseTodo-nofondo",
            "url_template": "https://tiles.example.test/base/{z}/{x}/{y}.png",
            "scheme": "xyz",
        },
    }


def _wms_document(
    *,
    estimated: int = 1,
    minimum: int = 0,
    maximum: int = 0,
    bounds: dict[str, float] | None = None,
    image_format: str = "image/png",
    supertile_size: int | None = None,
):
    descriptor = {
        **_common(estimated=estimated, minimum=minimum, maximum=maximum),
        "style": "approved",
        "crs": "EPSG:3857",
        "kvp": {
            "endpoint_url": "https://maps.example.test/geoserver/ows",
            "service": "WMS",
            "request": "GetMap",
            "version": "1.3.0",
            "layers": "planning:zones",
            "styles": "approved",
            "format": image_format,
            "transparent": "FALSE" if image_format == "image/jpeg" else "TRUE",
            "CRS": "EPSG:3857",
            "bbox_placeholder": "{bbox}",
            "width_placeholder": "{width}",
            "height_placeholder": "{height}",
        },
    }
    descriptor["format"] = image_format
    if bounds is not None:
        descriptor["bounds"] = bounds
    if supertile_size is not None:
        descriptor["coverage_profile"] = (
            "siur-castilla-y-leon-native-z16-v1"
        )
        descriptor["wms_supertile_size"] = supertile_size
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "wms_tiles",
        "definition_sha256": DEFINITION_SHA256,
        "descriptor": descriptor,
    }


def _image(
    width: int,
    height: int,
    *,
    image_format: str = "PNG",
    transparent: bool = False,
) -> bytes:
    mode = "RGBA" if image_format == "PNG" else "RGB"
    color = (18, 52, 86, 0 if transparent else 255)
    if mode == "RGB":
        color = color[:3]
    image = Image.new(mode, (width, height), color)
    target = BytesIO()
    image.save(target, format=image_format)
    return target.getvalue()


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


def test_wms_supertile_descriptor_is_strict_and_power_of_two() -> None:
    descriptor = parse_tile_source_document(
        _wms_document(supertile_size=8)
    )
    assert descriptor.wms_supertile_size == 8
    assert descriptor.payload["coverage_profile"].startswith("siur-")

    invalid = _wms_document(supertile_size=8)
    invalid["descriptor"]["wms_supertile_size"] = 3
    with pytest.raises(TileSeedError, match="supertile size"):
        parse_tile_source_document(invalid)

    wrong_protocol = _xyz_document()
    wrong_protocol["descriptor"]["wms_supertile_size"] = 8
    with pytest.raises(TileSeedError, match="keys"):
        parse_tile_source_document(wrong_protocol)


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


def test_wms_supertile_reduces_upstream_calls_without_changing_coverage(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=16 * 1024 * 1024,
    )
    requested: list[str] = []
    document = _wms_document(
        estimated=64,
        minimum=3,
        maximum=3,
        supertile_size=8,
    )

    def fetcher(url: str, media_type: str) -> bytes:
        requested.append(url)
        query = parse_qs(urlsplit(url).query)
        assert media_type == "image/png"
        return _image(int(query["WIDTH"][0]), int(query["HEIGHT"][0]))

    try:
        result = seed_tile_archive(
            store,
            source_document=document,
            max_archive_bytes=12 * 1024 * 1024,
            fetcher=fetcher,
            concurrency=4,
        )
        descriptor = parse_tile_source_document(document)
        assert len(requested) == 1
        assert parse_qs(urlsplit(requested[0]).query)["WIDTH"] == ["2048"]
        assert result.tile_count == 64
        assert result.coordinate_sha256 == coordinate_sha256(
            iter_tile_coordinates(descriptor)
        )
        connection = sqlite3.connect(store.resolve_blob(result.blob.storage_key))
        try:
            assert connection.execute("SELECT count(*) FROM tiles").fetchone() == (
                64,
            )
        finally:
            connection.close()
    finally:
        store.close()


def test_wms_supertile_uses_exact_partial_edge_dimensions_and_bbox(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=8 * 1024 * 1024,
    )
    requested: list[str] = []
    document = _wms_document(
        estimated=20,
        minimum=3,
        maximum=3,
        bounds={"west": -180.0, "south": 0.0, "east": 45.0, "north": 85.0},
        supertile_size=8,
    )

    def fetcher(url: str, _media_type: str) -> bytes:
        requested.append(url)
        query = parse_qs(urlsplit(url).query)
        return _image(int(query["WIDTH"][0]), int(query["HEIGHT"][0]))

    try:
        result = seed_tile_archive(
            store,
            source_document=document,
            max_archive_bytes=6 * 1024 * 1024,
            fetcher=fetcher,
        )
        assert len(requested) == 1
        query = parse_qs(urlsplit(requested[0]).query)
        assert query["WIDTH"] == ["1280"]
        assert query["HEIGHT"] == ["1024"]
        top_left = tile_bbox(3, 0, 0)
        bottom_right = tile_bbox(3, 4, 3)
        expected_bbox = (
            top_left[0],
            bottom_right[1],
            bottom_right[2],
            top_left[3],
        )
        assert query["BBOX"] == [
            ",".join(format(value, ".12f") for value in expected_bbox)
        ]
        assert result.tile_count == 20
    finally:
        store.close()


@pytest.mark.parametrize(
    ("media_type", "pillow_format", "transparent"),
    [
        ("image/png", "PNG", True),
        ("image/jpeg", "JPEG", False),
    ],
)
def test_wms_supertile_crops_png_alpha_and_jpeg_to_standard_tiles(
    tmp_path,
    media_type,
    pillow_format,
    transparent,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=8 * 1024 * 1024,
    )
    document = _wms_document(
        estimated=4,
        minimum=1,
        maximum=1,
        image_format=media_type,
        supertile_size=8,
    )
    requested: list[str] = []

    def fetcher(url: str, _media_type: str) -> bytes:
        requested.append(url)
        query = parse_qs(urlsplit(url).query)
        return _image(
            int(query["WIDTH"][0]),
            int(query["HEIGHT"][0]),
            image_format=pillow_format,
            transparent=transparent,
        )

    try:
        result = seed_tile_archive(
            store,
            source_document=document,
            max_archive_bytes=6 * 1024 * 1024,
            fetcher=fetcher,
        )
        assert len(requested) == 1
        query = parse_qs(urlsplit(requested[0]).query)
        assert query["TRANSPARENT"] == ["TRUE" if transparent else "FALSE"]
        connection = sqlite3.connect(store.resolve_blob(result.blob.storage_key))
        try:
            body = connection.execute(
                "SELECT tile_data FROM tiles ORDER BY zoom_level, tile_column, tile_row LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        with Image.open(BytesIO(body)) as tile:
            assert tile.format == pillow_format
            assert tile.size == (256, 256)
            if transparent:
                assert tile.convert("RGBA").getpixel((0, 0))[3] == 0
    finally:
        store.close()


def test_wms_supertile_dimension_rejection_reduces_on_a_bounded_ladder(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=16 * 1024 * 1024,
    )
    requested_widths: list[int] = []
    document = _wms_document(
        estimated=64,
        minimum=3,
        maximum=3,
        supertile_size=8,
    )

    def fetcher(url: str, _media_type: str) -> bytes:
        width = int(parse_qs(urlsplit(url).query)["WIDTH"][0])
        requested_widths.append(width)
        return _image(256, 256)

    try:
        result = seed_tile_archive(
            store,
            source_document=document,
            max_archive_bytes=12 * 1024 * 1024,
            fetcher=fetcher,
        )
        assert result.tile_count == 64
        assert requested_widths.count(2048) == 1
        assert requested_widths.count(1024) == 4
        assert requested_widths.count(512) == 16
        assert requested_widths.count(256) == 64
        assert set(requested_widths) == {2048, 1024, 512, 256}
    finally:
        store.close()


def test_wms_supertile_rejects_wrong_dimensions_at_single_tile(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=4 * 1024 * 1024,
    )
    try:
        with pytest.raises(TileSeedError, match="dimensions"):
            seed_tile_archive(
                store,
                source_document=_wms_document(supertile_size=8),
                max_archive_bytes=3 * 1024 * 1024,
                fetcher=lambda _url, _media_type: _image(128, 128),
            )
        assert not list((store.root / "blobs" / "sha256").rglob("?" * 64))
    finally:
        store.close()


def test_wms_without_opt_in_keeps_one_getmap_per_tile(tmp_path) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=8 * 1024 * 1024,
    )
    requested: list[str] = []

    def fetcher(url: str, _media_type: str) -> bytes:
        requested.append(url)
        return _png()

    try:
        result = seed_tile_archive(
            store,
            source_document=_wms_document(
                estimated=4,
                minimum=1,
                maximum=1,
            ),
            max_archive_bytes=6 * 1024 * 1024,
            fetcher=fetcher,
        )
        assert result.tile_count == 4
        assert len(requested) == 4
        assert {
            parse_qs(urlsplit(url).query)["WIDTH"][0] for url in requested
        } == {"256"}
    finally:
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


def test_preflight_samples_large_coverage_and_rejects_capacity_before_bulk_seed(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=4 * 1024 * 1024,
    )
    requested: list[str] = []

    def fetcher(url: str, _media_type: str) -> bytes:
        requested.append(url)
        return _png()

    try:
        with pytest.raises(TileSeedError, match="capacity projection"):
            preflight_tile_archive(
                store,
                source_document=_xyz_document(
                    estimated=4,
                    minimum=1,
                    maximum=1,
                ),
                max_archive_bytes=64 * 1024,
                fetcher=fetcher,
                sample_limit=2,
                concurrency=2,
            )
        assert len(requested) == 2
        assert not list((store.root / "blobs" / "sha256").rglob("?" * 64))
    finally:
        store.close()


def test_preflight_reports_conservative_projection_without_writing(tmp_path) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "store"),
        max_blob_bytes=4 * 1024 * 1024,
    )
    try:
        result = preflight_tile_archive(
            store,
            source_document=_xyz_document(
                estimated=4,
                minimum=1,
                maximum=1,
            ),
            max_archive_bytes=3 * 1024 * 1024,
            fetcher=lambda _url, _media_type: _png(),
            sample_limit=2,
        )
        assert result.tile_count == 4
        assert result.sample_count == 2
        assert result.largest_tile_bytes == len(_png())
        assert result.projected_payload_bytes >= result.sample_bytes
        assert result.projected_archive_bytes > result.sample_bytes
        assert result.projection_schema == PREFLIGHT_PROJECTION_SCHEMA
        assert not list((store.root / "blobs" / "sha256").rglob("?" * 64))
    finally:
        store.close()


def test_preflight_stratified_upper_mean_does_not_apply_one_png_outlier_to_all_tiles(
) -> None:
    descriptor = parse_tile_source_document(_siur_xyz_document())
    coordinates = tile_seed_module._sample_coordinates(descriptor, 64)
    sizes = [23_000] * len(coordinates)
    highest_zoom_index = max(
        index
        for index, coordinate in enumerate(coordinates)
        if coordinate.z == descriptor.max_zoom
    )
    sizes[highest_zoom_index] = 75_000

    projected_payload = tile_seed_module._project_tile_payload_bytes(
        descriptor,
        coordinates,
        sizes,
    )
    projected_archive = (
        tile_seed_module.PREFLIGHT_FIXED_BYTES
        + projected_payload
        + descriptor.estimated_tile_count
        * tile_seed_module.PREFLIGHT_TILE_OVERHEAD_BYTES
    )
    previous_largest_tile_projection = (
        tile_seed_module.PREFLIGHT_FIXED_BYTES
        + descriptor.estimated_tile_count
        * (
            max(sizes) * 2
            + tile_seed_module.PREFLIGHT_TILE_OVERHEAD_BYTES
        )
    )

    assert len(coordinates) == 64
    assert 30 * 10**9 < projected_archive < 60 * 10**9
    assert projected_archive < previous_largest_tile_projection // 3


def test_preflight_projection_still_enforces_real_store_quota(tmp_path) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "quota-store"),
        max_blob_bytes=128 * 1024,
        quota_bytes=128 * 1024,
    )
    try:
        store.put_stream(BytesIO(b"x" * 70_000))
        with pytest.raises(ReferenceStorageQuotaError):
            preflight_tile_archive(
                store,
                source_document=_xyz_document(
                    estimated=4,
                    minimum=1,
                    maximum=1,
                ),
                max_archive_bytes=120 * 1024,
                fetcher=lambda _url, _media_type: _png(),
            )
    finally:
        store.close()


def test_preflight_projection_still_enforces_configured_free_space_reserve(
    tmp_path,
    monkeypatch,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "reserve-store"),
        max_blob_bytes=128 * 1024,
        min_free_bytes=64 * 1024,
    )
    monkeypatch.setattr(
        blob_store_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=1024 * 1024,
            used=904 * 1024,
            free=120 * 1024,
        ),
    )
    try:
        with pytest.raises(ReferenceStorageSpaceError):
            preflight_tile_archive(
                store,
                source_document=_xyz_document(
                    estimated=4,
                    minimum=1,
                    maximum=1,
                ),
                max_archive_bytes=120 * 1024,
                fetcher=lambda _url, _media_type: _png(),
            )
    finally:
        store.close()
