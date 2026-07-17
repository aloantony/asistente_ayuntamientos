from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from conftest import headers_for
from sqlalchemy import func, select

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
    build_catalog_sync_plan,
)
from app.reference_layers.models import (
    OrganizationReferenceLayerSetting,
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceService,
)


def make_definition(
    *,
    layer_title: str = "Clasificación del suelo",
    include_overlay: bool = True,
    version: int = 1,
) -> ReferenceCatalogDefinition:
    layers = [
        ReferenceLayerDefinition(
            source_key="group:planning",
            node_type="group",
            title="Planeamiento urbanístico",
            sort_order=10,
        )
    ]
    if include_overlay:
        layers.append(
            ReferenceLayerDefinition(
                source_key="layer:classification",
                node_type="layer",
                title=layer_title,
                parent_key="group:planning",
                service_key="service:urbanismo-wms",
                remote_name="urbanismo:plau_cyl_clasificacion",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="proxy",
                image_format="image/png",
                supported_crs=("EPSG:25830", "EPSG:3857"),
                bounds={
                    "west": -7.1,
                    "south": 39.9,
                    "east": -1.7,
                    "north": 43.3,
                },
                sort_order=20,
                default_visible=True,
                default_opacity=Decimal("0.750"),
                queryable=True,
                downloadable=True,
                legend_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
                metadata_url="https://idecyl.jcyl.es/geonetwork/",
            )
        )
    return ReferenceCatalogDefinition(
        provider_key="siur",
        source_url=(
            "https://idecyl.jcyl.es/siur/assets/settings/settings.json"
        ),
        raw_catalog={"fixture_version": version, "layer_title": layer_title},
        services=(
            ReferenceServiceDefinition(
                source_key="service:urbanismo-wms",
                title="Urbanismo de Castilla y León",
                upstream_protocol="wms",
                base_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
                capabilities_url=(
                    "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
                    "?service=WMS&request=GetCapabilities"
                ),
                version="1.3.0",
                default_crs="EPSG:25830",
                default_format="image/png",
                attribution="Junta de Castilla y León",
                license_status="pending",
                cache_policy="on_demand",
            ),
        ),
        layers=tuple(layers),
        retrieved_at=datetime(2026, 7, 17, 12, version, tzinfo=timezone.utc),
    )


def get_overlay(db) -> ReferenceLayer:
    return db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.source_key == "layer:classification"
        )
    )


def test_sync_is_dry_run_idempotent_and_preserves_stable_layer_identity(db) -> None:
    definition = make_definition()

    plan = build_catalog_sync_plan(db, definition)

    assert plan.blocking_issues == ()
    assert plan.new_services == ("service:urbanismo-wms",)
    assert plan.new_layers == ("group:planning", "layer:classification")
    assert db.scalar(select(func.count(ReferenceCatalogSnapshot.id))) == 0

    snapshot, applied = apply_catalog_definition(db, definition)
    overlay = get_overlay(db)
    overlay_id = overlay.id

    assert snapshot.is_current is True
    assert snapshot.layer_count == 1
    assert applied.new_layers == plan.new_layers
    assert overlay.parent.source_key == "group:planning"
    assert overlay.service.source_key == "service:urbanismo-wms"

    second_plan = build_catalog_sync_plan(db, definition)
    second_snapshot, second_applied = apply_catalog_definition(db, definition)

    assert second_plan.new_services == ()
    assert second_plan.updated_services == ()
    assert second_plan.new_layers == ()
    assert second_plan.updated_layers == ()
    assert second_plan.missing_layers == ()
    assert second_plan.unchanged_count == 3
    assert second_applied.unchanged_count == 3
    assert second_snapshot.id == snapshot.id
    assert db.scalar(select(func.count(ReferenceCatalogSnapshot.id))) == 1

    renamed = make_definition(
        layer_title="Clasificación urbanística vigente",
        version=2,
    )
    renamed_plan = build_catalog_sync_plan(db, renamed)
    apply_catalog_definition(db, renamed)

    db.expire_all()
    renamed_overlay = get_overlay(db)
    assert renamed_plan.updated_layers == ("layer:classification",)
    assert renamed_overlay.id == overlay_id
    assert renamed_overlay.title == "Clasificación urbanística vigente"


def test_sync_marks_disappeared_layers_missing_without_deleting_preferences(
    db,
    make_organization,
) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    organization = make_organization()
    setting = OrganizationReferenceLayerSetting(
        organization_id=organization.id,
        layer_id=overlay.id,
        visible=False,
    )
    db.add(setting)
    db.commit()

    without_overlay = make_definition(include_overlay=False, version=3)
    plan = build_catalog_sync_plan(db, without_overlay)
    apply_catalog_definition(db, without_overlay)

    db.expire_all()
    persisted = db.get(ReferenceLayer, overlay.id)
    assert plan.missing_layers == ("layer:classification",)
    assert persisted.status == "missing"
    assert db.scalar(
        select(func.count(OrganizationReferenceLayerSetting.id)).where(
            OrganizationReferenceLayerSetting.layer_id == overlay.id
        )
    ) == 1


