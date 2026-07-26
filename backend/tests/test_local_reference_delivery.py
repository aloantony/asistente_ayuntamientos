from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.local_delivery import (
    LocalDeliveryError,
    catalog_local_delivery_availability,
    resolve_local_delivery,
)
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.mirror_lifecycle import (
    canonical_promotion_event_sha256,
)
from app.reference_layers.mirror_status import catalog_mirror_statuses
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSyncRun,
)


def _catalog_definition(
    *,
    revision: int = 1,
    remote_name: str = "source:layer",
    layer_status: str = "active",
    include_layer: bool = True,
) -> ReferenceCatalogDefinition:
    layer = ReferenceLayerDefinition(
        source_key="layer",
        node_type="layer",
        title=f"Layer v{revision}",
        service_key="service",
        remote_name=remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        queryable=True,
        styles=(
            ReferenceLayerStyleDefinition(
                source_key="default",
                remote_name="default",
                title="Default",
                is_default=True,
            ),
            ReferenceLayerStyleDefinition(
                source_key="alternate",
                remote_name="alternate",
                title="Alternate",
            ),
        ),
        style_name="default",
        status=layer_status,
    )
    return ReferenceCatalogDefinition(
        provider_key="local-delivery-test",
        source_url="https://example.test/settings.json",
        raw_catalog={"version": revision},
        services=(
            ReferenceServiceDefinition(
                source_key="service",
                title=f"Service v{revision}",
                upstream_protocol="wms",
                base_url="https://example.test/geoserver/wms",
                license_status="pending",
            ),
        ),
        layers=(layer,) if include_layer else (),
        retrieved_at=datetime(
            2026,
            7,
            21 + revision,
            tzinfo=timezone.utc,
        ),
    )


def seed_local_delivery(
    db,
    *,
    kind="vector",
    asset_metadata=None,
    asset_sha256: str = "e" * 64,
):
    definition = _catalog_definition()
    snapshot, _ = apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == definition.provider_key,
            ReferenceLayer.source_key == "layer",
        )
    )
    styles = list(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(ReferenceLayerStyle.layer_id == layer.id)
            .order_by(ReferenceLayerStyle.id)
        )
    )
    source = ReferenceLayerSource(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_key="source",
        protocol="wfs" if kind == "vector" else "wms_tiles",
        target_kind=kind,
        endpoint_url="https://example.test/source",
        remote_name="source:layer",
        sync_strategy="full_snapshot" if kind != "tiles" else "tile_seed",
        definition_sha256="a" * 64,
        enabled=True,
        is_primary=True,
    )
    db.add(source)
    db.flush()
    source_definition = {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": source.priority,
        "config": source.config_json,
    }
    source.definition_sha256 = canonical_json_sha256(source_definition)
    finished = datetime(2026, 7, 22, 10, tzinfo=timezone.utc)
    run = ReferenceSyncRun(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_id=source.id,
        source_definition_json=source_definition,
        source_definition_sha256=source.definition_sha256,
        trigger_kind="manual",
        check_mode="full",
        status="succeeded",
        started_at=finished,
        finished_at=finished,
    )
    db.add(run)
    db.flush()
    version = ReferenceDeliveryVersion(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_id=source.id,
        sync_run_id=run.id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        sequence_number=1,
        delivery_kind=kind,
        content_sha256="e" * 64,
        manifest_sha256="c" * 64,
        validation_sha256=canonical_json_sha256({"passed": True}),
        crs="EPSG:3857",
        bounds_json={"west": -7, "south": 40, "east": -1, "north": 44},
        feature_count=1 if kind == "vector" else None,
        validation_json={"passed": True},
    )
    db.add(version)
    db.flush()
    if asset_metadata is None:
        asset_metadata = (
            {
                "renderer": "geoserver",
                "layer_name": "layer_v1",
                "default_style_name": "default_v1",
                "styles": {
                    str(styles[0].id): "default_v1",
                    str(styles[1].id): "alternate_v1",
                },
                "identify_available": True,
                "legend_available": True,
            }
            if kind in {"vector", "raster"}
            else {
                "renderer": "tile_archive",
                "catalog_style_id": styles[0].id,
            }
        )
    asset = ReferenceDeliveryAsset(
        version_id=version.id,
        asset_key="primary",
        asset_kind={
            "vector": "vector_table",
            "raster": "raster_cog",
            "tiles": "tile_archive",
        }[kind],
        is_primary=True,
        storage_backend="postgres" if kind == "vector" else "filesystem",
        storage_key=(
            "reference_data.layer_v1"
            if kind == "vector"
            else "blobs/sha256/ee/" + "e" * 64
        ),
        media_type=(
            "application/x-postgis-table"
            if kind == "vector"
            else "application/vnd.mapbox-vector-tile"
        ),
        sha256=asset_sha256,
        size_bytes=None if kind == "vector" else 1024,
        metadata_json=asset_metadata,
    )
    db.add(asset)
    db.flush()
    promotion_reason = "Validated local version"
    promotion_created_at = finished
    promotion = ReferenceDeliveryPromotion(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        sequence_number=1,
        action="promote",
        to_version_id=version.id,
        run_id=run.id,
        reason=promotion_reason,
        event_sha256=canonical_promotion_event_sha256(
            provider_key=layer.provider_key,
            layer_id=layer.id,
            sequence_number=1,
            action="promote",
            from_version_id=None,
            to_version_id=version.id,
            run_id=run.id,
            actor_id=None,
            reason=promotion_reason,
            previous_event_id=None,
            previous_event_sha256=None,
            created_at=promotion_created_at,
        ),
        created_at=promotion_created_at,
    )
    db.add(promotion)
    db.flush()
    db.add(
        ReferenceLayerDeliveryState(
            provider_key=layer.provider_key,
            layer_id=layer.id,
            status="active",
            active_version_id=version.id,
            generation=1,
            last_promotion_id=promotion.id,
        )
    )
    db.commit()
    return layer, styles, source, run, version, asset


