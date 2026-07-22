from dataclasses import replace
from datetime import datetime, timezone

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
from app.reference_layers.source_audit import (
    _layer_definition,
    audit_current_catalog_sources,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
    candidate_definition,
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
