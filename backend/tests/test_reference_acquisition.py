from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import struct
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
import zipfile

import pytest
from sqlalchemy import func, select

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
from app.reference_layers.safe_download import HTTPSDownloadResult
from app.reference_layers.source_discovery import SourceCandidate


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
 xmlns:wfs="http://www.opengis.net/wfs/2.0">
 <wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>workspace:roads</wfs:Name>
 </wfs:FeatureType></wfs:FeatureTypeList></wfs:WFS_Capabilities>"""


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


@pytest.mark.parametrize(
    "href",
    [
        "https://assets.example.net/symbol.svg",
        "symbols/symbol.svg",
        "../symbol.svg",
    ],
)
def test_style_acquisition_rejects_every_auxiliary_resource_reference(
    store,
    limits,
    href,
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
        return Response(
            sld_payload("workspace:blue", external_href=href),
            "application/xml",
        )

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

    assert error.value.code == "unsafe_sld_reference"


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