def test_resolves_versioned_geoserver_layer_and_internal_style(db) -> None:
    layer, styles, _, _, version, asset = seed_local_delivery(db)

    selection = resolve_local_delivery(
        db,
        layer=layer,
        style=styles[1],
        operation="identify",
    )

    assert selection is not None
    assert selection.backend == "geoserver"
    assert selection.version_id == version.id
    assert selection.asset_id == asset.id
    assert selection.layer_name == "layer_v1"
    assert selection.style_name == "alternate_v1"
    assert selection.identify_available is True


def test_catalog_local_availability_exposes_only_published_styles(db) -> None:
    layer, styles, _, _, _, _ = seed_local_delivery(db)

    availability = catalog_local_delivery_availability(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
        styles=styles,
    )[layer.id]

    assert availability is not None
    assert availability.delivery_available is True
    assert availability.identify_available is True
    assert availability.legend_available is True
    assert availability.available_style_ids == tuple(style.id for style in styles)


def test_changed_mutable_source_does_not_invalidate_frozen_delivery(db) -> None:
    layer, styles, source, _, _, _ = seed_local_delivery(db)
    source.definition_sha256 = "9" * 64
    db.commit()

    selection = resolve_local_delivery(
        db,
        layer=layer,
        style=styles[0],
        operation="tile",
    )

    assert selection is not None
    assert selection.version_id is not None


def test_changed_frozen_run_definition_blocks_without_remote_fallback(db) -> None:
    layer, styles, _, run, _, _ = seed_local_delivery(db)
    run.source_definition_json = {
        **run.source_definition_json,
        "endpoint_url": "https://attacker.invalid/source",
    }
    db.commit()

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )

    assert raised.value.blocker == "local_source_changed"


