from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.source_audit import audit_current_catalog_sources
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
    candidate_definition,
)
from app.reference_layers.mirror_coverage import (
    SIUR_TILE_BOUNDS,
    SIUR_TILE_MAX_COUNT,
    SIUR_TILE_MAX_ZOOM,
    SIUR_TILE_MIN_ZOOM,
    SIUR_TILE_PROFILE,
)


def service(
    protocol: str = "wms",
    url: str = "https://idecyl.jcyl.es/geoserver/urbanismo/wms",
) -> ReferenceServiceDefinition:
    return ReferenceServiceDefinition(
        source_key="service",
        title="Service",
        upstream_protocol=protocol,
        base_url=url,
        default_format="image/png",
    )


def layer(remote_name: str = "urbanismo:plau_cyl_clasificacion"):
    return ReferenceLayerDefinition(
        source_key="layer",
        node_type="layer",
        title="Layer",
        service_key="service",
        remote_name=remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="proxy",
        bounds={"west": -7.1, "south": 40.0, "east": -1.7, "north": 43.3},
        min_zoom=6,
        max_zoom=18,
        style_name="default",
    )


def test_geoserver_wms_prefers_data_and_keeps_finite_image_fallback() -> None:
    candidates = acquisition_candidates(service(), layer())

    assert [item.protocol for item in candidates] == ["wfs", "wcs", "wms_tiles"]
    assert candidates[0].endpoint_url == (
        "https://idecyl.jcyl.es/geoserver/urbanismo/wfs"
    )
    assert candidates[1].endpoint_url.endswith("/geoserver/urbanismo/wcs")
    assert candidates[2].config == {
        "bounds": {
            "west": -7.1,
            "south": 40.0,
            "east": -1.7,
            "north": 43.3,
        },
        "min_zoom": 6,
        "max_zoom": 18,
        "format": "image/png",
        "style_name": "default",
        "coverage_required": True,
    }


def test_geoapps_workspace_has_data_candidates() -> None:
    candidates = acquisition_candidates(
        service(url="https://idecyl.jcyl.es/geoapps/topo/wms"),
        layer("ngbe_cyl"),
    )

    assert [item.endpoint_url for item in candidates[:2]] == [
        "https://idecyl.jcyl.es/geoapps/topo/wfs",
        "https://idecyl.jcyl.es/geoapps/topo/wcs",
    ]


def test_arcgis_wms_prefers_mapserver_rest() -> None:
    candidates = acquisition_candidates(
        service(
            url=(
                "https://mapas.igme.es/gis/services/Cartografia_Geologica/"
                "IGME_Geode_50/MapServer/WMSServer"
            )
        ),
        layer("1"),
    )

    assert candidates[0].protocol == "arcgis_rest"
    assert candidates[0].endpoint_url.endswith("IGME_Geode_50/MapServer")
    assert candidates[-1].protocol == "wms_tiles"


def test_inspire_wms_derives_wfs_but_still_requires_a_probe() -> None:
    candidates = acquisition_candidates(
        service(url="https://servicios.idee.es/wms-inspire/transportes"),
        layer("TN.RoadTransportNetwork.RoadLink"),
    )

    assert candidates[0].endpoint_url == (
        "https://servicios.idee.es/wfs-inspire/transportes"
    )
    assert candidates[0].config["discovery"] == "wfs_capabilities"


@pytest.mark.parametrize(
    ("protocol", "url", "expected"),
    [
        ("wmts", "https://www.ign.es/wmts/pnoa-ma", "wmts"),
        (
            "xyz",
            "https://tms-relieve.idee.es/1.0.0/relieve/{z}/{x}/{-y}.jpeg",
            "xyz",
        ),
        ("wfs", "https://example.es/geoserver/roads/wfs", "wfs"),
        (
            "arcgis_rest",
            "https://example.es/arcgis/rest/services/roads/MapServer",
            "arcgis_rest",
        ),
    ],
)
def test_native_delivery_protocols_always_have_a_local_candidate(
    protocol: str,
    url: str,
    expected: str,
) -> None:
    candidates = acquisition_candidates(service(protocol, url), layer("roads"))

    assert len(candidates) == 1
    assert candidates[0].protocol == expected


def test_siur_tile_fallback_uses_the_reviewed_finite_coverage() -> None:
    candidate = acquisition_candidates(
        service(),
        replace(
            layer(),
            source_key="layer:siur:" + "a" * 64,
            bounds=None,
            min_zoom=None,
            max_zoom=None,
        ),
    )[-1]

    assert candidate.config == {
        "bounds": SIUR_TILE_BOUNDS,
        "min_zoom": SIUR_TILE_MIN_ZOOM,
        "max_zoom": SIUR_TILE_MAX_ZOOM,
        "format": "image/png",
        "style_name": "default",
        "coverage_required": True,
        "max_tile_count": SIUR_TILE_MAX_COUNT,
        "coverage_profile": SIUR_TILE_PROFILE,
    }


