from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.resources import files
import io
import json
from pathlib import Path
import sqlite3
import struct
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
import zipfile

import pytest
from sqlalchemy import func, select

import app.reference_layers.acquisition as acquisition_module
from app.reference_layers.acquisition import (
    AcquiredArtifact,
    AcquisitionResult,
    AcquisitionConfigurationError,
    AcquisitionLimitError,
    AcquisitionLimits,
    AcquisitionPersistenceError,
    AcquisitionValidationError,
    ConditionalRequest,
    ReferenceAcquisitionPipeline,
    candidate_from_source_model,
    persist_acquisition_result,
    source_candidate_definition_sha256,
)
from app.reference_layers.blob_store import ReferenceBlobStore, StoredReferenceBlob
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.masked_geopackage import (
    MaskIdentity,
    MaskedGeoPackageResult,
)
from app.reference_layers.safe_download import HTTPSDownloadResult
from app.reference_layers.source_discovery import (
    SourceCandidate,
    acquisition_candidates,
)
from app.reference_layers.reviewed_ortho_evidence import (
    CAPABILITIES_RESOURCE as IGN_CAPABILITIES_RESOURCE,
    CATALOG_CAPABILITIES_RESOURCE as ITACYL_CAPABILITIES_RESOURCE,
    CATALOG_ENDPOINT_URL,
    reviewed_ign_ortho_expected_source_definition,
    reviewed_ign_ortho_substitution,
)


@dataclass(frozen=True)
class Response:
    body: bytes = b""
    content_type: str = "application/json"
    status: int = 200
    etag: str | None = None
    last_modified: str | None = None


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.policies = []
        self.calls = []

    def __call__(self, policy):
        self.policies.append(policy)
        return self

    def download(
        self,
        url,
        sink,
        *,
        etag=None,
        last_modified=None,
        accept=None,
    ):
        self.calls.append(
            {
                "url": url,
                "etag": etag,
                "last_modified": last_modified,
                "accept": accept,
            }
        )
        response = self.handler(url, etag, last_modified)
        if response.status == 304:
            return HTTPSDownloadResult(
                source_url=url,
                final_url=url,
                status_code=304,
                not_modified=True,
                content_type=None,
                size_bytes=0,
                sha256=None,
                etag=response.etag or etag,
                last_modified=response.last_modified or last_modified,
                redirects=0,
                redirect_chain=(url,),
            )
        assert response.status == 200
        for offset in range(0, len(response.body), 7):
            sink.write(response.body[offset : offset + 7])
        return HTTPSDownloadResult(
            source_url=url,
            final_url=url,
            status_code=200,
            not_modified=False,
            content_type=response.content_type,
            size_bytes=len(response.body),
            sha256=hashlib.sha256(response.body).hexdigest(),
            etag=response.etag,
            last_modified=response.last_modified,
            redirects=0,
            redirect_chain=(url,),
        )


@pytest.fixture()
def limits():
    return AcquisitionLimits(
        max_probe_bytes=32 * 1024,
        max_page_bytes=128 * 1024,
        max_dataset_bytes=1024 * 1024,
        max_total_bytes=2 * 1024 * 1024,
        page_size=2,
        max_pages=6,
        max_features=20,
        timeout_seconds=10,
        idle_timeout_seconds=2,
    )


@pytest.fixture()
def store(tmp_path):
    value = ReferenceBlobStore(tmp_path / "reference-acquisition")
    try:
        yield value
    finally:
        value.close()


def candidate(
    protocol: str,
    *,
    remote_name: str = "roads",
    endpoint: str = "https://data.example.es/service",
    config: dict | None = None,
) -> SourceCandidate:
    target = (
        "vector"
        if protocol in {"wfs", "ogc_api_features", "arcgis_rest", "download", "atom"}
        else "raster"
        if protocol == "wcs"
        else "tiles"
    )
    strategy = (
        "paged_snapshot"
        if protocol in {"wfs", "ogc_api_features", "arcgis_rest"}
        else "tile_seed"
        if target == "tiles"
        else "conditional_get"
        if protocol in {"download", "atom"}
        else "full_snapshot"
    )
    draft = SourceCandidate(
        protocol=protocol,
        target_kind=target,
        endpoint_url=endpoint,
        remote_name=remote_name,
        sync_strategy=strategy,
        priority=10,
        config=config or {},
        source_key=f"source:{protocol}",
        definition_sha256="0" * 64,
    )
    return replace(
        draft,
        definition_sha256=source_candidate_definition_sha256(draft),
    )


def reviewed_catalog_candidate(
    *,
    endpoint: str,
    layer_name: str,
    style_key: str,
    style_name: str,
) -> SourceCandidate:
    service = ReferenceServiceDefinition(
        source_key="service",
        title="Reviewed service",
        upstream_protocol="wms",
        base_url=endpoint,
        default_format="image/png",
    )
    layer = ReferenceLayerDefinition(
        source_key="layer:siur:" + "a" * 64,
        node_type="layer",
        title="Reviewed layer",
        service_key="service",
        remote_name=layer_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name=style_key,
        styles=(
            ReferenceLayerStyleDefinition(
                source_key=style_key,
                title=style_name,
                remote_name=style_name,
                is_default=True,
            ),
        ),
    )
    return acquisition_candidates(service, layer)[0]


def reviewed_eurostat_grid_candidate(
    resolution: str = "100",
) -> SourceCandidate:
    service = ReferenceServiceDefinition(
        source_key="service:idecyl-rejillas",
        title="IDECyL Eurostat grids",
        upstream_protocol="wms",
        base_url="https://idecyl.jcyl.es/geoserver/rejillas/wms",
        default_format="image/png",
    )
    styles = (
        ReferenceLayerStyleDefinition(
            source_key="rejilla_eurostat_cyl_morado",
            title="Borde celdas morado",
            remote_name="rejilla_eurostat_cyl_morado",
            is_default=True,
        ),
        ReferenceLayerStyleDefinition(
            source_key="rejilla_eurostat_cyl_blanco",
            title="Borde celdas blanco",
            remote_name="rejilla_eurostat_cyl_blanco",
        ),
        ReferenceLayerStyleDefinition(
            source_key="rejilla_eurostat_cyl_fucsia",
            title="Borde celdas fucsia",
            remote_name="rejilla_eurostat_cyl_fucsia",
        ),
    )
    layer = ReferenceLayerDefinition(
        source_key="layer:siur:" + "8" * 64,
        node_type="layer",
        title=f"Rejilla Eurostat {resolution} km",
        service_key=service.source_key,
        remote_name=(
            f"rejilla_eurostat_cyl_{resolution}x{resolution}"
        ),
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name="rejilla_eurostat_cyl_morado",
        styles=styles,
    )
    selected = acquisition_candidates(service, layer)
    assert len(selected) == 1
    return selected[0]


def json_response(value, **kwargs) -> Response:
    return Response(
        body=json.dumps(value, separators=(",", ":")).encode(),
        content_type="application/json",
        **kwargs,
    )


def shapefile_zip_payload() -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("roads.shp", b"shp payload")
        archive.writestr("roads.shx", b"shx payload")
        archive.writestr("roads.dbf", b"dbf payload")
    return target.getvalue()


def minimal_geopackage_payload(path: Path) -> bytes:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE gpkg_spatial_ref_sys "
            "(srs_name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE gpkg_contents "
            "(table_name TEXT NOT NULL)"
        )
    return path.read_bytes()


def masked_geopackage_config(
    mask_url: str,
    *,
    mask_identity_sha256: str = "a" * 64,
    identifier_sha256: str = "b" * 64,
) -> dict:
    return {
        "media_type": "application/geopackage+sqlite3",
        "data_format": "geopackage",
        "source_retention": "discard_after_derivation",
        "vector_transform": {
            "schema": "reference-masked-geopackage/v1",
            "mask_url": mask_url,
            "mask_media_type": "application/json",
            "mask_max_bytes": 1024 * 1024,
            "mask_identity_sha256": mask_identity_sha256,
            "source_layer": "grid_100km_surf",
            "output_layer": "grid_100km_surf_cyl",
            "selected_fields": ["GRD_ID", "X_LLC", "Y_LLC"],
            "identifier_field": "GRD_ID",
            "cell_size_meters": 100_000,
            "expected_feature_count": 19,
            "expected_identifier_sha256": identifier_sha256,
            "source_crs": "EPSG:3035",
            "mask_target_crs": "EPSG:3035",
            "predicate": "intersects",
            "geometry_mode": "preserve-whole-source-features",
        },
    }


def cadastral_gml_zip_payload(code: str = "05001") -> bytes:
    gml = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection
 xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:gml="http://www.opengis.net/gml/3.2"
 xmlns:cp="http://inspire.ec.europa.eu/schemas/cp/4.0">
 <wfs:member>
  <cp:CadastralParcel gml:id="ES.SDGC.CP.{code}.1">
   <cp:localId>{code}-1</cp:localId>
  </cp:CadastralParcel>
 </wfs:member>
