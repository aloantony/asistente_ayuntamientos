from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.models import ReferenceLayer
from app.reference_layers.mirror_lifecycle import build_mirror_bootstrap_plan
from app.reference_layers.source_audit import (
    _layer_definition,
    audit_current_catalog_sources,
)
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
    SIUR_WMS_SUPERTILE_SIZE,
)
from app.reference_layers.siur_settings import (
    SiurSettingsBaseline,
    analyze_siur_settings,
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


def test_geoserver_data_candidates_freeze_serviceable_catalog_style_identities() -> None:
    styled_layer = replace(
        layer(),
        styles=(
            ReferenceLayerStyleDefinition(
                source_key="style:zoning",
                title="Zoning",
                remote_name="Urbanismo:Zoning",
                status="active",
            ),
            ReferenceLayerStyleDefinition(
                source_key="style:degraded",
                title="Degraded but serviceable",
                remote_name="Urbanismo:Degraded",
                status="degraded",
            ),
            ReferenceLayerStyleDefinition(
                source_key="style:disabled",
                title="Disabled",
                remote_name="Urbanismo:Disabled",
                status="disabled",
            ),
            ReferenceLayerStyleDefinition(
                source_key="style:boundaries",
                title="Boundaries",
                remote_name="Urbanismo:Boundaries",
                status="active",
            ),
        ),
    )

    candidates = acquisition_candidates(service(), styled_layer)

    expected = {
        "style_endpoint_url": (
            "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
        ),
        "style_layer_name": "urbanismo:plau_cyl_clasificacion",
        "styles": [
            {
                "catalog_style_source_key": "style:boundaries",
                "remote_name": "Urbanismo:Boundaries",
            },
            {
                "catalog_style_source_key": "style:degraded",
                "remote_name": "Urbanismo:Degraded",
            },
            {
                "catalog_style_source_key": "style:zoning",
                "remote_name": "Urbanismo:Zoning",
            },
        ],
    }
    assert candidates[0].config == {
        "discovery": "wfs_capabilities",
        **expected,
    }
    assert candidates[1].config == {
        "discovery": "wcs_capabilities",
        **expected,
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


@pytest.mark.parametrize(
    (
        "catalog_endpoint",
        "catalog_remote_name",
        "wms_endpoint",
        "wms_remote_name",
        "image_format",
        "profile",
    ),
    [
        (
            "https://www.ign.es/wmts/pnoa-ma",
            "OI.OrthoimageCoverage",
            "https://www.ign.es/wms-inspire/pnoa-ma",
            "OI.OrthoimageCoverage",
            "image/jpeg",
            "ign-pnoa-current-ortho-wms-v1",
        ),
        (
            "https://www.ign.es/wmts/ign-base",
            "IGNBaseTodo-nofondo",
            "https://www.ign.es/wms-inspire/ign-base",
            "IGNBaseTodo-nofondo",
            "image/png",
            "ign-base-transparent-wms-v1",
        ),
        (
            "https://www.ign.es/wmts/mapa-raster",
            "MTN",
            "https://www.ign.es/wms-inspire/mapa-raster",
            "mtn_rasterizado",
            "image/jpeg",
            "ign-mtn-raster-wms-v1",
        ),
    ],
)
def test_reviewed_siur_native_maps_prefer_exact_official_wms_with_wmts_fallback(
    catalog_endpoint,
    catalog_remote_name,
    wms_endpoint,
    wms_remote_name,
    image_format,
    profile,
) -> None:
    catalog_service = replace(
        service("wmts", catalog_endpoint + "/"),
        default_format=None,
    )
    catalog_layer = replace(
        layer(catalog_remote_name),
        source_key="layer:siur:" + "d" * 64,
        role="base",
        bounds=None,
        min_zoom=None,
        max_zoom=None,
        style_name=None,
        image_format=None,
    )

    candidates = acquisition_candidates(catalog_service, catalog_layer)

    assert [item.protocol for item in candidates] == ["wms_tiles", "wmts"]
    assert [item.priority for item in candidates] == [40, 50]
    preferred, fallback = candidates
    assert preferred.endpoint_url == wms_endpoint
    assert preferred.remote_name == wms_remote_name
    assert preferred.config["format"] == image_format
    assert preferred.config["coverage_profile"] == SIUR_TILE_PROFILE
    assert preferred.config["wms_supertile_size"] == SIUR_WMS_SUPERTILE_SIZE
    assert preferred.config["reviewed_equivalence"] == {
        "schema": "siur-reviewed-native-wms-equivalence/v1",
        "profile": profile,
        "catalog_protocol": "wmts",
        "catalog_endpoint_url": catalog_endpoint,
        "catalog_remote_name": catalog_remote_name,
        "selected_protocol": "wms_tiles",
        "selected_endpoint_url": wms_endpoint,
        "selected_remote_name": wms_remote_name,
        "image_format": image_format,
        "coverage_profile": SIUR_TILE_PROFILE,
        "wms_supertile_size": SIUR_WMS_SUPERTILE_SIZE,
    }
    assert candidate_definition(preferred)["config"]["reviewed_equivalence"] == (
        preferred.config["reviewed_equivalence"]
    )
    assert fallback.endpoint_url == catalog_endpoint + "/"
    assert fallback.remote_name == catalog_remote_name
    assert fallback.config["coverage_profile"] == SIUR_TILE_PROFILE
    assert "wms_supertile_size" not in fallback.config
    assert acquisition_candidates(catalog_service, catalog_layer) == candidates


@pytest.mark.parametrize(
    ("source_key", "role", "endpoint", "remote_name"),
    [
        (
            "layer:other:ortho",
            "base",
            "https://www.ign.es/wmts/pnoa-ma",
            "OI.OrthoimageCoverage",
        ),
        (
            "layer:siur:" + "e" * 64,
            "overlay",
            "https://www.ign.es/wmts/pnoa-ma",
            "OI.OrthoimageCoverage",
        ),
        (
            "layer:siur:" + "f" * 64,
            "base",
            "https://www.ign.es/wmts/pnoa-ma-copy",
            "OI.OrthoimageCoverage",
        ),
        (
            "layer:siur:" + "0" * 64,
            "base",
            "https://www.ign.es/wmts/pnoa-ma",
            "SimilarOrthoLayer",
        ),
        (
            "layer:siur:" + "1" * 64,
            "base",
            "https://www.ign.es/wmts/pnoa-ma?variant=other",
            "OI.OrthoimageCoverage",
        ),
    ],
)
def test_reviewed_native_wms_equivalence_never_leaks_to_similar_sources(
    source_key,
    role,
    endpoint,
    remote_name,
) -> None:
    candidates = acquisition_candidates(
        replace(service("wmts", endpoint), default_format=None),
        replace(
            layer(remote_name),
            source_key=source_key,
            role=role,
            bounds=None,
            min_zoom=None,
            max_zoom=None,
        ),
    )

    assert [item.protocol for item in candidates] == ["wmts"]
    assert "reviewed_equivalence" not in candidates[0].config


def test_real_native_fixture_selects_reviewed_wms_before_its_wmts_fallback() -> None:
    document = (
        Path(__file__).parent / "fixtures" / "siur_settings_native.json"
    ).read_bytes()
    probe = analyze_siur_settings(document)
    baseline = SiurSettingsBaseline(
        top_level_groups=probe.top_level_group_count,
        groups=probe.group_count,
        layers=probe.layer_count,
        services=len(probe.services),
        raw_sha256=probe.raw_sha256,
        layer_keys=frozenset(
            node.source_key
            for node in probe.nodes
            if node.kind == "layer" and node.source_key is not None
        ),
    )
    definition = analyze_siur_settings(
        document,
        baseline=baseline,
    ).require_definition()
    services = {item.source_key: item for item in definition.services}
    native_maps = {
        item.remote_name: item
        for item in definition.layers
        if item.node_type == "layer" and item.role == "base"
    }

    for remote_name in ("OI.OrthoimageCoverage", "IGNBaseTodo-nofondo"):
        catalog_layer = native_maps[remote_name]
        candidates = acquisition_candidates(
            services[catalog_layer.service_key],
            catalog_layer,
        )
        assert [item.protocol for item in candidates] == ["wms_tiles", "wmts"]
        assert candidates[0].priority < candidates[1].priority
        assert candidates[0].config["wms_supertile_size"] == 8


def test_mirror_bootstrap_makes_reviewed_wms_primary_and_keeps_wmts_fallback(
    db,
) -> None:
    catalog_service = replace(
        service("wmts", "https://www.ign.es/wmts/pnoa-ma"),
        source_key="pnoa",
        default_format=None,
    )
    catalog_layer = replace(
        layer("OI.OrthoimageCoverage"),
        source_key="layer:siur:" + "2" * 64,
        service_key="pnoa",
        role="base",
        bounds=None,
        min_zoom=None,
        max_zoom=None,
        style_name=None,
    )
    definition = ReferenceCatalogDefinition(
        provider_key="siur",
        source_url="https://example.es/siur-settings.json",
        raw_catalog={"version": 1},
        services=(catalog_service,),
        layers=(catalog_layer,),
        retrieved_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    apply_catalog_definition(db, definition)

    plan = build_mirror_bootstrap_plan(db, provider_key="siur")

    assert len(plan.sources) == 2
    preferred, fallback = plan.sources
    assert preferred.protocol == "wms_tiles"
    assert preferred.endpoint_url == "https://www.ign.es/wms-inspire/pnoa-ma"
    assert preferred.remote_name == "OI.OrthoimageCoverage"
    assert preferred.priority == 40
    assert preferred.is_primary is True
    assert preferred.config_json["reviewed_equivalence"]["profile"] == (
        "ign-pnoa-current-ortho-wms-v1"
    )
    assert fallback.protocol == "wmts"
    assert fallback.endpoint_url == "https://www.ign.es/wmts/pnoa-ma"
    assert fallback.priority == 50
    assert fallback.is_primary is False


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
        "wms_supertile_size": SIUR_WMS_SUPERTILE_SIZE,
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
    assert "wms_supertile_size" not in candidate.config


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


def test_source_audit_reconstructs_persisted_layer_styles(db) -> None:
    definition = ReferenceCatalogDefinition(
        provider_key="audit-styles",
        source_url="https://example.es/catalog.json",
        raw_catalog={"version": 1},
        services=(
            replace(
                service(),
                source_key="urbanismo",
                license_status="pending",
            ),
        ),
        layers=(
            replace(
                layer(),
                source_key="planning",
                service_key="urbanismo",
                style_name="style:default",
                styles=(
                    ReferenceLayerStyleDefinition(
                        source_key="style:default",
                        title="Default",
                        remote_name="Urbanismo:Default",
                        is_default=True,
                    ),
                ),
            ),
        ),
        retrieved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    apply_catalog_definition(db, definition)
    record = db.scalar(
        select(ReferenceLayer)
        .options(selectinload(ReferenceLayer.styles))
        .where(
            ReferenceLayer.provider_key == "audit-styles",
            ReferenceLayer.source_key == "planning",
        )
    )

    rebuilt = _layer_definition(record)

    assert rebuilt.styles == definition.layers[0].styles
    candidates = acquisition_candidates(definition.services[0], rebuilt)
    assert candidates[0].config["styles"] == [
        {
            "catalog_style_source_key": "style:default",
            "remote_name": "Urbanismo:Default",
        }
    ]