def test_validation_blocks_duplicates_cycles_and_unsafe_service_urls(db) -> None:
    base = make_definition()
    duplicate = replace(base, layers=base.layers + (base.layers[0],))
    duplicate_plan = build_catalog_sync_plan(db, duplicate)
    assert "Duplicate layer key: group:planning" in duplicate_plan.blocking_issues

    cycle = replace(
        base,
        layers=(
            ReferenceLayerDefinition(
                source_key="group:a",
                node_type="group",
                title="A",
                parent_key="group:b",
            ),
            ReferenceLayerDefinition(
                source_key="group:b",
                node_type="group",
                title="B",
                parent_key="group:a",
            ),
        ),
    )
    cycle_plan = build_catalog_sync_plan(db, cycle)
    assert any("cycle" in issue for issue in cycle_plan.blocking_issues)

    unsafe_service = replace(
        base.services[0],
        base_url="http://user:secret@127.0.0.1/internal",
    )
    unsafe = replace(base, services=(unsafe_service,))
    unsafe_plan = build_catalog_sync_plan(db, unsafe)
    assert "Invalid service URL: service:urbanismo-wms" in (
        unsafe_plan.blocking_issues
    )


def test_catalog_requires_map_permission_and_never_exposes_upstream_urls(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    organization = make_organization()
    viewer = make_user()

    denied = client.get(
        f"/reference-layers/catalog?organization_id={organization.id}",
        headers=headers_for(viewer),
    )
    assert denied.status_code == 403

    grant_permissions(viewer, organization, ["map.view"])
    unavailable = client.get(
        f"/reference-layers/catalog?organization_id={organization.id}",
        headers=headers_for(viewer),
    )
    assert unavailable.status_code == 503

    apply_catalog_definition(db, make_definition())
    response = client.get(
        f"/reference-layers/catalog?organization_id={organization.id}",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["provider_key"] == "siur"
    assert [layer["source_key"] for layer in body["layers"]] == [
        "group:planning",
        "layer:classification",
    ]
    overlay = body["layers"][1]
    assert overlay["effective_visible"] is True
    assert overlay["effective_opacity"] == 0.75
    assert overlay["legend_available"] is True
    assert overlay["metadata_available"] is True
    serialized = response.text
    assert "base_url" not in serialized
    assert "capabilities_url" not in serialized
    assert "options_json" not in serialized
    assert "idecyl.jcyl.es" not in serialized


def test_organization_settings_require_manage_and_restore_catalog_defaults(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    first_org = make_organization("First council")
    second_org = make_organization("Second council")
    viewer = make_user()
    manager = make_user()
    grant_permissions(viewer, first_org, ["map.view"])
    grant_permissions(manager, first_org, ["map.manage"])

    path = (
        f"/organizations/{first_org.id}/reference-layers/"
        f"{overlay.id}/settings"
    )
    denied = client.put(
        path,
        headers=headers_for(viewer),
        json={"visible": False},
    )
    assert denied.status_code == 403

    updated = client.put(
        path,
        headers=headers_for(manager),
        json={"visible": False, "opacity": 0.25},
    )
    assert updated.status_code == 200
    assert updated.json()["updated_by_id"] == manager.id

    catalog = client.get(
        f"/reference-layers/catalog?organization_id={first_org.id}",
        headers=headers_for(manager),
    ).json()
    effective = next(
        item
        for item in catalog["layers"]
        if item["source_key"] == "layer:classification"
    )
    assert effective["effective_visible"] is False
    assert effective["effective_opacity"] == 0.25

    cross_org = client.put(
        f"/organizations/{second_org.id}/reference-layers/"
        f"{overlay.id}/settings",
        headers=headers_for(manager),
        json={"visible": False},
    )
    assert cross_org.status_code == 403

    deleted = client.delete(path, headers=headers_for(manager))
    assert deleted.status_code == 204
    reset_catalog = client.get(
        f"/reference-layers/catalog?organization_id={first_org.id}",
        headers=headers_for(manager),
    ).json()
    reset = next(
        item
        for item in reset_catalog["layers"]
        if item["source_key"] == "layer:classification"
    )
    assert reset["effective_visible"] is True
    assert reset["effective_opacity"] == 0.75


def test_superuser_can_manage_reference_settings_for_any_organization(
    client,
    db,
    superuser,
    make_organization,
) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    organization = make_organization()

    response = client.put(
        f"/organizations/{organization.id}/reference-layers/"
        f"{overlay.id}/settings",
        headers=headers_for(superuser),
        json={"opacity": 0.5},
    )

    assert response.status_code == 200
    assert response.json()["opacity"] == 0.5
    assert db.scalar(select(func.count(ReferenceService.id))) == 1
