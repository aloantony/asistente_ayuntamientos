from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import struct
import zipfile

import pytest

from app.reference_layers.geopackage_archive import (
    GeoPackageArchiveError,
    _validated_entries,
    geopackage_vsi_path,
    inspect_geopackage_zip,
)
from app.reference_layers.acquisition import (
    AcquisitionConfigurationError,
    AcquisitionLimits,
    AcquisitionValidationError,
    ReferenceAcquisitionPipeline,
    source_candidate_definition_sha256,
)
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.safe_download import HTTPSDownloadResult
from app.reference_layers.reviewed_archive_integrity import (
    INTEGRITY_SPEC_SCHEMA,
    canonical_json_sha256 as reviewed_integrity_sha256,
)
from app.reference_layers.source_content_parity import (
    PARITY_SPEC_SCHEMA,
    SourceContentParityError,
    build_promotion_parity_gate,
    canonical_json_sha256,
    evaluate_acquisition_parity,
    valid_acquisition_gate,
    valid_promotion_parity_gate,
)
from app.reference_layers.source_discovery import SourceCandidate


def _geometry(
    *,
    west: float,
    south: float,
    east: float,
    north: float,
) -> bytes:
    return (
        b"GP"
        + bytes((0, 3))
        + struct.pack(
            "<i4d",
            25830,
            west,
            east,
            south,
            north,
        )
        + struct.pack("<BI", 1, 2)
        + struct.pack("<I", 2)
        + struct.pack("<4d", west, south, east, north)
    )


def _archive(
    tmp_path: Path,
    *,
    member: str = "dataset/reviewed.gpkg",
    second_package: bool = False,
) -> Path:
    package = tmp_path / "source.gpkg"
    with sqlite3.connect(package) as connection:
        connection.executescript(
            """
            PRAGMA application_id = 1196444487;
            CREATE TABLE gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL,
                min_x DOUBLE,
                min_y DOUBLE,
                max_x DOUBLE,
                max_y DOUBLE,
                srs_id INTEGER
            );
            CREATE TABLE gpkg_geometry_columns (
                table_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,
                z TINYINT NOT NULL,
                m TINYINT NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
            CREATE TABLE reviewed (
                id INTEGER PRIMARY KEY,
                geometry MULTICURVE,
                label TEXT NOT NULL
            );
            INSERT INTO gpkg_spatial_ref_sys VALUES (
                'ETRS89 / UTM zone 30N',
                25830,
                'EPSG',
                25830,
                'EPSG:25830',
                ''
            );
            INSERT INTO gpkg_contents VALUES (
                'reviewed',
                'features',
                'reviewed',
                '',
                '2026-07-27T00:00:00.000Z',
                0,
                0,
                40,
                40,
                25830
            );
            INSERT INTO gpkg_geometry_columns VALUES (
                'reviewed',
                'geometry',
                'MULTICURVE',
                25830,
                0,
                0
            );
            """
        )
        for identifier in range(1, 5):
            west = float((identifier - 1) * 10)
            connection.execute(
                "INSERT INTO reviewed (id, geometry, label) "
                "VALUES (?, ?, ?)",
                (
                    identifier,
                    _geometry(
                        west=west,
                        south=west,
                        east=west + 10,
                        north=west + 10,
                    ),
                    f"feature-{identifier}",
                ),
            )
    path = tmp_path / "dataset.zip"
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.write(package, member)
        archive.writestr("README.txt", "official dataset")
        if second_package:
            archive.write(package, "other.gpkg")
    return path