</wfs:FeatureCollection>""".encode()
    target = io.BytesIO()
    with zipfile.ZipFile(
        target,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(
            f"A.ES.SDGC.CP.{code}.cadastralparcel.gml",
            gml,
        )
        archive.writestr(f"A.ES.SDGC.CP.{code}.metadata.xml", b"<metadata/>")
    return target.getvalue()


def raster_vat_dbf(
    class_field: str,
    value_class_mapping: list[tuple[int, int]],
) -> bytes:
    fields = (
        ("Value", "N", 10, 0),
        ("Count", "F", 19, 11),
        (class_field, "N", 10, 0),
        ("LimProvPen", "N", 10, 0),
        ("NUTS2", "F", 19, 11),
    )
    header_length = 32 + len(fields) * 32 + 1
    record_length = 1 + sum(item[2] for item in fields)
    header = bytearray(32)
    header[0] = 0x03
    struct.pack_into("<I", header, 4, len(value_class_mapping))
    struct.pack_into("<H", header, 8, header_length)
    struct.pack_into("<H", header, 10, record_length)
    descriptors = bytearray()
    for name, field_type, width, decimal_count in fields:
        descriptor = bytearray(32)
        encoded_name = name.encode("ascii")
        descriptor[: len(encoded_name)] = encoded_name
        descriptor[11] = ord(field_type)
        descriptor[16] = width
        descriptor[17] = decimal_count
        descriptors.extend(descriptor)
    records = bytearray()
    for value, class_value in value_class_mapping:
        row = (
            b" "
            + f"{value:>10d}".encode()
            + f"{1.0:>19.11e}".encode()
            + f"{class_value:>10d}".encode()
            + f"{24:>10d}".encode()
            + f"{41.0:>19.11e}".encode()
        )
        assert len(row) == record_length
        records.extend(row)
    return bytes(header + descriptors + b"\r" + records + b"\x1a")


def geotiff_zip_payload(
    member: str = "erosion.tiff",
    *,
    vat_class_field: str | None = None,
    value_class_mapping: list[tuple[int, int]] | None = None,
) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(
        target,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(member, b"II+\x00" + b"\x00" * 124)
        archive.writestr(member + ".aux.xml", b"<PAMDataset/>")
        if vat_class_field is not None:
            archive.writestr(
                member + ".vat.dbf",
                raster_vat_dbf(
                    vat_class_field,
                    value_class_mapping
                    or [(63 + value, value) for value in range(1, 10)],
                ),
            )
    return target.getvalue()


def atom_feed_payload(*hrefs: str) -> bytes:
    entries = "".join(
        f'<entry><id>{index}</id><link rel="enclosure" href="{href}" /></entry>'
        for index, href in enumerate(hrefs)
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"{entries}</feed>"
    ).encode()


def geotiff_payload() -> bytes:
    entries = [
        struct.pack("<HHII", 256, 4, 1, 256),
        struct.pack("<HHII", 257, 4, 1, 128),
        struct.pack("<HHII", 33550, 12, 3, 74),
        struct.pack("<HHII", 33922, 12, 6, 98),
        struct.pack("<HHII", 34735, 3, 4, 146),
    ]
    directory = struct.pack("<H", len(entries)) + b"".join(entries) + struct.pack("<I", 0)
    values = struct.pack("<3d", 1.0, 1.0, 0.0)
    values += struct.pack("<6d", 0.0, 0.0, 0.0, -7.0, 43.0, 0.0)
    values += struct.pack("<4H", 1, 1, 0, 0)
    return b"II" + struct.pack("<HI", 42, 8) + directory + values


def read_json_artifact(store, result, kind: str, *, metadata_kind: str | None = None):
    selected = [
        item
        for item in result.artifacts
        if item.artifact_kind == kind
        and (metadata_kind is None or item.metadata.get("schema") == metadata_kind)
    ]
    assert len(selected) == 1
    with store.open_blob(selected[0].blob.storage_key) as source:
        return json.load(source)


WFS_CAPABILITIES = b"""<wfs:WFS_Capabilities version="2.0.0"
 xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:ows="http://www.opengis.net/ows/1.1">
 <ows:OperationsMetadata>
  <ows:Constraint name="PagingIsTransactionSafe">
   <ows:DefaultValue>true</ows:DefaultValue>
  </ows:Constraint>
 </ows:OperationsMetadata>
 <wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>workspace:roads</wfs:Name>
 </wfs:FeatureType></wfs:FeatureTypeList></wfs:WFS_Capabilities>"""


def wfs_1x_capabilities(version: str) -> bytes:
    return (
        f'<wfs:WFS_Capabilities version="{version}" '
        'xmlns:wfs="http://www.opengis.net/wfs">'
        "<wfs:FeatureTypeList><wfs:FeatureType>"
        "<wfs:Name>workspace:roads</wfs:Name>"
        "</wfs:FeatureType></wfs:FeatureTypeList>"
        "</wfs:WFS_Capabilities>"
    ).encode()


def wfs_hits_response(count: int) -> Response:
    return Response(
        (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<wfs:FeatureCollection '
            'xmlns:wfs="http://www.opengis.net/wfs/2.0" '
            f'numberMatched="{count}" numberReturned="0"/>'
        ).encode(),
        "application/xml",
    )


def wfs_1x_hits_response(
    count: int,
    *,
    member: bool = False,
) -> Response:
    rendered_member = (
        '<gml:featureMember xmlns:gml="http://www.opengis.net/gml"/>'
        if member
        else ""
    )
    return Response(
        (
            '<wfs:FeatureCollection '
            'xmlns:wfs="http://www.opengis.net/wfs" '
            f'numberOfFeatures="{count}">'
            f"{rendered_member}</wfs:FeatureCollection>"
        ).encode(),
        "application/xml",
    )


def style_config(
    *styles: tuple[str, str],
    endpoint: str = "https://data.example.es/wms",
    layer_name: str = "workspace:roads",
):
    return {
        "style_endpoint_url": endpoint,
        "style_layer_name": layer_name,
        "styles": [
            {
                "catalog_style_source_key": source_key,
                "remote_name": remote_name,
            }
            for source_key, remote_name in sorted(styles)
        ],
    }


def sld_payload(
    *style_names: str,
    layer_name: str = "workspace:roads",
    external_href: str | None = None,
) -> bytes:
    rendered_styles = []
    for name in style_names:
        graphic = (
            "<PointSymbolizer><Graphic><ExternalGraphic>"
            f'<OnlineResource xlink:href="{external_href}" />'
            "<Format>image/png</Format></ExternalGraphic></Graphic>"
            "</PointSymbolizer>"
            if external_href is not None
            else (
                "<PolygonSymbolizer><Fill>"
                '<CssParameter name="fill">#336699</CssParameter>'
                "</Fill></PolygonSymbolizer>"
            )
        )
        rendered_styles.append(
            f"<UserStyle><Name>{name}</Name><FeatureTypeStyle><Rule>"
            f"{graphic}</Rule></FeatureTypeStyle></UserStyle>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<StyledLayerDescriptor version="1.0.0" '
        'xmlns="http://www.opengis.net/sld" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.opengis.net/sld '
        'StyledLayerDescriptor.xsd">'
        f"<NamedLayer><Name>{layer_name}</Name>{''.join(rendered_styles)}"
        "</NamedLayer></StyledLayerDescriptor>"
    ).encode()


def test_wfs_downloads_bounded_pages_and_builds_ingestion_manifest(store, limits):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        assert query["typeNames"] == ["workspace:roads"]
        assert query["count"] == ["2"]
        offset = int(query.get("startIndex", ["0"])[0])
        features = [
            {
                "type": "Feature",
                "id": f"roads.{value}",
                "properties": {"n": value},
                "geometry": None,
            }
            for value in ([1, 2] if offset == 0 else [3])
        ]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "numberReturned": len(features),
                "features": features,
            }
        )

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=transport
    ).acquire(candidate("wfs"))

    assert result.feature_count == 3
    assert result.stats == {
        "page_count": 2,
        "feature_count": 3,
        "page_size": 2,
        "number_matched": 3,
        "feature_ids_observed": 3,
    }
    assert [
        item.metadata.get("feature_count")
        for item in result.artifacts
        if item.artifact_kind == "dataset"
    ] == [2, 1]
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["kind"] == "feature-pages"
    assert len(manifest["materialization"]["page_artifact_sha256"]) == 2
    assert all(
        policy.allowed_origins == ("https://data.example.es",)
        for policy in transport.policies
    )
    assert list((store.root / "staging").iterdir()) == []


def test_legacy_wfs_never_downloads_without_transaction_safety_or_contract(
    store,
    limits,
):
    feature_calls = 0
    unsafe_capabilities = WFS_CAPABILITIES.replace(
        b"<ows:DefaultValue>true</ows:DefaultValue>",
        b"<ows:DefaultValue>false</ows:DefaultValue>",
    )

    def handler(url, _etag, _modified):
        nonlocal feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(unsafe_capabilities, "application/xml")
        feature_calls += 1
        return json_response(
            {
                "type": "FeatureCollection",
                # Even an apparently complete first response is not enough:
                # capped services can make this count look complete.
                "numberMatched": 2,
                "features": [
                    {
                        "type": "Feature",
                        "id": f"road-{value}",
                        "properties": {},
                        "geometry": None,
                    }
                    for value in (1, 2)
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(candidate("wfs"))

    assert captured.value.code == "wfs_paging_not_transaction_safe"
    assert feature_calls == 0
    assert list((store.root / "staging").iterdir()) == []


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
def test_wfs_1x_hits_omit_max_features_and_accept_number_of_features(
    store,
    limits,
    version,
):
    hits_calls = 0
    feature_calls = 0

    def handler(url, _etag, _modified):
        nonlocal hits_calls, feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(
                wfs_1x_capabilities(version),
                "application/xml",
            )
        assert query["typeName"] == ["workspace:roads"]
        if query.get("resultType") == ["hits"]:
            hits_calls += 1
            assert "maxFeatures" not in query
            return wfs_1x_hits_response(1)
        feature_calls += 1
        assert "maxFeatures" not in query
        assert "startIndex" not in query
        return json_response(
            {
                "type": "FeatureCollection",
                "totalFeatures": 1,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"fid": 1},
                        "geometry": None,
                    }
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            config={"wfs_snapshot": {"mode": "single_response"}},
        )
    )

    assert result.feature_count == 1
    assert hits_calls == 2
    assert feature_calls == 2


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0"])
def test_wfs_1x_hits_reject_non_empty_feature_collection(
    store,
    limits,
    version,
):
    feature_calls = 0

    def handler(url, _etag, _modified):
        nonlocal feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(
                wfs_1x_capabilities(version),
                "application/xml",
            )
        feature_calls += 1
        return wfs_1x_hits_response(1, member=True)

    with pytest.raises(AcquisitionValidationError, match="empty WFS hits"):
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {"mode": "single_response"},
                },
            )
        )

    assert feature_calls == 1


def test_wfs_2_hits_require_explicit_zero_number_returned(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        assert query.get("resultType") == ["hits"]
        return Response(
            b'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
            b'numberMatched="1"/>',
            "application/xml",
        )

    with pytest.raises(AcquisitionValidationError, match="prove a feature count"):
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {"mode": "single_response"},
                },
            )
        )


def test_wfs_safe_snapshot_converges_and_rewrites_ephemeral_ids(
    store,
    limits,
):
    feature_calls = 0

    def handler(url, _etag, _modified):
        nonlocal feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(3)
        assert query["sortBy"] == ["stable_id A"]
        assert query["count"] == ["2"]
        offset = int(query["startIndex"][0])
        pass_index = feature_calls // 2
        feature_calls += 1
        values = [1, 2] if offset == 0 else [3]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "numberReturned": len(values),
                "timeStamp": f"volatile-pass-{pass_index}",
                "links": [
                    {
                        "rel": "next",
                        "href": f"https://volatile.example/{pass_index}",
                    }
                ],
                "crs": {
                    "type": "name",
                    "properties": {"name": "urn:ogc:def:crs:EPSG::25830"},
                },
                "features": [
                    {
                        "type": "Feature",
                        "id": f"ephemeral-{pass_index}-{value}",
                        "properties": {
                            "stable_id": value,
                            "name": f"road-{value}",
                        },
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            config={
                "wfs_snapshot": {
                    "mode": "paged",
                    "identity_properties": ["stable_id"],
                }
            },
        )
    )

    assert feature_calls == 4
    assert result.feature_count == 3
    assert result.stats["snapshot_convergence_passes"] == 2
    assert result.stats["stable_identity_count"] == 3
    assert result.stats["stable_identity_properties"] == ["stable_id"]
    assert result.stats["sort_by"] == "stable_id A"
    assert len(result.stats["snapshot_identity_sha256"]) == 64
    assert len(result.stats["snapshot_content_sha256"]) == 64
    pages = [
        item
        for item in result.artifacts
        if item.artifact_kind == "dataset"
    ]
    assert len(pages) == 2
    canonical_ids: list[str] = []
    for artifact in pages:
        with store.open_blob(artifact.blob.storage_key) as source:
            page = json.load(source)
        assert "timeStamp" not in page
        assert "links" not in page
        assert page["numberMatched"] == 3
        assert page["numberReturned"] == len(page["features"])
        assert page["crs"]["properties"]["name"].endswith("25830")
        canonical_ids.extend(
            feature["id"] for feature in page["features"]
        )
    assert len(canonical_ids) == len(set(canonical_ids)) == 3
    assert all(value.startswith("siur-wfs-") for value in canonical_ids)
    assert all("ephemeral" not in value for value in canonical_ids)
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["canonical_snapshot"] is True
    assert (
        manifest["materialization"]["snapshot_content_sha256"]
        == result.stats["snapshot_content_sha256"]
    )
    assert list((store.root / "staging").iterdir()) == []


def test_wfs_safe_snapshot_aborts_when_content_changes_between_passes(
    store,
    limits,
):
    feature_calls = 0
    blobs_before = {
        item
        for item in (store.root / "blobs" / "sha256").rglob("*")
        if item.is_file()
    }

    def handler(url, _etag, _modified):
        nonlocal feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(3)
        offset = int(query["startIndex"][0])
        pass_index = feature_calls // 2
        feature_calls += 1
        values = [1, 2] if offset == 0 else [3]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "features": [
                    {
                        "type": "Feature",
                        "id": f"source-{value}",
                        "properties": {
                            "stable_id": value,
                            "name": (
                                "changed"
                                if pass_index == 1 and value == 3
                                else f"road-{value}"
                            ),
                        },
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {
                        "mode": "paged",
                        "identity_properties": ["stable_id"],
                    }
                },
            )
        )

    assert captured.value.code == "unstable_snapshot"
    assert captured.value.retryable is True
    assert feature_calls == 4
    assert list((store.root / "staging").iterdir()) == []
    blobs_after = {
        item
        for item in (store.root / "blobs" / "sha256").rglob("*")
        if item.is_file()
    }
    assert {
        item.name for item in blobs_after - blobs_before
    } == {hashlib.sha256(WFS_CAPABILITIES).hexdigest()}


def test_wfs_safe_snapshot_deduplicates_by_reviewed_stable_identity(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(4)
        offset = int(query["startIndex"][0])
        values = [1, 2] if offset == 0 else [2, 3]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 4,
                "features": [
                    {
                        "type": "Feature",
                        "id": f"distinct-source-id-{offset}-{value}",
                        "properties": {"stable_id": value},
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {
                        "mode": "paged",
                        "identity_properties": ["stable_id"],
                    }
                },
            )
        )

    assert captured.value.code == "unstable_pagination"
    assert captured.value.retryable is True


def test_wfs_safe_snapshot_requires_advertised_complete_count(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(1)
        return json_response(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"stable_id": 1},
                        "geometry": None,
                    }
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {
                        "mode": "paged",
                        "identity_properties": ["stable_id"],
                    }
                },
            )
        )

    assert captured.value.code == "snapshot_completeness_unproven"
    assert captured.value.retryable is True


def test_wfs_safe_snapshot_requires_every_identity_property(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(1)
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 1,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"other": 1},
                        "geometry": None,
                    }
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {
                        "mode": "paged",
                        "identity_properties": ["stable_id"],
                    }
                },
            )
        )

    assert captured.value.code == "missing_pagination_identity"


def test_wfs_single_response_converges_as_a_canonical_multiset(
    store,
    limits,
):
    full_calls = 0

    def handler(url, _etag, _modified):
        nonlocal full_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            assert query["count"] == ["1"]
            return wfs_hits_response(3)
        assert "count" not in query
        assert "startIndex" not in query
        assert "sortBy" not in query
        values = [("a", 1), ("b", 2), ("a", 1)]
        if full_calls:
            values.reverse()
        pass_index = full_calls
        full_calls += 1
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "numberReturned": 3,
                "timeStamp": f"volatile-{pass_index}",
                "features": [
                    {
                        "type": "Feature",
                        "id": f"volatile-{pass_index}-{index}",
                        "properties": {"name": name, "value": value},
                        "geometry": {
                            "type": "Point",
                            "coordinates": [value, value],
                        },
                    }
                    for index, (name, value) in enumerate(values)
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            config={
                "wfs_snapshot": {"mode": "single_response"},
            },
        )
    )

    assert full_calls == 2
    assert result.feature_count == 3
    assert result.stats["snapshot_mode"] == "single_response"
    assert result.stats["stable_identity_derivation"] == (
        "canonical-feature-content-and-occurrence"
    )
    dataset = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "dataset"
    )
    with store.open_blob(dataset.blob.storage_key) as source:
        document = json.load(source)
    assert "timeStamp" not in document
    assert [item["properties"]["name"] for item in document["features"]] == [
        "a",
        "a",
        "b",
    ]
    identifiers = [item["id"] for item in document["features"]]
    assert len(identifiers) == len(set(identifiers)) == 3
    assert all(item.startswith("siur-wfs-") for item in identifiers)


def test_wfs_single_response_aborts_when_multiset_changes(
    store,
    limits,
):
    full_calls = 0

    def handler(url, _etag, _modified):
        nonlocal full_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(2)
        full_calls += 1
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 2,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "value": (
                                value
                                if full_calls == 1 or value == 1
                                else 3
                            )
                        },
                        "geometry": None,
                    }
                    for value in (1, 2)
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {"mode": "single_response"},
                },
            )
        )

    assert captured.value.code == "unstable_snapshot"
    assert captured.value.retryable is True
    assert full_calls == 2


def test_wfs_paged_snapshot_uses_independent_hits_count_when_pages_are_capped(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(5)
        offset = int(query["startIndex"][0])
        values = {
            0: [1, 2],
            2: [3, 4],
            4: [5],
        }[offset]
        return json_response(
            {
                "type": "FeatureCollection",
                # Mirrors GeoServer's capped page declaration: the independent
                # hits request, not this value, proves the complete count.
                "numberMatched": 3,
                "numberReturned": len(values),
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"fid": value},
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            config={
                "wfs_snapshot": {
                    "mode": "paged",
                    "identity_properties": ["fid"],
                }
            },
        )
    )

    assert result.feature_count == 5
    assert result.stats["number_matched"] == 5
    assert result.stats["page_count"] == 3
    assert result.stats["sort_by"] == "fid A"


def test_wfs_paged_snapshot_finishes_on_exact_count_at_page_limit(
    store,
    limits,
):
    feature_calls = 0
    expected_count = limits.page_size * limits.max_pages

    def handler(url, _etag, _modified):
        nonlocal feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(expected_count)
        offset = int(query["startIndex"][0])
        feature_calls += 1
        values = range(offset + 1, offset + limits.page_size + 1)
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": expected_count,
                "numberReturned": limits.page_size,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"fid": value},
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            config={
                "wfs_snapshot": {
                    "mode": "paged",
                    "identity_properties": ["fid"],
                }
            },
        )
    )

    assert result.feature_count == expected_count
    assert result.stats["page_count"] == limits.max_pages
    assert feature_calls == 2 * limits.max_pages


def test_wfs_paged_snapshot_rejects_more_features_than_hits_count(
    store,
    limits,
):
    feature_calls = 0

    def handler(url, _etag, _modified):
        nonlocal feature_calls
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(3)
        offset = int(query["startIndex"][0])
        feature_calls += 1
        values = [1, 2] if offset == 0 else [3, 4]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "numberReturned": 2,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"fid": value},
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {
                        "mode": "paged",
                        "identity_properties": ["fid"],
                    }
                },
            )
        )

    assert captured.value.code == "unstable_snapshot"
    assert captured.value.retryable is True
    assert feature_calls == 2
    assert list((store.root / "staging").iterdir()) == []


def test_wfs_reviewed_paged_snapshot_accepts_11000_with_default_limits(
    store,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(1)
        assert query["count"] == ["11000"]
        assert query["sortBy"] == ["fid A"]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 1,
                "numberReturned": 1,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"fid": 1},
                        "geometry": None,
                    }
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=AcquisitionLimits(),
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            config={
                "page_size": 11_000,
                "wfs_snapshot": {
                    "mode": "paged",
                    "identity_properties": ["fid"],
                },
            },
        )
    )

    assert result.feature_count == 1
    assert result.stats["page_size"] == 11_000


def test_wfs_paged_snapshot_requires_strict_identity_order(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(3)
        offset = int(query["startIndex"][0])
        values = [1, 3] if offset == 0 else [2]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"fid": value},
                        "geometry": None,
                    }
                    for value in values
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {
                        "mode": "paged",
                        "identity_properties": ["fid"],
                    }
                },
            )
        )

    assert captured.value.code == "unstable_pagination"
    assert captured.value.retryable is True


@pytest.mark.parametrize(
    "snapshot_config",
    [
        {},
        {"mode": "single_response", "identity_properties": ["fid"]},
        {"mode": "paged"},
        {"mode": "paged", "identity_properties": []},
        {"mode": "unknown"},
    ],
)
def test_wfs_snapshot_rejects_invalid_contract_configuration(
    store,
    limits,
    snapshot_config,
):
    transport = FakeTransport(
        lambda url, _etag, _modified: Response(
            WFS_CAPABILITIES,
            "application/xml",
        )
        if parse_qs(urlsplit(url).query).get("request")
        == ["GetCapabilities"]
        else pytest.fail("invalid WFS snapshot config reached GetFeature")
    )

    with pytest.raises(AcquisitionConfigurationError):
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=transport,
        ).acquire(
            candidate(
                "wfs",
                config={"wfs_snapshot": snapshot_config},
            )
        )


def test_wfs_convergence_applies_one_aggregate_upstream_byte_budget(
    store,
    limits,
):
    padded = "x" * 420

    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        if query.get("resultType") == ["hits"]:
            return wfs_hits_response(1)
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 1,
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"payload": padded},
                        "geometry": None,
                    }
                ],
            }
        )

    constrained = replace(
        limits,
        max_probe_bytes=512,
        max_page_bytes=1_024,
        max_dataset_bytes=1_024,
        max_total_bytes=1_200,
    )
    with pytest.raises(AcquisitionLimitError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=constrained,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                config={
                    "wfs_snapshot": {"mode": "single_response"},
                },
            )
        )

    assert captured.value.code == "snapshot_too_large"
    assert list((store.root / "staging").iterdir()) == []


def test_wfs_upstream_byte_stats_include_capabilities_and_styles(
    store,
    limits,
):
    upstream_bytes = 0
    style_document = sld_payload("workspace:blue")

    def respond(response):
        nonlocal upstream_bytes
        upstream_bytes += len(response.body)
        return response

    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        request = query.get("request", [None])[0]
        if request == "GetCapabilities":
            return respond(Response(WFS_CAPABILITIES, "application/xml"))
        if request == "GetStyles":
            return respond(
                Response(
                    style_document,
                    "application/vnd.ogc.sld+xml",
                )
            )
        if query.get("resultType") == ["hits"]:
            return respond(wfs_hits_response(1))
        return respond(
            json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 1,
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"fid": 1},
                            "geometry": None,
                        }
                    ],
                }
            )
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            remote_name="workspace:roads",
            config={
                "wfs_snapshot": {"mode": "single_response"},
                **style_config(("style:blue", "workspace:blue")),
            },
        )
    )

    assert result.stats["upstream_observed_bytes"] == upstream_bytes
    assert result.stats["upstream_capabilities_bytes"] == len(
        WFS_CAPABILITIES
    )
    assert result.stats["upstream_style_bytes"] == len(style_document)
    assert result.stats["retained_bytes_before_manifest"] < result.total_bytes


def test_wfs_remote_budget_rejects_style_after_both_snapshot_passes(
    store,
    limits,
):
    hits = wfs_hits_response(1)
    data = json_response(
        {
            "type": "FeatureCollection",
            "numberMatched": 1,
            "features": [
                {
                    "type": "Feature",
                    "properties": {"fid": 1},
                    "geometry": None,
                }
            ],
        }
    )
    style_document = sld_payload("workspace:blue")
    remote_limit = (
        len(WFS_CAPABILITIES)
        + 2 * (len(hits.body) + len(data.body))
        + len(style_document)
        - 1
    )
    largest_response = max(
        len(WFS_CAPABILITIES),
        len(hits.body),
        len(data.body),
        len(style_document),
    )
    constrained = replace(
        limits,
        max_probe_bytes=largest_response,
        max_page_bytes=largest_response,
        max_dataset_bytes=largest_response,
        max_total_bytes=remote_limit,
    )
    style_calls = 0

    def handler(url, _etag, _modified):
        nonlocal style_calls
        query = parse_qs(urlsplit(url).query)
        request = query.get("request", [None])[0]
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetStyles":
            style_calls += 1
            return Response(
                style_document,
                "application/vnd.ogc.sld+xml",
            )
        if query.get("resultType") == ["hits"]:
            return hits
        return data

    with pytest.raises(AcquisitionLimitError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=constrained,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                remote_name="workspace:roads",
                config={
                    "wfs_snapshot": {"mode": "single_response"},
                    **style_config(("style:blue", "workspace:blue")),
                },
            )
        )

    assert captured.value.code == "snapshot_too_large"
    assert style_calls == 1
    assert list((store.root / "staging").iterdir()) == []


def test_wfs_acquires_exact_standalone_slds_with_bundle_provenance(store, limits):
    configured_styles = style_config(
        ("style:blue", "workspace:blue"),
        ("style:red", "workspace:red"),
    )

    def handler(url, etag, modified):
        query = parse_qs(urlsplit(url).query)
        request = query.get("request", [None])[0]
        assert etag is None
        assert modified is None
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetFeature":
            return json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 1,
                    "features": [
                        {
                            "type": "Feature",
                            "id": "roads.1",
                            "properties": {},
                            "geometry": None,
                        }
                    ],
                }
            )
        assert request == "GetStyles"
        assert query["service"] == ["WMS"]
        assert query["layers"] == ["workspace:roads"]
        return Response(
            sld_payload("workspace:blue", "workspace:red"),
            "application/vnd.ogc.sld+xml",
        )

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
    ).acquire(
        candidate(
            "wfs",
            remote_name="workspace:roads",
            config=configured_styles,
        )
    )

    style_artifacts = [
        item for item in result.artifacts if item.artifact_kind == "style"
    ]
    assert [item.role for item in style_artifacts] == [
        "observation",
        "style",
        "style",
    ]
    assert sum(
        parse_qs(urlsplit(call["url"]).query).get("request") == ["GetStyles"]
        for call in transport.calls
    ) == 1
    standalone = [item for item in style_artifacts if item.role == "style"]
    assert [item.metadata["catalog_style_source_key"] for item in standalone] == [
        "style:blue",
        "style:red",
    ]
    bundle = next(item for item in style_artifacts if item.role == "observation")
    assert bundle.metadata["catalog_style_source_keys"] == [
        "style:blue",
        "style:red",
    ]
    for item in standalone:
        assert item.metadata["parent_sha256"] == bundle.blob.sha256
        with store.open_blob(item.blob.storage_key) as source:
            root = ElementTree.parse(source).getroot()
        names = [
            (element.text or "").strip()
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "UserStyle"
            for element in element
            if element.tag.rsplit("}", 1)[-1] == "Name"
        ]
        assert names == [item.metadata["remote_name"]]
        assert all(
            attribute.rsplit("}", 1)[-1].casefold() != "schemalocation"
            for element in root.iter()
            for attribute in element.attrib
        )
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["style_artifact_sha256"] == {
        item.metadata["catalog_style_source_key"]: item.blob.sha256
        for item in standalone
    }


def test_style_acquisition_fails_when_getstyles_omits_exact_remote_name(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        request = parse_qs(urlsplit(url).query).get("request", [None])[0]
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetFeature":
            return json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 0,
                    "features": [],
                }
            )
        return Response(sld_payload("workspace:other"), "application/xml")

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                remote_name="workspace:roads",
                config=style_config(("style:blue", "workspace:blue")),
            )
        )

    assert error.value.code == "style_unavailable"


def test_cross_origin_style_resource_is_persisted_as_missing_parity(
    store,
    limits,
):
    href = "https://assets.example.net/symbol.svg"

    def handler(url, _etag, _modified):
        request = parse_qs(urlsplit(url).query).get("request", [None])[0]
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetFeature":
            return json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 0,
                    "features": [],
                }
            )
        return Response(
            sld_payload("workspace:blue", external_href=href),
            "application/xml",
        )

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
    ).acquire(
        candidate(
            "wfs",
            remote_name="workspace:roads",
            config=style_config(("style:blue", "workspace:blue")),
        )
    )

    style = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style" and item.role == "style"
    )
    assert style.metadata["parity_kind"] == "missing"
    assert style.metadata["unresolved_resources"] == [
        {
            "original_href": href,
            "reason_code": "style_resource_origin_unreviewed",
        }
    ]
    assert all(
        urlsplit(call["url"]).hostname != "assets.example.net"
        for call in transport.calls
    )


def test_same_origin_style_resource_is_copied_and_packaged_locally(
    store,
    limits,
):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001"
        "08060000001f15c4890000000d49444154789c6360000000"
        "020001e221bc330000000049454e44ae426082"
    )

    def handler(url, _etag, _modified):
        parsed = urlsplit(url)
        request = parse_qs(parsed.query).get("request", [None])[0]
        if parsed.path.endswith("/symbols/symbol.png"):
            return Response(png, "image/png")
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetFeature":
            return json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 0,
                    "features": [],
                }
            )
        return Response(
            sld_payload(
                "workspace:blue",
                external_href="symbols/symbol.png",
            ),
            "application/xml",
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wfs",
            remote_name="workspace:roads",
            config=style_config(("style:blue", "workspace:blue")),
        )
    )

    resource = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style_resource"
    )
    style = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style" and item.role == "style"
    )
    package = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style_package"
    )
    local_path = f"resources/{resource.blob.sha256}.png"
    assert style.metadata["parity_kind"] == "adapted"
    assert style.metadata["resource_bindings"][0]["local_path"] == local_path
    with store.open_blob(style.blob.storage_key) as source:
        assert local_path.encode() in source.read()
    with store.open_blob(package.blob.storage_key) as source:
        with zipfile.ZipFile(source) as archive:
            assert sorted(archive.namelist()) == [local_path, "style.sld"]
            assert archive.read(local_path) == png


def test_style_acquisition_rejects_cross_origin_before_any_request(store, limits):
    transport = FakeTransport(
        lambda *_args: pytest.fail("cross-origin style config reached the network")
    )

    with pytest.raises(AcquisitionConfigurationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=transport,
        ).acquire(
            candidate(
                "wfs",
                remote_name="workspace:roads",
                config=style_config(
                    ("style:blue", "workspace:blue"),
                    endpoint="https://unreviewed.example.net/wms",
                ),
            )
        )

    assert error.value.code == "cross_origin_url"
    assert transport.calls == []


def test_style_acquisition_enforces_bounded_xml_complexity(store, limits):
    nested = "<Rule>" * 70 + "<Title>x</Title>" + "</Rule>" * 70
    unsafe = (
        '<StyledLayerDescriptor version="1.0.0" '
        'xmlns="http://www.opengis.net/sld"><NamedLayer>'
        '<Name>workspace:roads</Name><UserStyle><Name>workspace:blue</Name>'
        f"<FeatureTypeStyle>{nested}</FeatureTypeStyle>"
        "</UserStyle></NamedLayer></StyledLayerDescriptor>"
    ).encode()

    def handler(url, _etag, _modified):
        request = parse_qs(urlsplit(url).query).get("request", [None])[0]
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetFeature":
            return json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 0,
                    "features": [],
                }
            )
        return Response(unsafe, "application/xml")

    with pytest.raises(AcquisitionLimitError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                remote_name="workspace:roads",
                config=style_config(("style:blue", "workspace:blue")),
            )
        )

    assert error.value.code == "sld_complexity_limit"


@pytest.mark.parametrize(
    "declaration",
    [
        b'<!DOCTYPE StyledLayerDescriptor SYSTEM "https://evil.example/sld.dtd">',
        b'<!DOCTYPE StyledLayerDescriptor [<!ENTITY x "unsafe">]>',
    ],
)
def test_style_acquisition_rejects_dtd_and_entity_declarations(
    store,
    limits,
    declaration,
):
    unsafe = declaration + sld_payload("workspace:blue")

    def handler(url, _etag, _modified):
        request = parse_qs(urlsplit(url).query).get("request", [None])[0]
        if request == "GetCapabilities":
            return Response(WFS_CAPABILITIES, "application/xml")
        if request == "GetFeature":
            return json_response(
                {
                    "type": "FeatureCollection",
                    "numberMatched": 0,
                    "features": [],
                }
            )
        return Response(unsafe, "application/xml")

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "wfs",
                remote_name="workspace:roads",
                config=style_config(("style:blue", "workspace:blue")),
            )
        )

    assert error.value.code == "unsafe_sld_xml"


def test_style_acquisition_limits_declared_style_count_before_network(store, limits):
    styles = tuple(
        (f"style:{index:03d}", f"remote_{index:03d}")
        for index in range(257)
    )
    transport = FakeTransport(
        lambda *_args: pytest.fail("oversized style config reached the network")
    )

    with pytest.raises(AcquisitionLimitError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=transport,
        ).acquire(
            candidate(
                "wfs",
                remote_name="workspace:roads",
                config=style_config(*styles),
            )
        )

    assert error.value.code == "style_count_limit"
    assert transport.calls == []


def test_wfs_repeated_nonempty_page_fails_closed_without_manifest(store, limits):
    page = json_response(
        {
            "type": "FeatureCollection",
            "numberMatched": 4,
            "features": [
                {"type": "Feature", "id": "one", "properties": {}, "geometry": None},
                {"type": "Feature", "id": "two", "properties": {}, "geometry": None},
            ],
        }
    )

    def handler(url, _etag, _modified):
        if parse_qs(urlsplit(url).query).get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        return page

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store, limits=limits, downloader_factory=FakeTransport(handler)
        ).acquire(candidate("wfs"))

    assert error.value.code == "pagination_loop"
    assert list((store.root / "staging").iterdir()) == []


def test_wfs_requires_feature_ids_when_more_than_one_page_is_needed(store, limits):
    def handler(url, _etag, _modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        offset = int(query.get("startIndex", ["0"])[0])
        values = [1, 2] if offset == 0 else [3]
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "features": [
                    {"type": "Feature", "properties": {"n": value}, "geometry": None}
                    for value in values
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(candidate("wfs"))

    assert error.value.code == "missing_pagination_identity"


def test_ogc_api_follows_next_link_and_rejects_cross_origin_before_request(store, limits):
    def handler(url, _etag, _modified):
        if urlsplit(url).path.endswith("/collections"):
            return json_response({"collections": [{"id": "roads"}]})
        return json_response(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "id": "1", "properties": {}, "geometry": None}
                ],
                "links": [{"rel": "next", "href": "https://evil.example.net/items"}],
            }
        )

    transport = FakeTransport(handler)
    with pytest.raises(AcquisitionConfigurationError) as error:
        ReferenceAcquisitionPipeline(
            store, limits=limits, downloader_factory=transport
        ).acquire(
            candidate(
                "ogc_api_features",
                endpoint="https://data.example.es/api",
            )
        )

    assert error.value.code == "cross_origin_url"
    assert all("evil.example.net" not in call["url"] for call in transport.calls)


def test_ogc_api_follows_relative_next_link_to_a_complete_snapshot(store, limits):
    def handler(url, _etag, _modified):
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path.endswith("/collections"):
            return json_response({"collections": [{"id": "roads"}]})
        offset = int(query["offset"][0])
        values = [1, 2] if offset == 0 else [3]
        payload = {
            "type": "FeatureCollection",
            "numberMatched": 3,
            "features": [
                {"type": "Feature", "id": value, "properties": {}, "geometry": None}
                for value in values
            ],
        }
        if offset == 0:
            payload["links"] = [{"rel": "next", "href": "?limit=2&offset=2&f=json"}]
        return json_response(payload)

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "ogc_api_features",
            endpoint="https://data.example.es/api",
        )
    )

    assert result.feature_count == 3
    assert result.stats["page_count"] == 2
    assert result.stats["number_matched"] == 3
    pages = [
        item
        for item in result.artifacts
        if item.artifact_kind == "dataset" and item.role == "input"
    ]
    assert [item.metadata["page_index"] for item in pages] == [0, 1]
    assert [item.metadata["feature_count"] for item in pages] == [2, 1]
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["kind"] == "feature-pages"
    assert manifest["materialization"]["page_artifact_sha256"] == [
        item.blob.sha256 for item in pages
    ]


MITECO_OGC_SCOPE = {
    "bbox": [-7.6, 39.9, -1.3, 43.4],
    "bbox_crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
    "require_number_matched": True,
}


def test_reviewed_ogc_api_authors_local_style_without_getstyles_network(
    store,
    limits,
) -> None:
    reviewed = reviewed_catalog_candidate(
        endpoint=(
            "https://wms.mapama.gob.es/sig/agua/ZI_LaminasQ10/wms.aspx"
        ),
        layer_name="Z.I. con alta probabilidad",
        style_key="default",
        style_name="default",
    )

    def handler(url, _etag, _modified):
        path = urlsplit(url).path
        if path.endswith("/collections"):
            return json_response(
                {"collections": [{"id": "agua:Zi_laminas_q10"}]}
            )
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 1,
                "features": [
                    {
                        "type": "Feature",
                        "id": "flood-1",
                        "properties": {},
                        "geometry": None,
                    }
                ],
            }
        )

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=replace(
            limits,
            page_size=2_000,
            max_features=10_000,
        ),
        downloader_factory=transport,
    ).acquire(reviewed)

    style = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style"
    )
    package = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style_package"
    )
    with store.open_blob(style.blob.storage_key) as source:
        document = source.read()
    assert b"#ff0000" in document
    assert b"#c80000" in document
    assert style.metadata["parity_kind"] == "adapted"
    assert style.metadata["resource_bindings"] == []
    assert package.metadata["sld_sha256"] == style.blob.sha256
    assert result.stats["style_count"] == 1
    assert result.artifacts[-1].artifact_kind == "manifest"
    assert all(
        parse_qs(urlsplit(call["url"]).query).get("request")
        != ["GetStyles"]
        for call in transport.calls
    )


def test_ogc_api_preserves_reviewed_bbox_through_complete_pagination(
    store,
    limits,
):
    page_calls = 0

    def handler(url, _etag, _modified):
        nonlocal page_calls
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path.endswith("/collections"):
            return json_response(
                {"collections": [{"id": "agua:Zi_laminas_q10"}]}
            )
        page_calls += 1
        assert query["bbox"] == ["-7.6,39.9,-1.3,43.4"]
        offset = int(query["offset"][0])
        ids = [1, 2] if offset == 0 else [3]
        payload = {
            "type": "FeatureCollection",
            "numberMatched": 3,
            "features": [
                {
                    "type": "Feature",
                    "id": identifier,
                    "properties": {},
                    "geometry": None,
                }
                for identifier in ids
            ],
        }
        if offset == 0:
            payload["links"] = [
                {
                    "rel": "next",
                    "href": (
                        "?limit=2&offset=2&f=json"
                        "&bbox=-7.6,39.9,-1.3,43.4"
                    ),
                }
            ]
        return json_response(payload)

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "ogc_api_features",
            endpoint=(
                "https://gis.miteco.example.es/geoserver/ogc/features/v1/"
            ),
            remote_name="agua:Zi_laminas_q10",
            config=MITECO_OGC_SCOPE,
        )
    )

    assert page_calls == 2
    assert result.feature_count == 3
    assert result.stats["bbox"] == [-7.6, 39.9, -1.3, 43.4]
    assert result.stats["bbox_crs"] == MITECO_OGC_SCOPE["bbox_crs"]
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["collection"] == (
        "agua:Zi_laminas_q10"
    )
    assert manifest["materialization"]["bbox"] == [
        -7.6,
        39.9,
        -1.3,
        43.4,
    ]
    assert manifest["materialization"]["bbox_crs"] == (
        MITECO_OGC_SCOPE["bbox_crs"]
    )


def test_ogc_api_rejects_next_link_that_drops_reviewed_bbox(
    store,
    limits,
):
    item_calls = 0

    def handler(url, _etag, _modified):
        nonlocal item_calls
        if urlsplit(url).path.endswith("/collections"):
            return json_response(
                {"collections": [{"id": "agua:Zi_laminas_q10"}]}
            )
        item_calls += 1
        return json_response(
            {
                "type": "FeatureCollection",
                "numberMatched": 3,
                "features": [
                    {
                        "type": "Feature",
                        "id": 1,
                        "properties": {},
                        "geometry": None,
                    },
                    {
                        "type": "Feature",
                        "id": 2,
                        "properties": {},
                        "geometry": None,
                    },
                ],
                "links": [
                    {
                        "rel": "next",
                        "href": "?limit=2&offset=2&f=json",
                    }
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "ogc_api_features",
                endpoint=(
                    "https://gis.miteco.example.es/geoserver/ogc/"
                    "features/v1/"
                ),
                remote_name="agua:Zi_laminas_q10",
                config=MITECO_OGC_SCOPE,
            )
        )

    assert captured.value.code == "pagination_scope_changed"
    assert item_calls == 1


def test_scoped_ogc_api_requires_advertised_filtered_feature_count(
    store,
    limits,
):
    def handler(url, _etag, _modified):
        if urlsplit(url).path.endswith("/collections"):
            return json_response(
                {"collections": [{"id": "agua:Zi_laminas_q10"}]}
            )
        return json_response(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": 1,
                        "properties": {},
                        "geometry": None,
                    }
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "ogc_api_features",
                endpoint=(
                    "https://gis.miteco.example.es/geoserver/ogc/"
                    "features/v1/"
                ),
                remote_name="agua:Zi_laminas_q10",
                config=MITECO_OGC_SCOPE,
            )
        )

    assert captured.value.code == "snapshot_completeness_unproven"


@pytest.mark.parametrize(
    ("second_ids", "expected_code"),
    [
        ([2, 3], "unstable_pagination"),
        ([1, 2], "pagination_loop"),
    ],
)
def test_ogc_api_rejects_duplicate_features_or_repeated_pages(
    store,
    limits,
    second_ids,
    expected_code,
):
    calls = 0

    def handler(url, _etag, _modified):
        nonlocal calls
        if urlsplit(url).path.endswith("/collections"):
            return json_response({"collections": [{"id": "agua:Zi_laminas_q10"}]})
        calls += 1
        ids = [1, 2] if calls == 1 else second_ids
        payload = {
            "type": "FeatureCollection",
            "numberMatched": 4,
            "features": [
                {
                    "type": "Feature",
                    "id": identifier,
                    "properties": {},
                    "geometry": None,
                }
                for identifier in ids
            ],
        }
        if calls == 1 or expected_code == "pagination_loop":
            payload["links"] = [
                {"rel": "next", "href": "?limit=2&offset=2&f=json"}
            ]
        return json_response(payload)

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "ogc_api_features",
                endpoint=(
                    "https://gis.miteco.example.es/geoserver/ogc/"
                    "features/v1/"
                ),
                remote_name="agua:Zi_laminas_q10",
            )
        )

    assert captured.value.code == expected_code


def test_feature_page_rejects_duplicate_json_keys_before_blob_publication(store, limits):
    malformed = (
        b'{"type":"FeatureCollection","features":[],"features":[]}'
    )

    def handler(url, _etag, _modified):
        if parse_qs(urlsplit(url).query).get("request") == ["GetCapabilities"]:
            return Response(WFS_CAPABILITIES, "application/xml")
        return Response(malformed, "application/json")

    with pytest.raises(AcquisitionValidationError, match="strict UTF-8 JSON"):
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(candidate("wfs"))

    assert list((store.root / "staging").iterdir()) == []


ARCGIS_METADATA = {
    "currentVersion": 11.2,
    "layers": [{"id": 2, "name": "Roads", "type": "Feature Layer"}],
}


def test_arcgis_uses_object_id_batches_and_verifies_stable_membership(store, limits):
    id_requests = 0

    def handler(url, _etag, _modified):
        nonlocal id_requests
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path.endswith("/MapServer"):
            return json_response(ARCGIS_METADATA)
        if path.endswith("/MapServer/2"):
            return json_response(
                {
                    "id": 2,
                    "objectIdField": "OBJECTID",
                    "maxRecordCount": 2,
                    "geometryType": "esriGeometryPolyline",
                }
            )
        if query.get("returnIdsOnly") == ["true"]:
            id_requests += 1
            return json_response(
                {"objectIdFieldName": "OBJECTID", "objectIds": [3, 1, 2]}
            )
        ids = [int(value) for value in query["objectIds"][0].split(",")]
        return json_response(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "id": value, "properties": {}, "geometry": None}
                    for value in ids
                ],
            }
        )

    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=FakeTransport(handler)
    ).acquire(
        candidate(
            "arcgis_rest",
            remote_name="Roads",
            endpoint="https://data.example.es/arcgis/rest/services/Roads/MapServer",
        )
    )

    assert result.feature_count == 3
    assert result.stats["id_set_verified"] is True
    assert result.stats["page_count"] == 2
    assert id_requests == 2


def test_arcgis_rejects_membership_change_during_snapshot(store, limits):
    id_requests = 0

    def handler(url, _etag, _modified):
        nonlocal id_requests
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path.endswith("/MapServer"):
            return json_response(ARCGIS_METADATA)
        if path.endswith("/MapServer/2"):
            return json_response({"id": 2, "maxRecordCount": 2})
        if query.get("returnIdsOnly") == ["true"]:
            id_requests += 1
            values = [1, 2] if id_requests == 1 else [1, 2, 3]
            return json_response({"objectIdFieldName": "OBJECTID", "objectIds": values})
        ids = [int(value) for value in query["objectIds"][0].split(",")]
        return json_response(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "id": value, "properties": {}, "geometry": None}
                    for value in ids
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store, limits=limits, downloader_factory=FakeTransport(handler)
        ).acquire(
            candidate(
                "arcgis_rest",
                remote_name="Roads",
                endpoint="https://data.example.es/MapServer",
            )
        )

    assert error.value.code == "unstable_snapshot"
    assert error.value.retryable is True


def test_arcgis_rejects_a_page_with_the_wrong_object_ids(store, limits):
    id_requests = 0

    def handler(url, _etag, _modified):
        nonlocal id_requests
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path.endswith("/MapServer"):
            return json_response(ARCGIS_METADATA)
        if path.endswith("/MapServer/2"):
            return json_response({"id": 2, "maxRecordCount": 2})
        if query.get("returnIdsOnly") == ["true"]:
            id_requests += 1
            return json_response({"objectIdFieldName": "OBJECTID", "objectIds": [1, 2]})
        return json_response(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "id": 98, "properties": {}, "geometry": None},
                    {"type": "Feature", "id": 99, "properties": {}, "geometry": None},
                ],
            }
        )

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "arcgis_rest",
                remote_name="Roads",
                endpoint="https://data.example.es/MapServer",
            )
        )

    assert error.value.code == "unstable_snapshot"
    assert error.value.retryable is True


WCS_CAPABILITIES = b"""<wcs:Capabilities version="2.0.1"
 xmlns:wcs="http://www.opengis.net/wcs/2.0"><wcs:Contents>
 <wcs:CoverageSummary><wcs:CoverageId>terrain</wcs:CoverageId>
 </wcs:CoverageSummary></wcs:Contents></wcs:Capabilities>"""


def test_wcs_honors_conditional_dataset_response_without_partial_blob(store, limits):
    def handler(url, etag, modified):
        query = parse_qs(urlsplit(url).query)
        if query.get("request") == ["GetCapabilities"]:
            return Response(WCS_CAPABILITIES, "application/xml")
        assert query["coverageId"] == ["terrain"]
        assert etag == '"prior"'
        assert modified == "Wed, 22 Jul 2026 10:00:00 GMT"
        return Response(status=304, etag='"prior"', last_modified=modified)

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=transport
    ).acquire(
        candidate(
            "wcs",
            remote_name="terrain",
            config=style_config(
                ("style:terrain", "terrain_style"),
                layer_name="terrain",
            ),
        ),
        conditional=ConditionalRequest(
            source_url=(
                "https://data.example.es/service?service=WCS&request=GetCoverage"
                "&version=2.0.1&coverageId=terrain&format=image/tiff"
            ),
            etag='"prior"',
            last_modified="Wed, 22 Jul 2026 10:00:00 GMT",
        ),
    )

    assert result.not_modified is True
    assert result.manifest_sha256 is None
    assert [item.artifact_kind for item in result.artifacts] == ["capabilities"]
    assert [
        parse_qs(urlsplit(call["url"]).query)["request"][0]
        for call in transport.calls
    ] == ["GetCapabilities", "GetCoverage"]
    assert list((store.root / "staging").iterdir()) == []


def test_wcs_rejects_exception_xml_disguised_as_binary_before_commit(store, limits):
    def handler(url, _etag, _modified):
        if parse_qs(urlsplit(url).query).get("request") == ["GetCapabilities"]:
            return Response(WCS_CAPABILITIES, "application/xml")
        return Response(b"<Exception>coverage failed</Exception>", "application/octet-stream")

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store, limits=limits, downloader_factory=FakeTransport(handler)
        ).acquire(candidate("wcs", remote_name="terrain"))

    assert error.value.code == "invalid_raster"
    assert list((store.root / "staging").iterdir()) == []


def test_wcs_commits_a_structurally_georeferenced_tiff(store, limits):
    def handler(url, _etag, _modified):
        if parse_qs(urlsplit(url).query).get("request") == ["GetCapabilities"]:
            return Response(WCS_CAPABILITIES, "application/xml")
        return Response(geotiff_payload(), "image/tiff", etag='"coverage-v1"')

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(candidate("wcs", remote_name="terrain"))

    dataset = next(item for item in result.artifacts if item.artifact_kind == "dataset")
    assert dataset.metadata["data_format"] == "geotiff"
    assert dataset.blob.size_bytes == len(geotiff_payload())
    assert result.observed_etag == '"coverage-v1"'


def test_wcs_acquires_styles_after_conditional_dataset_success(store, limits):
    coverage_url = (
        "https://data.example.es/service?service=WCS&request=GetCoverage"
        "&version=2.0.1&coverageId=terrain&format=image/tiff"
    )

    def handler(url, etag, modified):
        query = parse_qs(urlsplit(url).query)
        request = query.get("request", [None])[0]
        if request == "GetCapabilities":
            assert etag is None
            return Response(WCS_CAPABILITIES, "application/xml")
        if request == "GetCoverage":
            assert etag == '"coverage-v1"'
            assert modified is None
            return Response(geotiff_payload(), "image/tiff", etag='"coverage-v2"')
        assert request == "GetStyles"
        assert etag is None
        assert modified is None
        assert query["layers"] == ["terrain"]
        assert "styles" not in query
        return Response(
            sld_payload("terrain_style", layer_name="terrain"),
            "application/vnd.ogc.sld+xml",
        )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(handler),
    ).acquire(
        candidate(
            "wcs",
            remote_name="terrain",
            config=style_config(
                ("style:terrain", "terrain_style"),
                layer_name="terrain",
            ),
        ),
        conditional=ConditionalRequest(
            source_url=coverage_url,
            etag='"coverage-v1"',
        ),
    )

    assert result.not_modified is False
    assert result.observed_etag == '"coverage-v2"'
    style = next(item for item in result.artifacts if item.role == "style")
    assert style.metadata["catalog_style_source_key"] == "style:terrain"
    assert result.stats["style_count"] == 1


def test_direct_download_is_content_addressed_and_records_conditional_headers(store, limits):
    payload = shapefile_zip_payload()

    def handler(_url, etag, modified):
        assert etag == '"v1"'
        assert modified is None
        return Response(
            payload,
            "application/zip",
            etag='"v2"',
            last_modified="Thu, 23 Jul 2026 08:00:00 GMT",
        )

    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=FakeTransport(handler)
    ).acquire(
        candidate(
            "download",
            endpoint="https://data.example.es/export.zip",
            config={"media_type": "application/zip", "data_format": "shapefile-zip"},
        ),
        conditional=ConditionalRequest(
            source_url="https://data.example.es/export.zip",
            etag='"v1"',
        ),
    )

    dataset = next(item for item in result.artifacts if item.artifact_kind == "dataset")
    digest = hashlib.sha256(payload).hexdigest()
    assert dataset.blob.sha256 == digest
    assert dataset.blob.storage_key.endswith(digest)
    assert result.observed_etag == '"v2"'
    assert result.observed_last_modified.isoformat() == "2026-07-23T08:00:00+00:00"
    assert (
        read_json_artifact(store, result, "manifest")["materialization"]["format"]
        == "shapefile-zip"
    )


def test_masked_geopackage_acquisition_keeps_composite_provenance(
    store,
    limits,
    monkeypatch,
    tmp_path,
):
    source_url = "https://grid.example.es/grid.gpkg"
    mask_url = "https://mask.example.es/cyl.geojson"
    source_payload = minimal_geopackage_payload(
        tmp_path / "grid-source.gpkg"
    )
    mask_payload = b'{"type":"FeatureCollection","features":[]}'
    mask_identity = MaskIdentity(
        sha256="a" * 64,
        feature_id="ES41",
        properties={"codnut2": "ES41"},
        coordinate_pairs=5,
    )
    derived_blob = store.put_stream(io.BytesIO(b"derived-only" * 100))
    identifier_sha256 = "b" * 64
    captured_source_path = None

    def validate_mask(_path, _size, spec):
        assert spec.mask_url == mask_url
        return mask_identity

    def derive(
        target_store,
        *,
        source_path,
        source_sha256,
        source_size_bytes,
        mask_path,
        mask_sha256,
        mask_size_bytes,
        mask_identity: MaskIdentity,
        spec,
        **_kwargs,
    ):
        nonlocal captured_source_path
        captured_source_path = source_path
        assert target_store is store
        assert source_path.is_relative_to(tmp_path / "transient")
        assert source_path.read_bytes() == source_payload
        assert source_sha256 == hashlib.sha256(source_payload).hexdigest()
        assert source_size_bytes == len(source_payload)
        assert mask_path.read_bytes() == mask_payload
        assert mask_sha256 == hashlib.sha256(mask_payload).hexdigest()
        assert mask_size_bytes == len(mask_payload)
        assert mask_identity == mask_identity_value
        assert spec.output_layer == "grid_100km_surf_cyl"
        return MaskedGeoPackageResult(
            blob=derived_blob,
            feature_count=19,
            identifier_sha256=identifier_sha256,
            validation={
                "schema": (
                    "reference-masked-geopackage-derivation/v1"
                ),
                "passed": True,
            },
        )

    mask_identity_value = mask_identity
    monkeypatch.setattr(
        acquisition_module,
        "validate_reviewed_mask",
        validate_mask,
    )
    monkeypatch.setattr(
        acquisition_module,
        "derive_masked_geopackage_files",
        derive,
    )

    def handler(url, etag, modified):
        assert modified is None
        if url == source_url:
            assert etag == '"source-v1"'
            return Response(
                source_payload,
                "application/geopackage+sqlite3",
                etag='"source-v2"',
            )
        assert url == mask_url
        assert etag is None
        return Response(mask_payload, "application/json")

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
        transient_root=tmp_path / "transient",
    ).acquire(
        candidate(
            "download",
            remote_name="grid_100km_surf",
            endpoint=source_url,
            config=masked_geopackage_config(
                mask_url,
                mask_identity_sha256=mask_identity.sha256,
                identifier_sha256=identifier_sha256,
            ),
        ),
        conditional=ConditionalRequest(
            source_url=source_url,
            etag='"source-v1"',
        ),
    )

    assert [item["url"] for item in transport.calls] == [
        source_url,
        mask_url,
    ]
    assert [policy.allowed_origins for policy in transport.policies] == [
        ("https://grid.example.es",),
        ("https://mask.example.es",),
    ]
    datasets = [
        item
        for item in result.artifacts
        if item.artifact_kind == "dataset"
    ]
    assert len(datasets) == 1
    assert datasets[0].metadata["materialization_input"] is True
    assert datasets[0].metadata["input_layer"] == (
        "grid_100km_surf_cyl"
    )
    assert datasets[0].metadata["derivation"][
        "raw_source_retained"
    ] is False
    observations = [
        item
        for item in result.artifacts
        if item.artifact_kind == "metadata"
    ]
    source_observation = next(
        item
        for item in observations
        if item.metadata["derivation_role"] == "source_dataset"
    )
    with store.open_blob(source_observation.blob.storage_key) as stream:
        source_document = json.load(stream)
    assert source_observation.role == "input"
    assert source_observation.source_url == source_url
    assert source_observation.final_url == source_url
    assert source_observation.upstream_etag == '"source-v2"'
    assert source_document["sha256"] == hashlib.sha256(
        source_payload
    ).hexdigest()
    assert source_document["retained"] is False
    assert source_document["discarded_after_derivation"] is True
    mask = next(
        item
        for item in observations
        if item.metadata["derivation_role"] == "spatial_mask"
    )
    assert mask.metadata["derivation_role"] == "spatial_mask"
    assert captured_source_path is not None
    assert captured_source_path.exists() is False
    source_digest = hashlib.sha256(source_payload).hexdigest()
    assert (
        store.root
        / "blobs"
        / "sha256"
        / source_digest[:2]
        / source_digest
    ).exists() is False
    assert list((tmp_path / "transient" / "staging").iterdir()) == []
    assert list((tmp_path / "transient" / "blobs" / "sha256").iterdir()) == []
    assert result.feature_count == 19
    assert result.stats["identifier_sha256"] == identifier_sha256
    assert result.stats["composite_snapshot_full_refresh"] is True
    assert result.stats["raw_source_retained"] is False
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["dataset_sha256"] == (
        derived_blob.sha256
    )
    assert manifest["materialization"]["input_layer"] == (
        "grid_100km_surf_cyl"
    )


def test_masked_geopackage_304_still_validates_independent_mask(
    store,
    limits,
    monkeypatch,
    tmp_path,
):
    source_url = "https://grid.example.es/grid.gpkg"
    mask_url = "https://mask.example.es/cyl.geojson"
    mask_payload = b"{}"
    mask_identity = MaskIdentity(
        sha256="a" * 64,
        feature_id="ES41",
        properties={"codnut2": "ES41"},
        coordinate_pairs=5,
    )
    monkeypatch.setattr(
        acquisition_module,
        "validate_reviewed_mask",
        lambda *_args: mask_identity,
    )
    monkeypatch.setattr(
        acquisition_module,
        "derive_masked_geopackage_files",
        lambda *_args, **_kwargs: pytest.fail(
            "a dataset 304 must not run the expensive derivation"
        ),
    )

    def handler(url, etag, modified):
        assert modified is None
        if url == source_url:
            assert etag == '"source-v1"'
            return Response(status=304, etag='"source-v1"')
        assert url == mask_url
        assert etag is None
        return Response(mask_payload, "application/json")

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
        transient_root=tmp_path / "transient",
    ).acquire(
        candidate(
            "download",
            remote_name="grid_100km_surf",
            endpoint=source_url,
            config=masked_geopackage_config(mask_url),
        ),
        conditional=ConditionalRequest(
            source_url=source_url,
            etag='"source-v1"',
        ),
    )

    assert result.not_modified is True
    assert [item["url"] for item in transport.calls] == [
        source_url,
        mask_url,
    ]
    assert len(result.artifacts) == 1
    assert result.artifacts[0].metadata["mask_identity"][
        "sha256"
    ] == mask_identity.sha256
    assert result.stats == {
        "not_modified": True,
        "composite_inputs_checked": True,
        "mask_identity_sha256": mask_identity.sha256,
        "style_count": 0,
    }


def test_masked_geopackage_requires_nonpersistent_transient_storage(
    store,
    limits,
):
    selected = reviewed_eurostat_grid_candidate()
    transport = FakeTransport(
        lambda *_args: pytest.fail(
            "missing transient storage must fail before network access"
        )
    )

    with pytest.raises(AcquisitionConfigurationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=transport,
        ).acquire(selected)

    assert error.value.code == "transient_storage_unavailable"
    assert transport.calls == []


def test_transient_storage_must_not_overlap_persistent_store(
    store,
    limits,
):
    for root in (store.root, store.root / "transient"):
        with pytest.raises(ValueError, match="must not overlap"):
            ReferenceAcquisitionPipeline(
                store,
                limits=limits,
                transient_root=root,
            )


def test_transient_source_cleanup_failure_fails_closed(
    tmp_path,
    monkeypatch,
):
    original_unlink = Path.unlink
    transient_root = tmp_path / "transient"

    def fail_transient_unlink(path, *args, **kwargs):
        if path.parent == transient_root / "staging":
            raise OSError("injected transient unlink failure")
        return original_unlink(path, *args, **kwargs)

    with ReferenceBlobStore(transient_root) as transient:
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "unlink", fail_transient_unlink)
            with pytest.raises(AcquisitionPersistenceError) as error:
                with acquisition_module._strict_staging(
                    transient,
                    max_bytes=1024,
                ) as staging:
                    staging.write(b"raw source")

        assert error.value.code == "staging_cleanup_failed"
        leftovers = list((transient_root / "staging").iterdir())
        assert len(leftovers) == 1
        leftovers[0].unlink()


def test_strict_staging_does_not_suppress_body_failure(tmp_path):
    with ReferenceBlobStore(tmp_path / "store") as strict_store:
        with pytest.raises(RuntimeError, match="injected body failure"):
            with acquisition_module._strict_staging(
                strict_store,
                max_bytes=1024,
            ) as staging:
                staging.write(b"partial")
                raise RuntimeError("injected body failure")

        assert list((strict_store.root / "staging").iterdir()) == []


def test_reviewed_eurostat_grid_authors_all_styles_without_idecyl_network(
    store,
    limits,
    monkeypatch,
    tmp_path,
):
    selected = reviewed_eurostat_grid_candidate()
    transform = selected.config["vector_transform"]
    source_payload = minimal_geopackage_payload(
        tmp_path / "eurostat-grid.gpkg"
    )
    mask_payload = b'{"type":"FeatureCollection","features":[]}'
    mask_identity = MaskIdentity(
        sha256=transform["mask_identity_sha256"],
        feature_id="1124753",
        properties={"codnut2": "ES41"},
        coordinate_pairs=5,
    )
    derived_blob = store.put_stream(io.BytesIO(b"derived-grid" * 100))
    monkeypatch.setattr(
        acquisition_module,
        "validate_reviewed_mask",
        lambda *_args: mask_identity,
    )
    monkeypatch.setattr(
        acquisition_module,
        "derive_masked_geopackage_files",
        lambda *_args, **_kwargs: MaskedGeoPackageResult(
            blob=derived_blob,
            feature_count=transform["expected_feature_count"],
            identifier_sha256=transform[
                "expected_identifier_sha256"
            ],
            validation={
                "schema": "reference-masked-geopackage-derivation/v1",
                "passed": True,
            },
        ),
    )

    def handler(url, etag, modified):
        assert etag is None
        assert modified is None
        if url == selected.endpoint_url:
            return Response(
                source_payload,
                "application/geopackage+sqlite3",
            )
        if url == transform["mask_url"]:
            return Response(mask_payload, "application/json")
        pytest.fail(f"unexpected grid acquisition URL: {url}")

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
        transient_root=tmp_path / "transient",
    ).acquire(selected)

    assert [item["url"] for item in transport.calls] == [
        selected.endpoint_url,
        transform["mask_url"],
    ]
    assert [policy.allowed_origins for policy in transport.policies] == [
        ("https://gisco-services.ec.europa.eu",),
        ("https://api-features.ign.es",),
    ]
    styles = [
        item
        for item in result.artifacts
        if item.artifact_kind == "style" and item.role == "style"
    ]
    packages = [
        item
        for item in result.artifacts
        if item.artifact_kind == "style_package"
    ]
    assert len(styles) == len(packages) == 3
    expected_colors = {
        "rejilla_eurostat_cyl_blanco": "#ffffff",
        "rejilla_eurostat_cyl_fucsia": "#e6007e",
        "rejilla_eurostat_cyl_morado": "#6d28d9",
    }
    for style in styles:
        source_key = style.metadata["catalog_style_source_key"]
        with store.open_blob(style.blob.storage_key) as stream:
            document = ElementTree.fromstring(stream.read())
        css = {
            element.attrib["name"]: (element.text or "")
            for element in document.iter()
            if element.tag.endswith("CssParameter")
        }
        assert css["fill-opacity"] == "0"
        assert css["stroke"] == expected_colors[source_key]
        assert css["stroke-width"] == "1"
        maximum = next(
            element
            for element in document.iter()
            if element.tag.endswith("MaxScaleDenominator")
        )
        assert maximum.text == "4000000"
        evidence = style.metadata["authored_local_evidence"]
        assert evidence["parity_kind"] == "adapted"
        assert evidence["exact_style_claim"] is False
        assert style.source_url is None
        assert style.final_url is None
    assert result.stats["style_count"] == 3
    manifest_styles = read_json_artifact(store, result, "manifest")[
        "materialization"
    ]["style_artifact_sha256"]
    assert set(manifest_styles) == set(expected_colors)


def test_reviewed_eurostat_grid_304_uses_only_gisco_and_ign_origins(
    store,
    limits,
    monkeypatch,
    tmp_path,
):
    selected = reviewed_eurostat_grid_candidate()
    transform = selected.config["vector_transform"]
    mask_identity = MaskIdentity(
        sha256=transform["mask_identity_sha256"],
        feature_id="1124753",
        properties={"codnut2": "ES41"},
        coordinate_pairs=5,
    )
    monkeypatch.setattr(
        acquisition_module,
        "validate_reviewed_mask",
        lambda *_args: mask_identity,
    )
    monkeypatch.setattr(
        acquisition_module,
        "derive_masked_geopackage_files",
        lambda *_args, **_kwargs: pytest.fail(
            "a dataset 304 must not run the expensive derivation"
        ),
    )
    def handler(url, etag, _modified):
        if url == selected.endpoint_url:
            assert etag == '"source-v1"'
            return Response(
                status=304,
                etag='"source-v1"',
            )
        if url == transform["mask_url"]:
            assert etag is None
            return Response(b"{}", "application/json")
        pytest.fail(f"unexpected grid acquisition URL: {url}")

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
        transient_root=tmp_path / "transient",
    ).acquire(
            selected,
            conditional=ConditionalRequest(
                source_url=selected.endpoint_url,
                etag='"source-v1"',
            ),
    )

    assert result.not_modified is True
    assert [item["url"] for item in transport.calls] == [
        selected.endpoint_url,
        transform["mask_url"],
    ]
    assert [policy.allowed_origins for policy in transport.policies] == [
        ("https://gisco-services.ec.europa.eu",),
        ("https://api-features.ign.es",),
    ]
    assert all(
        parse_qs(urlsplit(call["url"]).query).get("request")
        != ["GetStyles"]
        for call in transport.calls
    )
    assert result.stats["style_count"] == 0


def test_conditional_headers_are_not_reused_for_a_different_url(store, limits):
    transport = FakeTransport(
        lambda _url, etag, modified: (
            pytest.fail("validators leaked across resource URLs")
            if etag is not None or modified is not None
            else Response(shapefile_zip_payload(), "application/zip")
        )
    )
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
    ).acquire(
        candidate(
            "download",
            endpoint="https://data.example.es/new.zip",
            config={"media_type": "application/zip", "data_format": "shapefile-zip"},
        ),
        conditional=ConditionalRequest(
            source_url="https://data.example.es/old.zip",
            etag='"old"',
        ),
    )

    assert result.not_modified is False
    assert transport.calls[0]["etag"] is None


ATOM_FEED = b"""<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>roads</id><title>Roads</title>
 <link rel="enclosure" href="files/roads.gpkg" /></entry></feed>"""


def test_atom_resolves_one_relative_same_origin_enclosure(store, limits):
    def handler(url, _etag, _modified):
        if url.endswith("/feed.xml"):
            return Response(ATOM_FEED, "application/atom+xml")
        assert url == "https://data.example.es/catalog/files/roads.gpkg"
        return Response(b"fgb\x03fgb\x01local-data", "application/x-flatgeobuf")

    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=FakeTransport(handler)
    ).acquire(
        candidate(
            "atom",
            endpoint="https://data.example.es/catalog/feed.xml",
            config={
                "media_type": "application/x-flatgeobuf",
                "data_format": "flatgeobuf",
            },
        )
    )

    assert [item.artifact_kind for item in result.artifacts] == [
        "metadata",
        "dataset",
        "manifest",
    ]


def test_atom_never_fetches_cross_origin_enclosure(store, limits):
    feed = ATOM_FEED.replace(
        b"files/roads.gpkg",
        b"https://unreviewed.example.net/roads.gpkg",
    )
    transport = FakeTransport(
        lambda _url, _etag, _modified: Response(feed, "application/atom+xml")
    )

    with pytest.raises(AcquisitionConfigurationError) as error:
        ReferenceAcquisitionPipeline(
            store, limits=limits, downloader_factory=transport
        ).acquire(candidate("atom", endpoint="https://data.example.es/feed.xml"))

    assert error.value.code == "cross_origin_url"
    assert len(transport.calls) == 1


def test_nested_atom_acquires_ordered_cadastral_gml_zip_snapshot(
    store,
    limits,
):
    root_url = "https://catastro.example.es/root.xml"
    feed_05 = "https://catastro.example.es/05/feed.xml"
    feed_09 = "https://catastro.example.es/09/feed.xml"
    datasets = {
        "https://catastro.example.es/05/05001%20ALFA/A.05001.zip": (
            cadastral_gml_zip_payload("05001")
        ),
        "https://catastro.example.es/05/05002-BETA/A.05002.zip": (
            cadastral_gml_zip_payload("05002")
        ),
        "https://catastro.example.es/09/09001-GAMMA/A.09001.zip": (
            cadastral_gml_zip_payload("09001")
        ),
    }

    def handler(url, etag, modified):
        assert etag is None
        assert modified is None
        if url == root_url:
            return Response(
                atom_feed_payload(
                    "http://catastro.example.es/09/feed.xml",
                    "http://catastro.example.es/05/feed.xml",
                ),
                "application/atom+xml",
            )
        if url == feed_05:
            return Response(
                atom_feed_payload(
                    "05002-BETA/A.05002.zip",
                    "05001 ALFA/A.05001.zip",
                ),
                "application/atom+xml",
            )
        if url == feed_09:
            return Response(
                atom_feed_payload("09001-GAMMA/A.09001.zip"),
                "application/atom+xml",
            )
        return Response(
            datasets[url],
            (
                "application/x-zip-compressed"
                if "05001" in url
                else "application/zip"
            ),
        )

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
    ).acquire(
        candidate(
            "atom",
            endpoint=root_url,
            config={
                "data_format": "inspire-cadastral-parcel-gml-zip",
                "input_layer": "CadastralParcel",
                "media_types": [
                    "application/octet-stream",
                    "application/x-zip-compressed",
                    "application/zip",
                ],
                "nested_feed_urls": [feed_05, feed_09],
            },
        ),
        conditional=ConditionalRequest(
            source_url="https://catastro.example.es/old.zip",
            etag='"old"',
        ),
    )

    assert [item["url"] for item in transport.calls] == [
        root_url,
        feed_05,
        feed_09,
        *datasets,
    ]
    dataset_artifacts = [
        item for item in result.artifacts if item.artifact_kind == "dataset"
    ]
    assert [item.metadata["page_index"] for item in dataset_artifacts] == [
        0,
        1,
        2,
    ]
    assert [
        item.metadata["archive_member"] for item in dataset_artifacts
    ] == [
        "A.ES.SDGC.CP.05001.cadastralparcel.gml",
        "A.ES.SDGC.CP.05002.cadastralparcel.gml",
        "A.ES.SDGC.CP.09001.cadastralparcel.gml",
    ]
    assert all(
        item.metadata["input_layer"] == "CadastralParcel"
        for item in dataset_artifacts
    )
    assert result.stats["nested_feed_count"] == 2
    assert result.stats["dataset_count"] == 3
    manifest = read_json_artifact(store, result, "manifest")
    assert manifest["materialization"]["kind"] == "dataset-parts"
    assert len(manifest["materialization"]["dataset_artifact_sha256"]) == 3


def test_reviewed_catastro_atom_authors_closed_local_style_before_manifest(
    store,
    limits,
) -> None:
    reviewed = reviewed_catalog_candidate(
        endpoint=(
            "https://ovc.catastro.meh.es/Cartografia/WMS/"
            "ServidorWMS.aspx"
        ),
        layer_name="Catastro",
        style_key="default",
        style_name="Default",
    )
    nested_urls = reviewed.config["nested_feed_urls"]
    dataset_by_feed = {
        feed_url: (
            feed_url.rsplit("/", 1)[0] + f"/municipality/A.{code}001.zip",
            f"{code}001",
        )
        for code, feed_url in zip(
            ["05", "09", "24", "34", "37", "40", "42", "47", "49"],
            nested_urls,
            strict=True,
        )
    }

    def handler(url, _etag, _modified):
        if url == reviewed.endpoint_url:
            return Response(
                atom_feed_payload(*nested_urls),
                "application/atom+xml",
            )
        if url in dataset_by_feed:
            dataset_url, _code = dataset_by_feed[url]
            return Response(
                atom_feed_payload(dataset_url),
                "application/atom+xml",
            )
        code = next(
            expected_code
            for dataset_url, expected_code in dataset_by_feed.values()
            if dataset_url == url
        )
        return Response(
            cadastral_gml_zip_payload(code),
            "application/zip",
        )

    transport = FakeTransport(handler)
    result = ReferenceAcquisitionPipeline(
        store,
        limits=replace(limits, max_pages=20),
        downloader_factory=transport,
    ).acquire(reviewed)

    style = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style"
    )
    package = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style_package"
    )
    assert style.metadata["catalog_style_source_key"] == "default"
    assert style.metadata["remote_name"] == "Default"
    assert style.metadata["parity_kind"] == "adapted"
    assert package.metadata["resource_bindings"] == []
    assert result.stats["dataset_count"] == 9
    assert result.stats["style_count"] == 1
    assert result.artifacts[-1].artifact_kind == "manifest"
    assert all(
        parse_qs(urlsplit(call["url"]).query).get("request")
        != ["GetStyles"]
        for call in transport.calls
    )


def test_nested_atom_rejects_duplicate_or_excess_dataset_sets(
    store,
    limits,
):
    root_url = "https://catastro.example.es/root.xml"
    feeds = [
        "https://catastro.example.es/05/feed.xml",
        "https://catastro.example.es/09/feed.xml",
    ]

    def acquire(handler, nested_feeds):
        transport = FakeTransport(handler)
        pipeline = ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=transport,
        )
        selected = candidate(
            "atom",
            endpoint=root_url,
            config={
                "data_format": "inspire-cadastral-parcel-gml-zip",
                "media_type": "application/zip",
                "nested_feed_urls": nested_feeds,
            },
        )
        return transport, pipeline, selected

    duplicate = "https://catastro.example.es/data/repeated.zip"

    def duplicate_handler(url, _etag, _modified):
        if url == root_url:
            return Response(
                atom_feed_payload(
                    "http://catastro.example.es/05/feed.xml",
                    "http://catastro.example.es/09/feed.xml",
                ),
                "application/atom+xml",
            )
        return Response(
            atom_feed_payload(duplicate),
            "application/atom+xml",
        )

    transport, pipeline, selected = acquire(duplicate_handler, feeds)
    with pytest.raises(AcquisitionValidationError) as duplicate_error:
        pipeline.acquire(selected)
    assert duplicate_error.value.code == "atom_dataset_duplicate"
    assert len(transport.calls) == 3

    one_feed = feeds[:1]

    def limit_handler(url, _etag, _modified):
        if url == root_url:
            return Response(
                atom_feed_payload("http://catastro.example.es/05/feed.xml"),
                "application/atom+xml",
            )
        return Response(
            atom_feed_payload(
                *(
                    f"https://catastro.example.es/data/{index}.zip"
                    for index in range(limits.max_pages + 1)
                )
            ),
            "application/atom+xml",
        )

    transport, pipeline, selected = acquire(limit_handler, one_feed)
    with pytest.raises(AcquisitionLimitError) as limit_error:
        pipeline.acquire(selected)
    assert limit_error.value.code == "page_limit"
    assert len(transport.calls) == 2


def test_nested_atom_rejects_archive_without_unique_parcel_gml(
    store,
    limits,
):
    root_url = "https://catastro.example.es/root.xml"
    feed_url = "https://catastro.example.es/05/feed.xml"
    dataset_url = "https://catastro.example.es/data/invalid.zip"
    invalid = io.BytesIO()
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("metadata.xml", b"<metadata/>")

    def handler(url, _etag, _modified):
        if url == root_url:
            return Response(
                atom_feed_payload("http://catastro.example.es/05/feed.xml"),
                "application/atom+xml",
            )
        if url == feed_url:
            return Response(
                atom_feed_payload(dataset_url),
                "application/atom+xml",
            )
        return Response(invalid.getvalue(), "application/zip")

    with pytest.raises(AcquisitionValidationError) as captured:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(handler),
        ).acquire(
            candidate(
                "atom",
                endpoint=root_url,
                config={
                    "data_format": (
                        "inspire-cadastral-parcel-gml-zip"
                    ),
                    "media_type": "application/zip",
                    "nested_feed_urls": [feed_url],
                },
            )
        )

    assert captured.value.code == "cadastral_parcel_member_invalid"


def test_direct_geotiff_zip_validates_and_records_selected_member(
    store,
    limits,
):
    payload = geotiff_zip_payload("EroPotNiveles_41.tiff")
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                payload,
                "application/zip",
            )
        ),
    ).acquire(
        candidate(
            "download",
            endpoint="https://miteco.example.es/E_Potencial.zip",
            config={
                "data_format": "geotiff-zip",
                "media_type": "application/zip",
            },
        )
    )

    dataset = next(
        item for item in result.artifacts if item.artifact_kind == "dataset"
    )
    assert dataset.metadata["archive_member"] == "EroPotNiveles_41.tiff"
    assert dataset.metadata["uncompressed_bytes"] > 100


def test_reviewed_geotiff_zip_preserves_complete_vat_value_mapping(
    store,
    limits,
):
    mapping = [
        (64, 7),
        (65, 1),
        (66, 6),
        (67, 5),
        (68, 4),
        (69, 3),
        (75, 2),
        (80, 9),
        (92, 8),
    ]
    payload = geotiff_zip_payload(
        "EroPotNiveles_41.tiff",
        vat_class_field="EroPot_pb",
        value_class_mapping=mapping,
    )
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                payload,
                "application/zip",
            )
        ),
    ).acquire(
        candidate(
            "download",
            endpoint="https://miteco.example.es/E_Potencial.zip",
            config={
                "data_format": "geotiff-zip",
                "media_type": "application/zip",
                "vat_value_field": "Value",
                "vat_class_field": "EroPot_pb",
                "vat_class_values": list(range(1, 10)),
            },
        )
    )

    dataset = next(
        item for item in result.artifacts if item.artifact_kind == "dataset"
    )
    vat = dataset.metadata["raster_value_attribute_table"]
    assert vat["member"] == "EroPotNiveles_41.tiff.vat.dbf"
    assert vat["row_count"] == len(mapping)
    assert vat["value_field"] == "Value"
    assert vat["class_field"] == "EroPot_pb"
    assert vat["value_class_mapping"] == [
        {"value": value, "class_value": class_value}
        for value, class_value in sorted(mapping)
    ]
    assert len(vat["sha256"]) == 64


def test_reviewed_ines_download_authors_colormap_from_validated_vat(
    store,
    limits,
) -> None:
    mapping = [
        (64, 7),
        (65, 1),
        (66, 6),
        (67, 5),
        (68, 4),
        (69, 3),
        (75, 2),
        (80, 9),
        (92, 8),
        (120, 1),
    ]
    payload = geotiff_zip_payload(
        "EroPotNiveles_41.tiff",
        vat_class_field="EroPot_pb",
        value_class_mapping=mapping,
    )
    reviewed = reviewed_catalog_candidate(
        endpoint=(
            "https://wms.mapama.gob.es/sig/Biodiversidad/"
            "INESErosionPotencial"
        ),
        layer_name="NZ.HazardArea",
        style_key="biodiversidad_ines_erosionpotencial",
        style_name="Biodiversidad_INES_ErosionPotencial",
    )
    transport = FakeTransport(
        lambda _url, _etag, _modified: Response(
            payload,
            "application/zip",
        )
    )

    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=transport,
    ).acquire(reviewed)

    style = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style"
    )
    package = next(
        item
        for item in result.artifacts
        if item.artifact_kind == "style_package"
    )
    with store.open_blob(style.blob.storage_key) as source:
        root = ElementTree.fromstring(source.read())
    entries = [
        element
        for element in root.iter()
        if element.tag.endswith("}ColorMapEntry")
    ]
    assert [int(item.attrib["quantity"]) for item in entries] == [
        value for value, _class_value in mapping
    ]
    assert package.metadata["sld_sha256"] == style.blob.sha256
    assert package.metadata["package_members"] == [
        {"path": "style.sld", "sha256": style.blob.sha256}
    ]
    assert result.stats["style_count"] == 1
    assert len(transport.calls) == 1
    assert parse_qs(urlsplit(transport.calls[0]["url"]).query).get(
        "request"
    ) is None


def test_reviewed_geotiff_zip_rejects_duplicate_vat_values(store, limits):
    payload = geotiff_zip_payload(
        "EroPotNiveles_41.tiff",
        vat_class_field="EroPot_pb",
        value_class_mapping=[
            (64, 1),
            (64, 2),
            *[(64 + value, value) for value in range(3, 10)],
        ],
    )
    pipeline = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                payload,
                "application/zip",
            )
        ),
    )

    with pytest.raises(AcquisitionValidationError) as captured:
        pipeline.acquire(
            candidate(
                "download",
                endpoint="https://miteco.example.es/E_Potencial.zip",
                config={
                    "data_format": "geotiff-zip",
                    "media_type": "application/zip",
                    "vat_value_field": "Value",
                    "vat_class_field": "EroPot_pb",
                    "vat_class_values": list(range(1, 10)),
                },
            )
        )

    assert captured.value.code == "raster_vat_invalid"


TILE_CONFIG = {
    "bounds": {"west": -7.1, "south": 40, "east": -1.7, "north": 43.3},
    "min_zoom": 6,
    "max_zoom": 18,
    "format": "image/png",
    "style_name": "default",
    "coverage_required": True,
}


def test_xyz_materializes_tile_source_manifest_without_network_seed(store, limits):
    transport = FakeTransport(
        lambda *_args: pytest.fail("XYZ materialization must not access the network")
    )
    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=transport
    ).acquire(
        candidate(
            "xyz",
            endpoint="https://tiles.example.es/base/{z}/{x}/{-y}.png",
            config=TILE_CONFIG,
        )
    )

    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["scheme"] == "tms"
    assert descriptor["url_template"].endswith("/{z}/{x}/{-y}.png")
    assert result.stats["seeded"] is False
    assert transport.calls == []


def test_xyz_rejects_unknown_or_repeated_template_tokens(store, limits):
    for endpoint in (
        "https://tiles.example.es/{z}/{x}/{quadkey}.png",
        "https://tiles.example.es/{z}/{x}/{x}/{y}.png",
    ):
        with pytest.raises(AcquisitionConfigurationError):
            ReferenceAcquisitionPipeline(
                store,
                limits=limits,
                downloader_factory=FakeTransport(lambda *_args: None),
            ).acquire(candidate("xyz", endpoint=endpoint, config=TILE_CONFIG))


WMTS_CAPABILITIES = b"""<Capabilities version="1.0.0"
 xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1">
 <Contents><Layer><ows:Identifier>ortho</ows:Identifier><Style isDefault="true">
 <ows:Identifier>default</ows:Identifier></Style><Format>image/png</Format>
 <TileMatrixSetLink><TileMatrixSet>GoogleMapsCompatible</TileMatrixSet>
 <TileMatrixSetLimits><TileMatrixLimits><TileMatrix>arbitrary-one</TileMatrix>
 <MinTileRow>0</MinTileRow><MaxTileRow>1</MaxTileRow>
 <MinTileCol>0</MinTileCol><MaxTileCol>1</MaxTileCol>
 </TileMatrixLimits></TileMatrixSetLimits></TileMatrixSetLink>
 <ResourceURL format="image/png" resourceType="Tile"
 template="https://data.example.es/wmts/ortho/{TileMatrix}/{TileRow}/{TileCol}.png" />
 </Layer><TileMatrixSet><ows:Identifier>GoogleMapsCompatible</ows:Identifier>
 <ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>
 <ows:WellKnownScaleSet>urn:ogc:def:wkss:OGC:1.0:GoogleMapsCompatible</ows:WellKnownScaleSet>
 <TileMatrix><ows:Identifier>arbitrary-zero</ows:Identifier>
 <ScaleDenominator>559082264.0287178</ScaleDenominator>
 <TopLeftCorner>-20037508.342789244 20037508.342789244</TopLeftCorner>
 <TileWidth>256</TileWidth><TileHeight>256</TileHeight>
 <MatrixWidth>1</MatrixWidth><MatrixHeight>1</MatrixHeight></TileMatrix>
 <TileMatrix><ows:Identifier>arbitrary-one</ows:Identifier>
 <ScaleDenominator>279541132.0143589</ScaleDenominator>
 <TopLeftCorner>-20037508.342789244 20037508.342789244</TopLeftCorner>
 <TileWidth>256</TileWidth><TileHeight>256</TileHeight>
 <MatrixWidth>2</MatrixWidth><MatrixHeight>2</MatrixHeight></TileMatrix>
 </TileMatrixSet></Contents></Capabilities>"""


def test_wmts_manifest_keeps_matrix_sets_styles_and_safe_resource_template(store, limits):
    transport = FakeTransport(
        lambda _url, _etag, _modified: Response(WMTS_CAPABILITIES, "application/xml")
    )
    result = ReferenceAcquisitionPipeline(
        store, limits=limits, downloader_factory=transport
    ).acquire(
        candidate(
            "wmts",
            remote_name="ortho",
            endpoint="https://data.example.es/wmts",
            config={**TILE_CONFIG, "min_zoom": 0, "max_zoom": 1},
        )
    )

    assert result.probe.metadata["tile_matrix_sets"] == ["GoogleMapsCompatible"]
    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["style"] == "default"
    assert descriptor["tile_matrix_sets"] == ["GoogleMapsCompatible"]
    assert descriptor["selected_tile_matrix_set"] == "GoogleMapsCompatible"
    assert [
        (item["zoom"], item["identifier"])
        for item in descriptor["tile_matrix_set"]["tile_matrices"]
    ] == [(0, "arbitrary-zero"), (1, "arbitrary-one")]
    assert descriptor["tile_matrix_limits"] == [
        {
            "tile_matrix": "arbitrary-one",
            "min_tile_row": 0,
            "max_tile_row": 1,
            "min_tile_col": 0,
            "max_tile_col": 1,
        }
    ]
    assert descriptor["resource_urls"][0]["resource_type"] == "tile"


def test_wmts_accepts_empty_style_as_the_advertised_default(store, limits):
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                WMTS_CAPABILITIES,
                "application/xml",
            )
        ),
    ).acquire(
        candidate(
            "wmts",
            remote_name="ortho",
            endpoint="https://data.example.es/wmts",
            config={
                **TILE_CONFIG,
                "style_name": "",
                "min_zoom": 0,
                "max_zoom": 1,
            },
        )
    )

    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["style"] == "default"


def test_wmts_estimate_intersects_advertised_matrix_limits(store, limits):
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                WMTS_CAPABILITIES,
                "application/xml",
            )
        ),
    ).acquire(
        candidate(
            "wmts",
            remote_name="ortho",
            endpoint="https://data.example.es/wmts",
            config={
                **TILE_CONFIG,
                "bounds": {
                    "west": -180.0,
                    "south": -85.0,
                    "east": 180.0,
                    "north": 85.0,
                },
                "min_zoom": 1,
                "max_zoom": 1,
            },
        )
    )

    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["estimated_tile_count"] == 4

    limited = WMTS_CAPABILITIES.replace(
        b"<MaxTileRow>1</MaxTileRow>",
        b"<MaxTileRow>0</MaxTileRow>",
    ).replace(
        b"<MaxTileCol>1</MaxTileCol>",
        b"<MaxTileCol>0</MaxTileCol>",
    )
    limited_result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(limited, "application/xml")
        ),
    ).acquire(
        candidate(
            "wmts",
            remote_name="ortho",
            endpoint="https://data.example.es/wmts",
            config={
                **TILE_CONFIG,
                "bounds": {
                    "west": -180.0,
                    "south": -85.0,
                    "east": 180.0,
                    "north": 85.0,
                },
                "min_zoom": 1,
                "max_zoom": 1,
            },
        )
    )
    limited_descriptor = read_json_artifact(
        store,
        limited_result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert limited_descriptor["estimated_tile_count"] == 1


def test_wmts_rejects_non_webmercator_matrix_set_as_not_materializable(store, limits):
    capabilities = WMTS_CAPABILITIES.replace(
        b"urn:ogc:def:crs:EPSG::3857",
        b"urn:ogc:def:crs:EPSG::25830",
    )

    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(
                lambda _url, _etag, _modified: Response(
                    capabilities,
                    "application/xml",
                )
            ),
        ).acquire(
            candidate(
                "wmts",
                remote_name="ortho",
                endpoint="https://data.example.es/wmts",
                config={**TILE_CONFIG, "min_zoom": 0, "max_zoom": 1},
            )
        )

    assert error.value.code == "wmts_not_materializable"


WMS_CAPABILITIES = b"""<WMS_Capabilities version="1.3.0"><Capability>
 <Request><GetMap><Format>image/png</Format></GetMap></Request><Layer>
 <CRS>EPSG:3857</CRS>
 <Layer><Name>workspace:roads</Name><Style><Name>default</Name></Style></Layer>
 </Layer></Capability></WMS_Capabilities>"""


def test_wms_materializes_finite_getmap_recipe_instead_of_runtime_proxy(store, limits):
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(WMS_CAPABILITIES, "application/xml")
        ),
    ).acquire(
        candidate(
            "wms_tiles",
            remote_name="workspace:roads",
            endpoint="https://data.example.es/geoserver/wms",
            config=TILE_CONFIG,
        )
    )

    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["kvp"]["request"] == "GetMap"
    assert descriptor["kvp"]["CRS"] == "EPSG:3857"
    assert descriptor["kvp"]["transparent"] == "TRUE"
    assert descriptor["min_zoom"] == 6
    assert descriptor["max_zoom"] == 18
    assert descriptor["estimated_tile_count"] == 16_885_744
    assert descriptor["estimated_tile_count"] < descriptor["max_tile_count"]


def test_wms_accepts_empty_style_as_the_layer_default(store, limits):
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                WMS_CAPABILITIES,
                "application/xml",
            )
        ),
    ).acquire(
        candidate(
            "wms_tiles",
            remote_name="workspace:roads",
            endpoint="https://data.example.es/geoserver/wms",
            config={**TILE_CONFIG, "style_name": ""},
        )
    )

    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["style"] == ""


def test_wms_jpeg_recipe_disables_impossible_transparency(store, limits):
    capabilities = WMS_CAPABILITIES.replace(b"image/png", b"image/jpeg")
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(capabilities, "application/xml")
        ),
    ).acquire(
        candidate(
            "wms_tiles",
            remote_name="workspace:roads",
            endpoint="https://data.example.es/geoserver/wms",
            config={**TILE_CONFIG, "format": "image/jpeg"},
        )
    )

    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["kvp"]["transparent"] == "FALSE"


def test_reviewed_ortho_acquisition_requires_both_live_semantic_gates(
    store,
    limits,
):
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2020",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)
    draft = SourceCandidate(
        **definition,
        source_key="source:reviewed-ortho",
        definition_sha256="0" * 64,
    )
    source = replace(
        draft,
        definition_sha256=source_candidate_definition_sha256(draft),
    )
    package = files("app.reference_layers")
    selected = package.joinpath(IGN_CAPABILITIES_RESOURCE).read_bytes()
    catalog = package.joinpath(ITACYL_CAPABILITIES_RESOURCE).read_bytes()
    roomy_limits = replace(
        limits,
        max_probe_bytes=512 * 1024,
        max_page_bytes=512 * 1024,
        max_total_bytes=4 * 1024 * 1024,
    )

    def handler(url, _etag, _modified):
        return Response(
            catalog if "orto.wms.itacyl.es" in url else selected,
            "application/xml",
        )

    pipeline = ReferenceAcquisitionPipeline(
        store,
        limits=roomy_limits,
        downloader_factory=FakeTransport(handler),
    )
    result = pipeline.acquire(source)
    gates = [
        (
            item.metadata.get("reviewed_ortho_capabilities_gate")
            or item.metadata["probe"]["reviewed_ortho_capabilities_gate"]
        )
        for item in result.artifacts
        if item.artifact_kind == "capabilities"
    ]
    assert {gate["phase"] for gate in gates} == {
        "pre_download",
        "parity_catalog",
    }
    promotion_gate = pipeline.revalidate_reviewed_ortho_capabilities(source)
    assert promotion_gate is not None
    assert promotion_gate["selected_capabilities"]["phase"] == (
        "pre_promotion"
    )
    assert promotion_gate["catalog_capabilities"]["phase"] == (
        "parity_catalog"
    )

    changed = selected.replace(b"<Title>PNOA 2020</Title>", b"<Title>Otro</Title>")
    with pytest.raises(AcquisitionValidationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=roomy_limits,
            downloader_factory=FakeTransport(
                lambda url, _etag, _modified: Response(
                    catalog if "orto.wms.itacyl.es" in url else changed,
                    "application/xml",
                )
            ),
        ).acquire(source)
    assert error.value.code == "reviewed_ortho_capabilities_changed"


def test_wms_supertile_opt_in_requires_and_preserves_reviewed_siur_profile(
    store,
    limits,
):
    reviewed_config = {
        **TILE_CONFIG,
        "coverage_profile": "siur-castilla-y-leon-native-z16-v1",
        "wms_supertile_size": 8,
    }
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                WMS_CAPABILITIES,
                "application/xml",
            )
        ),
    ).acquire(
        candidate(
            "wms_tiles",
            remote_name="workspace:roads",
            endpoint="https://data.example.es/geoserver/wms",
            config=reviewed_config,
        )
    )
    descriptor = read_json_artifact(
        store,
        result,
        "metadata",
        metadata_kind="reference-tile-source/v1",
    )["descriptor"]
    assert descriptor["wms_supertile_size"] == 8
    assert descriptor["coverage_profile"] == reviewed_config["coverage_profile"]

    with pytest.raises(
        AcquisitionConfigurationError,
        match="reviewed SIUR coverage profile",
    ):
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(
                lambda _url, _etag, _modified: Response(
                    WMS_CAPABILITIES,
                    "application/xml",
                )
            ),
        ).acquire(
            candidate(
                "wms_tiles",
                remote_name="workspace:roads",
                endpoint="https://data.example.es/geoserver/wms",
                config={**TILE_CONFIG, "wms_supertile_size": 8},
            )
        )


def test_tile_materialization_rejects_unbounded_or_invalid_coverage(store, limits):
    invalid = {**TILE_CONFIG, "bounds": {"west": -7, "south": 40, "east": -7, "north": 43}}
    with pytest.raises(AcquisitionConfigurationError):
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(lambda *_args: None),
        ).acquire(
            candidate(
                "xyz",
                endpoint="https://tiles.example.es/{z}/{x}/{y}.png",
                config=invalid,
            )
        )


def test_tile_materialization_enforces_a_reviewed_tile_count_limit(store, limits):
    with pytest.raises(AcquisitionLimitError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=FakeTransport(lambda *_args: None),
        ).acquire(
            candidate(
                "xyz",
                endpoint="https://tiles.example.es/{z}/{x}/{y}.png",
                config={**TILE_CONFIG, "max_tile_count": 100},
            )
        )

    assert getattr(error.value, "code", None) == "tile_count_limit"


def test_model_adapter_requires_enabled_source_and_matching_running_snapshot():
    expected = candidate(
        "wfs",
        endpoint="https://data.example.es/wfs",
    )
    source = ReferenceLayerSource(
        id=7,
        provider_key="siur",
        layer_id=1,
        source_key="source:wfs",
        protocol="wfs",
        target_kind="vector",
        endpoint_url="https://data.example.es/wfs",
        remote_name="roads",
        sync_strategy="paged_snapshot",
        config_json={},
        definition_sha256=expected.definition_sha256,
        enabled=True,
        priority=10,
    )
    adapted = candidate_from_source_model(source)
    assert adapted.endpoint_url == source.endpoint_url
    assert adapted.definition_sha256 == source.definition_sha256

    source.enabled = False
    with pytest.raises(AcquisitionConfigurationError) as error:
        candidate_from_source_model(source)
    assert error.value.code == "source_disabled"


def test_effective_source_mutation_cannot_reuse_an_old_definition_digest(store, limits):
    valid = candidate("download", config={"media_type": "application/zip", "data_format": "zip"})
    tampered = replace(valid, endpoint_url="https://data.example.es/other.zip")
    transport = FakeTransport(lambda *_args: pytest.fail("tampered source was requested"))

    with pytest.raises(AcquisitionConfigurationError) as error:
        ReferenceAcquisitionPipeline(
            store,
            limits=limits,
            downloader_factory=transport,
        ).acquire(tampered)

    assert error.value.code == "source_definition_mismatch"
    assert transport.calls == []


def test_acquisition_values_fail_before_exceeding_database_metadata_bounds() -> None:
    blob = StoredReferenceBlob(
        storage_backend="filesystem",
        storage_key="blobs/sha256/aa/" + "a" * 64,
        sha256="a" * 64,
        size_bytes=1,
    )
    with pytest.raises(AcquisitionValidationError) as artifact_error:
        AcquiredArtifact(
            artifact_kind="metadata",
            role="metadata",
            media_type="application/json",
            blob=blob,
            metadata={"oversized": "x" * (4 * 1024 * 1024)},
        )
    assert artifact_error.value.code == "metadata_too_large"

    with pytest.raises(AcquisitionValidationError) as result_error:
        AcquisitionResult(
            source_key="source",
            source_definition_sha256="a" * 64,
            protocol="wfs",
            target_kind="vector",
            not_modified=False,
            artifacts=(),
            manifest_sha256=None,
            probe=None,
            observed_etag="e" * 4097,
            observed_last_modified=None,
            observed_version=None,
            feature_count=0,
            total_bytes=0,
            stats={},
        )
    assert result_error.value.code == "metadata_too_large"


def test_conditional_request_from_previous_run_formats_utc_header():
    run = ReferenceSyncRun(
        observed_etag='"v2"',
        observed_last_modified=datetime(
            2026,
            7,
            23,
            8,
            tzinfo=timezone.utc,
        ),
    )

    conditional = ConditionalRequest.from_run(
        run,
        source_url="https://data.example.es/roads.zip",
    )

    assert conditional == ConditionalRequest(
        source_url="https://data.example.es/roads.zip",
        etag='"v2"',
        last_modified="Thu, 23 Jul 2026 08:00:00 GMT",
    )


def test_persistence_links_content_addressed_artifacts_idempotently(
    db,
    store,
    limits,
):
    definition = ReferenceCatalogDefinition(
        provider_key="acquisition-test",
        source_url="https://data.example.es/catalog.json",
        raw_catalog={"version": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="downloads",
                title="Downloads",
                upstream_protocol="wms",
                base_url="https://data.example.es/wms",
                license_status="pending",
                cache_policy="mirror",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="roads",
                node_type="layer",
                title="Roads",
                service_key="downloads",
                remote_name="roads",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == definition.provider_key,
            ReferenceLayer.source_key == "roads",
        )
    )
    source = ReferenceLayerSource(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_key="source:download",
        protocol="download",
        target_kind="vector",
        endpoint_url="https://data.example.es/roads.zip",
        remote_name="roads",
        source_format="application/zip",
        sync_strategy="conditional_get",
        config_json={
            "media_type": "application/zip",
            "data_format": "shapefile-zip",
        },
        definition_sha256=candidate(
            "download",
            endpoint="https://data.example.es/roads.zip",
            config={
                "media_type": "application/zip",
                "data_format": "shapefile-zip",
            },
        ).definition_sha256,
        enabled=True,
        is_primary=True,
        priority=10,
    )
    db.add(source)
    db.flush()
    now = datetime(2026, 7, 23, 9, tzinfo=timezone.utc)
    run = ReferenceSyncRun(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        source_definition_json={"source_key": source.source_key},
        source_definition_sha256=source.definition_sha256,
        trigger_kind="manual",
        check_mode="full",
        status="running",
        attempt_no=1,
        expected_active_generation=0,
        lease_token="lease-token",
        lease_expires_at=now + timedelta(minutes=10),
        heartbeat_at=now,
        started_at=now,
        stats_json={},
    )
    db.add(run)
    db.flush()
    result = ReferenceAcquisitionPipeline(
        store,
        limits=limits,
        downloader_factory=FakeTransport(
            lambda _url, _etag, _modified: Response(
                shapefile_zip_payload(),
                "application/zip",
            )
        ),
    ).acquire(source, run=run)

    with pytest.raises(AcquisitionPersistenceError) as fenced:
        persist_acquisition_result(
            db,
            source=source,
            run=run,
            result=result,
            lease_token="stale-token",
            now=now,
        )
    assert fenced.value.code == "lease_fenced"

    first = persist_acquisition_result(
        db,
        source=source,
        run=run,
        result=result,
        lease_token="lease-token",
        now=now,
    )
    second = persist_acquisition_result(
        db,
        source=source,
        run=run,
        result=result,
        lease_token="lease-token",
        now=now,
    )

    assert [item.id for item in first] == [item.id for item in second]
    assert db.scalar(
        select(func.count()).select_from(ReferenceSourceArtifact).where(
            ReferenceSourceArtifact.source_id == source.id
        )
    ) == 2
    assert db.scalar(
        select(func.count()).select_from(ReferenceSyncRunArtifact).where(
            ReferenceSyncRunArtifact.run_id == run.id
        )
    ) == 2
    assert all(item.storage_key.endswith(item.sha256) for item in first)