def test_catalog_v2_keeps_v1_servable_until_v2_is_promoted(db) -> None:
    layer, _, source, _, version, _ = seed_local_delivery(db)
    snapshot_v2, _ = apply_catalog_definition(
        db,
        _catalog_definition(
            revision=2,
            remote_name="source:layer-v2",
        ),
    )
    current_layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == layer.provider_key,
            ReferenceLayer.source_key == layer.source_key,
        )
    )
    current_styles = list(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(ReferenceLayerStyle.layer_id == layer.id)
            .order_by(ReferenceLayerStyle.id)
        )
    )
    source.endpoint_url = "https://example.test/source-v2"
    source.remote_name = "source:layer-v2"
    source.enabled = False
    source.is_primary = False
    source.definition_sha256 = canonical_json_sha256(
        {
            "protocol": source.protocol,
            "target_kind": source.target_kind,
            "endpoint_url": source.endpoint_url,
            "remote_name": source.remote_name,
            "sync_strategy": source.sync_strategy,
            "priority": source.priority,
            "config": source.config_json,
        }
    )
    db.commit()

    selection = resolve_local_delivery(
        db,
        layer=current_layer,
        style=current_styles[0],
        operation="tile",
    )
    availability = catalog_local_delivery_availability(
        db,
        provider_key=current_layer.provider_key,
        layers=[current_layer],
        styles=current_styles,
    )[current_layer.id]
    status = catalog_mirror_statuses(
        db,
        provider_key=current_layer.provider_key,
        layers=[current_layer],
        styles=current_styles,
        local_availability={current_layer.id: availability},
    )[current_layer.id]

    assert snapshot_v2.id != version.catalog_snapshot_id
    assert selection is not None
    assert selection.version_id == version.id
    assert availability is not None
    assert availability.delivery_available is True
    assert status.status == "serving_previous"


@pytest.mark.parametrize(
    ("layer_status", "include_layer"),
    (("disabled", True), ("active", False)),
)
def test_removed_or_disabled_current_layer_is_never_exposed(
    db,
    layer_status: str,
    include_layer: bool,
) -> None:
    layer, styles, _, _, _, _ = seed_local_delivery(db)
    apply_catalog_definition(
        db,
        _catalog_definition(
            revision=2,
            layer_status=layer_status,
            include_layer=include_layer,
        ),
    )
    db.refresh(layer)
    for style in styles:
        db.refresh(style)

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )
    assert raised.value.blocker == "local_disabled"

    availability = catalog_local_delivery_availability(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
        styles=styles,
    )[layer.id]
    assert availability is not None
    assert availability.delivery_available is False
    assert availability.delivery_blocker == "local_disabled"


@pytest.mark.parametrize("corruption", ("snapshot", "asset"))
def test_frozen_snapshot_or_asset_corruption_is_fail_closed(
    db,
    corruption: str,
) -> None:
    layer, styles, _, _, version, _ = seed_local_delivery(
        db,
        asset_sha256="f" * 64 if corruption == "asset" else "e" * 64,
    )
    if corruption == "snapshot":
        snapshot = db.get(
            ReferenceCatalogSnapshot,
            version.catalog_snapshot_id,
        )
        snapshot.normalized_definition_json = {
            **snapshot.normalized_definition_json,
            "unresolved_count": 999,
        }
    db.commit()

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )
    assert raised.value.blocker == "local_version_invalid"