def _parity_config(inspection: dict) -> dict:
    expected = {
        "archive_sha256": inspection["archive_sha256"],
        "archive_size_bytes": inspection["archive_size_bytes"],
        "archive_entry_count": inspection["archive_entry_count"],
        "archive_uncompressed_bytes": inspection[
            "archive_uncompressed_bytes"
        ],
        "archive_member": inspection["archive_member"],
        "archive_member_crc32": inspection["archive_member_crc32"],
        "archive_member_size_bytes": inspection[
            "archive_member_size_bytes"
        ],
        "archive_member_sha256": inspection["archive_member_sha256"],
        "feature_layer": inspection["feature_layer"],
        "feature_layers": inspection["feature_layers"],
        "feature_count": inspection["feature_count"],
        "geometry_type": inspection["geometry_type"],
        "crs": inspection["crs"],
        "declared_bounds": inspection["declared_bounds"],
        "geometry_bounds": inspection["geometry_bounds"],
        "empty_geometry_count": inspection["empty_geometry_count"],
        "data_schema_sha256": inspection["data_schema_sha256"],
        "sample_sha256": inspection["sample"]["sha256"],
        "content_identity_sha256": inspection[
            "content_identity_sha256"
        ],
    }
    semantic = {
        "schema_version": PARITY_SPEC_SCHEMA,
        "expected": expected,
    }
    return {
        "source_content_parity": {
            **semantic,
            "spec_sha256": canonical_json_sha256(semantic),
        }
    }


def _reviewed_archive_config(
    *,
    required_members: list[str],
    license_member: str,
    license_sha256: str,
) -> dict:
    semantic = {
        "schema_version": INTEGRITY_SPEC_SCHEMA,
        "response_constraints": {
            "content_type": "application/x-zip-compressed",
            "max_content_length": 2 * 1024 * 1024,
            "require_etag": True,
            "require_last_modified": True,
        },
        "archive_constraints": {
            "max_entries": 8,
            "max_uncompressed_bytes": 2 * 1024 * 1024,
            "required_members": sorted(required_members),
            "license_member": license_member,
            "license_max_uncompressed_bytes": 64 * 1024,
            "license_sha256_allowlist": [license_sha256],
        },
    }
    return {
        "reviewed_archive_integrity": {
            **semantic,
            "spec_sha256": reviewed_integrity_sha256(semantic),
        }
    }


def _sld() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<sld:StyledLayerDescriptor version="1.0.0"
 xmlns:sld="http://www.opengis.net/sld"
 xmlns:ogc="http://www.opengis.net/ogc">
 <sld:NamedLayer>
  <sld:Name>reviewed</sld:Name>
  <sld:UserStyle>
   <sld:Name>reviewed_style</sld:Name>
   <sld:Title>Reviewed style</sld:Title>
   <sld:FeatureTypeStyle>
    <sld:Rule>
     <sld:LineSymbolizer>
      <sld:Stroke>
       <sld:CssParameter name="stroke">#224466</sld:CssParameter>
      </sld:Stroke>
     </sld:LineSymbolizer>
    </sld:Rule>
   </sld:FeatureTypeStyle>
  </sld:UserStyle>
 </sld:NamedLayer>
