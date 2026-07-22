import json

import pytest

from app.reference_layers.source_discovery import SourceCandidate
from app.reference_layers.source_probes import (
    SourceProbeError,
    probe_candidate_document,
)


def candidate(protocol: str, remote_name: str) -> SourceCandidate:
    return SourceCandidate(
        protocol=protocol,
        target_kind=(
            "vector"
            if protocol in {"wfs", "arcgis_rest", "ogc_api_features"}
            else "raster"
        ),
        endpoint_url="https://example.es/service",
        remote_name=remote_name,
        sync_strategy="full_snapshot",
        priority=10,
        config={},
        source_key="source",
        definition_sha256="a" * 64,
    )


def test_wfs_probe_matches_workspace_by_unambiguous_local_name() -> None:
    document = b"""<?xml version="1.0" encoding="UTF-8"?>
    <wfs:WFS_Capabilities version="2.0.0"
      xmlns:wfs="http://www.opengis.net/wfs/2.0">
      <wfs:FeatureTypeList><wfs:FeatureType>
        <wfs:Name>urbanismo:roads</wfs:Name>
        <wfs:DefaultCRS>urn:ogc:def:crs:EPSG::25830</wfs:DefaultCRS>
      </wfs:FeatureType></wfs:FeatureTypeList>
    </wfs:WFS_Capabilities>"""

    probe = probe_candidate_document(candidate("wfs", "roads"), document)

    assert probe.available is True
    assert probe.canonical_name == "urbanismo:roads"
    assert probe.service_version == "2.0.0"
    assert probe.fingerprint_quality == "weak"
    assert probe.metadata["crs"] == ["urn:ogc:def:crs:EPSG::25830"]


def test_ambiguous_wfs_local_name_is_not_selected() -> None:
    document = b"""<WFS_Capabilities version="2.0.0">
      <FeatureTypeList>
        <FeatureType><Name>a:roads</Name></FeatureType>
        <FeatureType><Name>b:roads</Name></FeatureType>
      </FeatureTypeList>
    </WFS_Capabilities>"""

    probe = probe_candidate_document(candidate("wfs", "roads"), document)

    assert probe.available is False
    assert probe.reason == "collection is not advertised"


def test_wcs_probe_supports_v2_coverage_id() -> None:
    document = b"""<wcs:Capabilities version="2.0.1"
      xmlns:wcs="http://www.opengis.net/wcs/2.0">
      <wcs:Contents><wcs:CoverageSummary>
        <wcs:CoverageId>relieve:mdt</wcs:CoverageId>
      </wcs:CoverageSummary></wcs:Contents>
    </wcs:Capabilities>"""

    probe = probe_candidate_document(candidate("wcs", "relieve:mdt"), document)

    assert probe.available is True
    assert probe.canonical_name == "relieve:mdt"


def test_wmts_probe_requires_exact_advertised_layer() -> None:
    document = b"""<Capabilities version="1.0.0"
      xmlns="http://www.opengis.net/wmts/1.0"
      xmlns:ows="http://www.opengis.net/ows/1.1">
      <Contents><Layer><ows:Identifier>OI.OrthoimageCoverage</ows:Identifier>
      </Layer></Contents>
    </Capabilities>"""

    probe = probe_candidate_document(
        candidate("wmts", "OI.OrthoimageCoverage"),
        document,
    )

    assert probe.available is True
    assert probe.service_version == "1.0.0"


def test_wms_image_fallback_still_proves_the_layer_exists() -> None:
    document = b"""<WMS_Capabilities version="1.3.0">
      <Capability><Layer><Layer><Name>flood:Q100</Name></Layer></Layer></Capability>
    </WMS_Capabilities>"""

    probe = probe_candidate_document(candidate("wms_tiles", "flood:Q100"), document)

    assert probe.available is True
    assert probe.canonical_name == "flood:Q100"


def test_arcgis_probe_uses_layer_id_and_last_edit_fingerprint() -> None:
    document = json.dumps(
        {
            "currentVersion": 11.3,
            "capabilities": "Map,Query,Data",
            "layers": [{"id": 2, "name": "Aquifers", "type": "Feature Layer"}],
            "editingInfo": {"lastEditDate": 1784678400000},
        }
    ).encode()

    probe = probe_candidate_document(candidate("arcgis_rest", "2"), document)

    assert probe.available is True
    assert probe.canonical_name == "2"
    assert probe.fingerprint_quality == "strong"
    assert probe.metadata["last_edit_epoch_ms"] == 1784678400000


def test_arcgis_error_payload_is_not_treated_as_absent_layer() -> None:
    with pytest.raises(SourceProbeError):
        probe_candidate_document(
            candidate("arcgis_rest", "2"),
            b'{"error":{"code":499,"message":"Token Required"}}',
        )


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(SourceProbeError):
        probe_candidate_document(
            candidate("arcgis_rest", "2"),
            b'{"layers":[],"layers":[]}',
        )


def test_ogc_api_probe_parses_collection_id_and_extent() -> None:
    document = json.dumps(
        {
            "collections": [
                {
                    "id": "roads",
                    "title": "Roads",
                    "extent": {"spatial": {"bbox": [[-8, 40, -1, 44]]}},
                }
            ]
        }
    ).encode()

    probe = probe_candidate_document(
        candidate("ogc_api_features", "roads"),
        document,
    )

    assert probe.available is True
    assert probe.metadata["title"] == "Roads"


@pytest.mark.parametrize(
    "document",
    [
        b'<!DOCTYPE x [<!ENTITY file SYSTEM "file:///etc/passwd">]><x>&file;</x>',
        b"<WFS_Capabilities>\x00</WFS_Capabilities>",
        b'<?xml version="1.0" encoding="UTF-16"?><WFS_Capabilities/>',
        b"<WFS_Capabilities>",
    ],
)
def test_unsafe_or_malformed_xml_is_rejected(document: bytes) -> None:
    with pytest.raises(SourceProbeError):
        probe_candidate_document(candidate("wfs", "roads"), document)


def test_same_capability_has_a_stable_fingerprint() -> None:
    document = b"""<WFS_Capabilities version="2.0.0"><FeatureTypeList>
      <FeatureType><Name>roads</Name></FeatureType>
    </FeatureTypeList></WFS_Capabilities>"""

    first = probe_candidate_document(candidate("wfs", "roads"), document)
    second = probe_candidate_document(candidate("wfs", "roads"), document)

    assert first.fingerprint_sha256 == second.fingerprint_sha256
    assert first.fingerprint_sha256 is not None
    assert len(first.fingerprint_sha256) == 64