@pytest.mark.parametrize("corruption", ["generation", "unprojected_head"])
def test_delivery_state_must_match_the_valid_promotion_head(
    db,
    corruption: str,
) -> None:
    layer, styles, _, _, version, _ = seed_local_delivery(db)
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    promotion = db.get(ReferenceDeliveryPromotion, state.last_promotion_id)
    if corruption == "generation":
        state.generation += 1
    elif corruption == "unprojected_head":
        created_at = promotion.created_at.replace(microsecond=0)
        reason = "forged head without matching delivery state"
        db.add(
            ReferenceDeliveryPromotion(
                provider_key=layer.provider_key,
                layer_id=layer.id,
                sequence_number=2,
                action="deactivate",
                from_version_id=version.id,
                to_version_id=None,
                run_id=None,
                reason=reason,
                previous_event_id=promotion.id,
                previous_event_sha256=promotion.event_sha256,
                event_sha256=canonical_promotion_event_sha256(
                    provider_key=layer.provider_key,
                    layer_id=layer.id,
                    sequence_number=2,
                    action="deactivate",
                    from_version_id=version.id,
                    to_version_id=None,
                    run_id=None,
                    actor_id=None,
                    reason=reason,
                    previous_event_id=promotion.id,
                    previous_event_sha256=promotion.event_sha256,
                    created_at=created_at,
                ),
                created_at=created_at,
            )
        )
    db.commit()

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )
    assert raised.value.blocker == "local_version_invalid"

    availability = catalog_local_delivery_availability(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
        styles=styles,
    )[layer.id]
    assert availability is not None
    assert availability.delivery_available is False
    assert availability.delivery_blocker == "local_version_invalid"


def test_invalid_internal_resource_name_is_fail_closed(db) -> None:
    layer, styles, _, _, _, _ = seed_local_delivery(
        db,
        asset_metadata={
            "renderer": "geoserver",
            "layer_name": "../../unsafe",
            "styles": {},
        },
    )

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )

    assert raised.value.blocker == "local_version_invalid"


def test_tile_archive_selects_style_specific_local_asset(db) -> None:
    layer, styles, _, _, version, primary = seed_local_delivery(db, kind="tiles")
    second = ReferenceDeliveryAsset(
        version_id=version.id,
        asset_key="alternate",
        asset_kind="tile_archive",
        is_primary=False,
        storage_backend="filesystem",
        storage_key="blobs/sha256/11/" + "1" * 64,
        media_type="application/vnd.mbtiles",
        sha256="1" * 64,
        size_bytes=2048,
        metadata_json={
            "renderer": "tile_archive",
            "catalog_style_id": styles[1].id,
        },
    )
    db.add(second)
    db.commit()

    default_selection = resolve_local_delivery(
        db,
        layer=layer,
        style=styles[0],
        operation="tile",
    )
    alternate_selection = resolve_local_delivery(
        db,
        layer=layer,
        style=styles[1],
        operation="tile",
    )

    assert default_selection is not None
    assert alternate_selection is not None
    assert default_selection.asset_id == primary.id
    assert alternate_selection.asset_id == second.id
    assert alternate_selection.backend == "tile_archive"


def test_tile_archive_never_claims_identify_or_legend(db) -> None:
    layer, styles, _, _, _, _ = seed_local_delivery(db, kind="tiles")

    for operation, blocker in (
        ("identify", "local_identify_unavailable"),
        ("legend", "local_legend_unavailable"),
    ):
        with pytest.raises(LocalDeliveryError) as raised:
            resolve_local_delivery(
                db,
                layer=layer,
                style=styles[0],
                operation=operation,
            )
        assert raised.value.blocker == blocker


def test_configured_source_without_promotion_never_falls_back_remote(db) -> None:
    layer, styles, _, _, _, _ = seed_local_delivery(db)
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    db.delete(state)
    db.commit()

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )
    assert raised.value.blocker == "local_not_ready"

    availability = catalog_local_delivery_availability(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
        styles=styles,
    )[layer.id]
    assert availability is not None
    assert availability.delivery_available is False
    assert availability.delivery_blocker == "local_not_ready"


def test_disabled_local_sources_are_authoritative(db) -> None:
    layer, styles, source, _, _, _ = seed_local_delivery(db)
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    db.delete(state)
    source.enabled = False
    db.commit()

    with pytest.raises(LocalDeliveryError) as raised:
        resolve_local_delivery(
            db,
            layer=layer,
            style=styles[0],
            operation="tile",
        )
    assert raised.value.blocker == "local_disabled"