</sld:StyledLayerDescriptor>
"""


def _sld_with_identity(
    *,
    layer_name: str,
    style_name: str,
    external_resource: bool = False,
) -> bytes:
    document = _sld().replace(
        b"<sld:Name>reviewed</sld:Name>",
        f"<sld:Name>{layer_name}</sld:Name>".encode(),
        1,
    ).replace(
        b"<sld:Name>reviewed_style</sld:Name>",
        f"<sld:Name>{style_name}</sld:Name>".encode(),
        1,
    )
    if external_resource:
        document = document.replace(
            b'xmlns:ogc="http://www.opengis.net/ogc">',
            (
                b'xmlns:ogc="http://www.opengis.net/ogc" '
                b'xmlns:xlink="http://www.w3.org/1999/xlink">'
            ),
            1,
        ).replace(
            b"<sld:FeatureTypeStyle>",
            (
                b"<sld:OnlineResource "
                b"xlink:href=\"https://styles.example.test/line.png\"/>"
                b"<sld:FeatureTypeStyle>"
            ),
            1,
        )
    return document


def _exact_archive_style(
    path: Path,
    *,
    member: str,
    catalog_style_source_key: str = "reviewed_style",
    remote_name: str = "reviewed_style",
    is_default: bool = True,
    sld_layer_name: str = "reviewed",
    sld_style_name: str = "reviewed_style",
) -> dict:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
        document = archive.read(info)
    return {
        "catalog_style_source_key": catalog_style_source_key,
        "remote_name": remote_name,
        "is_default": is_default,
        "archive_member": member,
        "sha256": hashlib.sha256(document).hexdigest(),
        "size_bytes": info.file_size,
        "crc32": f"{info.CRC:08x}",
        "sld_layer_name": sld_layer_name,
        "sld_style_name": sld_style_name,
    }


class _Download:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/zip",
        last_modified: str | None = None,
    ) -> None:
        self.body = body
        self.content_type = content_type
        self.last_modified = last_modified

    def __call__(self, _policy):
        return self

    def download(
        self,
        url,
        sink,
        *,
        etag=None,
        last_modified=None,
        accept=None,
    ) -> HTTPSDownloadResult:
        del etag, last_modified
        assert accept == self.content_type
        sink.write(self.body)
        return HTTPSDownloadResult(
            source_url=url,
            final_url=url,
            status_code=200,
            not_modified=False,
            content_type=self.content_type,
            size_bytes=len(self.body),
            sha256=hashlib.sha256(self.body).hexdigest(),
            etag='"reviewed-v1"',
            last_modified=self.last_modified,
            redirects=0,
            redirect_chain=(url,),
        )


def _download_candidate(config: dict) -> SourceCandidate:
    draft = SourceCandidate(
        protocol="download",
        target_kind="vector",
        endpoint_url="https://data.example.es/reviewed.zip",
        remote_name="reviewed",
        sync_strategy="conditional_get",
        priority=5,
        config=config,
        source_key="source:reviewed-gpkgzip",
        definition_sha256="0" * 64,
    )
    return replace(
        draft,
        definition_sha256=source_candidate_definition_sha256(draft),
    )


def test_closed_inspection_fingerprints_archive_schema_bounds_and_sample(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )

    assert inspection["archive_sha256"] == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    assert inspection["archive_size_bytes"] == path.stat().st_size
    assert inspection["archive_entry_count"] == 2
    assert inspection["feature_layers"] == ["reviewed"]
    assert inspection["feature_count"] == 4
    assert inspection["geometry_type"] == "MULTICURVE"
    assert inspection["crs"] == "EPSG:25830"
    assert inspection["declared_bounds"] == {
        "west": 0.0,
        "south": 0.0,
        "east": 40.0,
        "north": 40.0,
    }
    assert inspection["geometry_bounds"] == inspection["declared_bounds"]
    assert inspection["empty_geometry_count"] == 0
    assert inspection["data_schema_sha256"]
    assert inspection["sample"]["positions"] == [0, 1, 2, 3]
    assert inspection["sample"]["sha256"]
    assert inspection["content_identity_sha256"]
    assert geopackage_vsi_path(
        path,
        expected_member="dataset/reviewed.gpkg",
    ).endswith("/dataset/reviewed.gpkg")


def test_acquisition_persists_exact_archive_parity_and_sld(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    style = _sld()
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("reviewed.sld", style)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        "archive_styles": [
            {
                "catalog_style_source_key": "reviewed_style",
                "remote_name": "reviewed_style",
                "archive_member": "reviewed.sld",
                "sha256": hashlib.sha256(style).hexdigest(),
            }
        ],
        **_parity_config(inspection),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        result = ReferenceAcquisitionPipeline(
            store,
            limits=AcquisitionLimits(
                max_probe_bytes=256 * 1024,
                max_page_bytes=512 * 1024,
                max_dataset_bytes=2 * 1024 * 1024,
                max_total_bytes=4 * 1024 * 1024,
                page_size=100,
                max_pages=2,
                max_features=100,
                timeout_seconds=10,
                idle_timeout_seconds=2,
            ),
            downloader_factory=_Download(path.read_bytes()),
        ).acquire(_download_candidate(config))
    finally:
        store.close()

    dataset = next(item for item in result.artifacts if item.role == "input")
    acquired_style = next(
        item for item in result.artifacts if item.role == "style"
    )
    assert dataset.metadata["archive_member"] == "dataset/reviewed.gpkg"
    assert dataset.metadata["geopackage_inspection"] == inspection
    assert dataset.metadata["source_content_parity"]["passed"] is True
    assert acquired_style.metadata["archive_member_sha256"] == (
        hashlib.sha256(style).hexdigest()
    )
    assert acquired_style.metadata["parent_sha256"] == (
        inspection["archive_sha256"]
    )
    assert acquired_style.metadata["parity_kind"] == "exact"


def test_acquisition_binds_enriched_archive_style_member_and_sld_identity(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    member = "styles/official.sld"
    style = _sld_with_identity(
        layer_name="archive_layer",
        style_name="official_style",
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(member, style)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    archive_style = _exact_archive_style(
        path,
        member=member,
        sld_layer_name="archive_layer",
        sld_style_name="official_style",
    )
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        "archive_styles": [archive_style],
        **_parity_config(inspection),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        result = ReferenceAcquisitionPipeline(
            store,
            limits=AcquisitionLimits(
                max_probe_bytes=256 * 1024,
                max_page_bytes=512 * 1024,
                max_dataset_bytes=2 * 1024 * 1024,
                max_total_bytes=4 * 1024 * 1024,
                page_size=100,
                max_pages=2,
                max_features=100,
                timeout_seconds=10,
                idle_timeout_seconds=2,
            ),
            downloader_factory=_Download(path.read_bytes()),
        ).acquire(_download_candidate(config))
    finally:
        store.close()

    acquired_style = next(
        item for item in result.artifacts if item.role == "style"
    )
    assert acquired_style.metadata["catalog_style_source_key"] == (
        "reviewed_style"
    )
    assert acquired_style.metadata["remote_name"] == "reviewed_style"
    assert acquired_style.metadata["is_default"] is True
    assert acquired_style.metadata["archive_member"] == member
    assert acquired_style.metadata["archive_member_sha256"] == (
        archive_style["sha256"]
    )
    assert acquired_style.metadata["archive_member_size_bytes"] == len(style)
    assert acquired_style.metadata["archive_member_crc32"] == (
        archive_style["crc32"]
    )
    assert acquired_style.metadata["sld_named_layer"] == "archive_layer"
    assert acquired_style.metadata["sld_user_style"] == "official_style"
    assert acquired_style.metadata["parity_kind"] == "exact"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size_bytes", 1),
        ("crc32", "deadbeef"),
    ],
)
def test_acquisition_rejects_changed_archive_style_size_or_crc(
    tmp_path,
    field,
    value,
) -> None:
    path = _archive(tmp_path)
    member = "reviewed.sld"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(member, _sld())
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    archive_style = _exact_archive_style(path, member=member)
    archive_style[field] = value
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        "archive_styles": [archive_style],
        **_parity_config(inspection),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        with pytest.raises(
            AcquisitionValidationError,
            match="style member is invalid",
        ):
            ReferenceAcquisitionPipeline(
                store,
                limits=AcquisitionLimits(
                    max_probe_bytes=256 * 1024,
                    max_page_bytes=512 * 1024,
                    max_dataset_bytes=2 * 1024 * 1024,
                    max_total_bytes=4 * 1024 * 1024,
                    page_size=100,
                    max_pages=2,
                    max_features=100,
                    timeout_seconds=10,
                    idle_timeout_seconds=2,
                ),
                downloader_factory=_Download(path.read_bytes()),
            ).acquire(_download_candidate(config))
    finally:
        store.close()


def test_archive_style_configuration_rejects_traversal_before_download(
    tmp_path,
) -> None:
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_styles": [
            {
                "catalog_style_source_key": "reviewed_style",
                "remote_name": "reviewed_style",
                "is_default": True,
                "archive_member": "styles/../reviewed.sld",
                "sha256": "a" * 64,
                "size_bytes": 100,
                "crc32": "0123abcd",
                "sld_layer_name": "reviewed",
                "sld_style_name": "reviewed_style",
            }
        ],
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        with pytest.raises(
            AcquisitionConfigurationError,
            match="style identity is invalid",
        ):
            ReferenceAcquisitionPipeline(
                store,
                downloader_factory=lambda _policy: pytest.fail(
                    "invalid style config reached the network"
                ),
            ).acquire(_download_candidate(config))
    finally:
        store.close()


def test_acquisition_rejects_archive_style_with_external_resource(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    member = "reviewed.sld"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            member,
            _sld_with_identity(
                layer_name="reviewed",
                style_name="reviewed_style",
                external_resource=True,
            ),
        )
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        "archive_styles": [
            _exact_archive_style(path, member=member)
        ],
        **_parity_config(inspection),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        with pytest.raises(
            AcquisitionValidationError,
            match="not self-contained",
        ):
            ReferenceAcquisitionPipeline(
                store,
                limits=AcquisitionLimits(
                    max_probe_bytes=256 * 1024,
                    max_page_bytes=512 * 1024,
                    max_dataset_bytes=2 * 1024 * 1024,
                    max_total_bytes=4 * 1024 * 1024,
                    page_size=100,
                    max_pages=2,
                    max_features=100,
                    timeout_seconds=10,
                    idle_timeout_seconds=2,
                ),
                downloader_factory=_Download(path.read_bytes()),
            ).acquire(_download_candidate(config))
    finally:
        store.close()


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("sld_layer_name", "invented_layer", "exact requested layer"),
        ("sld_style_name", "invented_style", "exact requested style"),
    ],
)
def test_acquisition_rejects_invented_sld_name_mapping(
    tmp_path,
    field,
    value,
    match,
) -> None:
    path = _archive(tmp_path)
    member = "reviewed.sld"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(member, _sld())
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    archive_style = _exact_archive_style(path, member=member)
    archive_style[field] = value
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        "archive_styles": [archive_style],
        **_parity_config(inspection),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        with pytest.raises(AcquisitionValidationError, match=match):
            ReferenceAcquisitionPipeline(
                store,
                limits=AcquisitionLimits(
                    max_probe_bytes=256 * 1024,
                    max_page_bytes=512 * 1024,
                    max_dataset_bytes=2 * 1024 * 1024,
                    max_total_bytes=4 * 1024 * 1024,
                    page_size=100,
                    max_pages=2,
                    max_features=100,
                    timeout_seconds=10,
                    idle_timeout_seconds=2,
                ),
                downloader_factory=_Download(path.read_bytes()),
            ).acquire(_download_candidate(config))
    finally:
        store.close()


def test_shapefile_zip_can_use_exact_embedded_sld(tmp_path) -> None:
    path = tmp_path / "shapefile.zip"
    style = _sld_with_identity(
        layer_name="reviewed",
        style_name="reviewed_style",
    )
    with zipfile.ZipFile(
        path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("reviewed.shp", b"shp")
        archive.writestr("reviewed.shx", b"shx")
        archive.writestr("reviewed.dbf", b"dbf")
        archive.writestr("reviewed.sld", style)
    config = {
        "media_type": "application/zip",
        "data_format": "shapefile-zip",
        "archive_styles": [
            _exact_archive_style(path, member="reviewed.sld")
        ],
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        result = ReferenceAcquisitionPipeline(
            store,
            limits=AcquisitionLimits(
                max_probe_bytes=256 * 1024,
                max_page_bytes=512 * 1024,
                max_dataset_bytes=2 * 1024 * 1024,
                max_total_bytes=4 * 1024 * 1024,
                page_size=100,
                max_pages=2,
                max_features=100,
                timeout_seconds=10,
                idle_timeout_seconds=2,
            ),
            downloader_factory=_Download(path.read_bytes()),
        ).acquire(_download_candidate(config))
    finally:
        store.close()

    acquired_style = next(
        item for item in result.artifacts if item.role == "style"
    )
    assert acquired_style.metadata["archive_member"] == "reviewed.sld"
    assert acquired_style.metadata["archive_member_size_bytes"] == len(style)


def test_acquisition_accepts_reviewed_live_geopackage_without_pinning_data_hash(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    license_member = "Licencia-IGCYL.txt"
    license_body = b"Audited IGCYL license text"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(license_member, license_body)
    config = {
        "media_type": "application/x-zip-compressed",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        **_reviewed_archive_config(
            required_members=[
                license_member,
                "dataset/reviewed.gpkg",
            ],
            license_member=license_member,
            license_sha256=hashlib.sha256(license_body).hexdigest(),
        ),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        result = ReferenceAcquisitionPipeline(
            store,
            limits=AcquisitionLimits(
                max_probe_bytes=256 * 1024,
                max_page_bytes=512 * 1024,
                max_dataset_bytes=2 * 1024 * 1024,
                max_total_bytes=4 * 1024 * 1024,
                page_size=100,
                max_pages=2,
                max_features=100,
                timeout_seconds=10,
                idle_timeout_seconds=2,
            ),
            downloader_factory=_Download(
                path.read_bytes(),
                content_type="application/x-zip-compressed",
                last_modified="Sun, 27 Jul 2026 12:00:00 GMT",
            ),
        ).acquire(_download_candidate(config))
    finally:
        store.close()

    dataset = next(item for item in result.artifacts if item.role == "input")
    gate = dataset.metadata["reviewed_archive_integrity"]
    assert gate["passed"] is True
    assert gate["response"]["etag"] == '"reviewed-v1"'
    assert gate["license_sha256"] == hashlib.sha256(
        license_body
    ).hexdigest()
    assert dataset.metadata["geopackage_inspection"]["feature_count"] == 4
    assert "source_content_parity" not in dataset.metadata


def test_acquisition_rejects_changed_style_even_with_new_data_parity(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    changed_style = _sld().replace(b"#224466", b"#ffffff")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("reviewed.sld", changed_style)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = {
        "media_type": "application/zip",
        "data_format": "geopackage-zip",
        "archive_member": "dataset/reviewed.gpkg",
        "input_layer": "reviewed",
        "archive_max_uncompressed_bytes": 2 * 1024 * 1024,
        "archive_styles": [
            {
                "catalog_style_source_key": "reviewed_style",
                "remote_name": "reviewed_style",
                "archive_member": "reviewed.sld",
                "sha256": hashlib.sha256(_sld()).hexdigest(),
            }
        ],
        **_parity_config(inspection),
    }
    store = ReferenceBlobStore(tmp_path / "blob-store")
    try:
        with pytest.raises(
            AcquisitionValidationError,
            match="style digest changed",
        ):
            ReferenceAcquisitionPipeline(
                store,
                limits=AcquisitionLimits(
                    max_probe_bytes=256 * 1024,
                    max_page_bytes=512 * 1024,
                    max_dataset_bytes=2 * 1024 * 1024,
                    max_total_bytes=4 * 1024 * 1024,
                    page_size=100,
                    max_pages=2,
                    max_features=100,
                    timeout_seconds=10,
                    idle_timeout_seconds=2,
                ),
                downloader_factory=_Download(path.read_bytes()),
            ).acquire(_download_candidate(config))
    finally:
        store.close()


@pytest.mark.parametrize(
    ("expected_member", "expected_layer", "second_package", "match"),
    [
        ("wrong.gpkg", "reviewed", False, "unique reviewed member"),
        (
            "dataset/reviewed.gpkg",
            "missing",
            False,
            "reviewed feature layer",
        ),
        (
            "dataset/reviewed.gpkg",
            "reviewed",
            True,
            "unique reviewed member",
        ),
    ],
)
def test_inspection_rejects_ambiguous_or_wrong_identity(
    tmp_path,
    expected_member,
    expected_layer,
    second_package,
    match,
) -> None:
    path = _archive(tmp_path, second_package=second_package)

    with pytest.raises(GeoPackageArchiveError, match=match):
        inspect_geopackage_zip(
            path,
            expected_member=expected_member,
            expected_layer=expected_layer,
            maximum_uncompressed_bytes=2 * 1024 * 1024,
        )


def test_spoofed_sqlite_file_is_not_a_geopackage(tmp_path) -> None:
    path = tmp_path / "spoofed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "reviewed.gpkg",
            b"SQLite format 3\x00" + b"\x00" * 256,
        )

    with pytest.raises(GeoPackageArchiveError):
        inspect_geopackage_zip(
            path,
            expected_member="reviewed.gpkg",
            expected_layer="reviewed",
            maximum_uncompressed_bytes=1024 * 1024,
        )


@pytest.mark.parametrize(
    "unsafe",
    ["traversal", "encrypted", "symlink", "bomb", "case_duplicate"],
)
def test_zip_entry_policy_rejects_unsafe_entries(unsafe) -> None:
    package = zipfile.ZipInfo("reviewed.gpkg")
    package.compress_type = zipfile.ZIP_STORED
    package.file_size = 100
    package.compress_size = 100
    package.external_attr = (stat.S_IFREG | 0o600) << 16
    entries = [package]

    if unsafe == "traversal":
        extra = zipfile.ZipInfo("../escape.txt")
    elif unsafe == "encrypted":
        extra = zipfile.ZipInfo("secret.txt")
        extra.flag_bits = 0x1
    elif unsafe == "symlink":
        extra = zipfile.ZipInfo("link.txt")
        extra.external_attr = (stat.S_IFLNK | 0o777) << 16
    elif unsafe == "bomb":
        extra = zipfile.ZipInfo("bomb.txt")
        extra.file_size = 100_001
        extra.compress_size = 1
    else:
        extra = zipfile.ZipInfo("REVIEWED.GPKG")
    extra.compress_type = zipfile.ZIP_STORED
    if unsafe not in {"bomb"}:
        extra.file_size = max(extra.file_size, 1)
        extra.compress_size = max(extra.compress_size, 1)
    entries.append(extra)

    expected_match = (
        "unsafe compression ratio"
        if unsafe == "bomb"
        else "unsafe entry"
    )
    with pytest.raises(GeoPackageArchiveError, match=expected_match):
        _validated_entries(
            entries,
            expected_member="reviewed.gpkg",
            maximum_uncompressed_bytes=2 * 1024 * 1024,
        )


def test_acquisition_and_promotion_gates_bind_the_same_input(tmp_path) -> None:
    path = _archive(tmp_path)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = _parity_config(inspection)
    acquisition = evaluate_acquisition_parity(config, inspection)

    assert acquisition is not None
    assert acquisition["passed"] is True
    gate = build_promotion_parity_gate(
        config=config,
        source_definition_sha256="a" * 64,
        input_artifact_id=17,
        input_artifact_sha256=inspection["archive_sha256"],
        input_artifact_metadata={"source_content_parity": acquisition},
        delivery_kind="vector",
        feature_count=4,
    )
    assert gate is not None
    assert gate["passed"] is True
    assert valid_promotion_parity_gate(
        config=config,
        gate=gate,
        source_definition_sha256="a" * 64,
        input_artifact_id=17,
        input_artifact_sha256=inspection["archive_sha256"],
        input_artifact_metadata={"source_content_parity": acquisition},
        delivery_kind="vector",
        feature_count=4,
    )
    assert not valid_promotion_parity_gate(
        config=config,
        gate=gate,
        source_definition_sha256="a" * 64,
        input_artifact_id=17,
        input_artifact_sha256="b" * 64,
        input_artifact_metadata={"source_content_parity": acquisition},
        delivery_kind="vector",
        feature_count=4,
    )


def test_acquisition_gate_rejects_forged_matching_check_values(tmp_path) -> None:
    path = _archive(tmp_path)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = _parity_config(inspection)
    acquisition = evaluate_acquisition_parity(config, inspection)
    assert acquisition is not None
    forged = deepcopy(acquisition)
    forged["checks"]["feature_count"]["expected"] = 99
    forged["checks"]["feature_count"]["observed"] = 99
    observed = {
        key: check["observed"]
        for key, check in forged["checks"].items()
    }
    forged["observation_sha256"] = canonical_json_sha256(observed)

    assert valid_acquisition_gate(config, acquisition)
    assert not valid_acquisition_gate(config, forged)


@pytest.mark.parametrize(
    "field",
    [
        "archive_sha256",
        "feature_count",
        "geometry_type",
        "crs",
        "declared_bounds",
        "geometry_bounds",
        "data_schema_sha256",
        "sample",
        "content_identity_sha256",
    ],
)
def test_acquisition_parity_reports_every_changed_identity_field(
    tmp_path,
    field,
) -> None:
    path = _archive(tmp_path)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = _parity_config(inspection)
    changed = deepcopy(inspection)
    if field == "sample":
        changed["sample"]["sha256"] = "f" * 64
        expected_failed = "sample_sha256"
    elif field in {"declared_bounds", "geometry_bounds"}:
        changed[field]["east"] += 1
        expected_failed = field
    elif field == "feature_count":
        changed[field] += 1
        expected_failed = field
    elif field in {"geometry_type", "crs"}:
        changed[field] += "_changed"
        expected_failed = field
    else:
        changed[field] = "f" * 64
        expected_failed = field

    gate = evaluate_acquisition_parity(config, changed)

    assert gate is not None
    assert gate["passed"] is False
    assert expected_failed in gate["failed_checks"]


def test_promotion_rejects_feature_count_or_missing_acquisition_gate(
    tmp_path,
) -> None:
    path = _archive(tmp_path)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = _parity_config(inspection)
    acquisition = evaluate_acquisition_parity(config, inspection)
    assert acquisition is not None

    with pytest.raises(SourceContentParityError):
        build_promotion_parity_gate(
            config=config,
            source_definition_sha256="a" * 64,
            input_artifact_id=1,
            input_artifact_sha256=inspection["archive_sha256"],
            input_artifact_metadata={"source_content_parity": acquisition},
            delivery_kind="vector",
            feature_count=5,
        )
    with pytest.raises(SourceContentParityError):
        build_promotion_parity_gate(
            config=config,
            source_definition_sha256="a" * 64,
            input_artifact_id=1,
            input_artifact_sha256=inspection["archive_sha256"],
            input_artifact_metadata={},
            delivery_kind="vector",
            feature_count=4,
        )


def test_parity_spec_hash_rejects_semantic_tampering(tmp_path) -> None:
    path = _archive(tmp_path)
    inspection = inspect_geopackage_zip(
        path,
        expected_member="dataset/reviewed.gpkg",
        expected_layer="reviewed",
        maximum_uncompressed_bytes=2 * 1024 * 1024,
    )
    config = _parity_config(inspection)
    config["source_content_parity"]["expected"]["feature_count"] += 1

    with pytest.raises(SourceContentParityError, match="hash is invalid"):
        evaluate_acquisition_parity(config, inspection)