def test_unknown_provider_with_missing_coverage_remains_fail_closed() -> None:
    candidate = acquisition_candidates(
        service("xyz", "https://tiles.example.es/{z}/{x}/{y}.png"),
        replace(
            layer(),
            source_key="layer:other:roads",
            bounds=None,
            min_zoom=None,
            max_zoom=None,
        ),
    )[0]

    assert candidate.config["bounds"] is None
    assert candidate.config["min_zoom"] is None
    assert candidate.config["max_zoom"] is None
    assert "coverage_profile" not in candidate.config
    assert "max_tile_count" not in candidate.config


def test_xyz_jpeg_template_uses_matching_archive_format() -> None:
    candidate = acquisition_candidates(
        replace(
            service(
                "xyz",
                "https://tms-relieve.idee.es/1.0.0/relieve/{z}/{x}/{-y}.jpeg",
            ),
            default_format=None,
        ),
        replace(
            layer(),
            source_key="layer:siur:" + "b" * 64,
            bounds=None,
            min_zoom=None,
            max_zoom=None,
            image_format=None,
        ),
    )[0]

    assert candidate.config["format"] == "image/jpeg"


def test_siur_ortho_fallback_uses_reviewed_jpeg_and_z15_profile() -> None:
    candidate = acquisition_candidates(
        replace(
            service("wms", "https://orto.wms.itacyl.es/WMS"),
            default_format=None,
        ),
        replace(
            layer("Ortofoto_2002"),
            source_key="layer:siur:" + "c" * 64,
            bounds=None,
            min_zoom=None,
            max_zoom=None,
            image_format=None,
        ),
    )[0]

    assert candidate.protocol == "wms_tiles"
    assert candidate.config["format"] == "image/jpeg"
    assert candidate.config["max_zoom"] == 15
    assert candidate.config["coverage_profile"].endswith("ortho-native-z15-v1")


def test_source_identity_changes_with_effective_definition_only() -> None:
    first = acquisition_candidates(service(), layer())[0]
    repeated = acquisition_candidates(service(), layer())[0]
    changed = acquisition_candidates(
        service(),
        replace(layer(), remote_name="urbanismo:plau_cyl_sectores"),
    )[0]

    assert first == repeated
    assert first.definition_sha256 != changed.definition_sha256
    assert first.source_key != changed.source_key
    assert set(candidate_definition(first)) == {
        "protocol",
        "target_kind",
        "endpoint_url",
        "remote_name",
        "sync_strategy",
        "priority",
        "config",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://idecyl.jcyl.es/geoserver/urbanismo/wms",
        "https://user:secret@idecyl.jcyl.es/geoserver/urbanismo/wms",
        "https://idecyl.jcyl.es:8443/geoserver/urbanismo/wms",
        "https://idecyl.jcyl.es/geoserver/urbanismo/wms#fragment",
    ],
)
def test_unsafe_catalog_endpoint_never_becomes_a_candidate(url: str) -> None:
    with pytest.raises(SourceDiscoveryError):
        acquisition_candidates(service(url=url), layer())


def test_group_or_unnamed_layer_cannot_be_marked_covered() -> None:
    with pytest.raises(SourceDiscoveryError):
        acquisition_candidates(
            service(),
            replace(layer(), node_type="group", remote_name=None),
        )


def test_current_catalog_audit_requires_a_candidate_for_every_leaf(db) -> None:
    definition = ReferenceCatalogDefinition(
        provider_key="audit",
        source_url="https://example.es/catalog.json",
        raw_catalog={"version": 1},
        services=(
            replace(
                service(),
                source_key="urbanismo",
                license_status="pending",
            ),
            service(
                "wmts",
                "https://www.ign.es/wmts/pnoa-ma",
            ),
        ),
        layers=(
            replace(
                layer(),
                source_key="planning",
                service_key="urbanismo",
                style_name=None,
            ),
            replace(
                layer("OI.OrthoimageCoverage"),
                source_key="ortho",
                service_key="service",
                role="base",
                style_name=None,
            ),
        ),
        retrieved_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    apply_catalog_definition(db, definition)

    report = audit_current_catalog_sources(db, provider_key="audit")

    assert report.candidate_coverage_complete is True
    assert report.layer_count == 2
    assert report.candidate_count == 4
    assert report.data_candidate_layer_count == 1
    assert report.tile_fallback_layer_count == 2
    assert report.protocol_counts == {
        "wcs": 1,
        "wfs": 1,
        "wms_tiles": 1,
        "wmts": 1,
    }
