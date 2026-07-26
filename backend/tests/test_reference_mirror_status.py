from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.mirror_status import catalog_mirror_statuses
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceSyncRun,
)
from test_local_reference_delivery import seed_local_delivery

NOW = datetime(2026, 7, 23, 9, tzinfo=timezone.utc)


def test_active_status_and_failed_refresh_serve_previous_version(db) -> None:
    layer, _, source, _, version, _ = seed_local_delivery(db)
    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "active"
    assert status.active_version_id == version.id

    failed_at = version.created_at + timedelta(seconds=1)
    db.add(
        ReferenceSyncRun(
            provider_key=layer.provider_key,
            layer_id=layer.id,
            source_id=source.id,
            source_definition_json={"source": "test"},
            source_definition_sha256=source.definition_sha256,
            trigger_kind="retry",
            check_mode="conditional",
            status="failed",
            expected_active_generation=1,
            started_at=failed_at,
            finished_at=failed_at,
            error_code="upstream_timeout",
            error_summary="The daily check timed out",
        )
    )
    db.commit()

    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "serving_previous"
    assert status.active_version_id == version.id
    assert status.last_error_code == "upstream_timeout"
    assert status.last_error_summary == "The daily check timed out"


def test_pending_syncing_and_disabled_statuses(db) -> None:
    layer, _, source, previous_run, _, _ = seed_local_delivery(db)
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    db.delete(state)
    db.commit()

    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "pending"

    db.add(
        ReferenceSyncRun(
            provider_key=layer.provider_key,
            layer_id=layer.id,
            source_id=source.id,
            source_definition_json={"source": "test"},
            source_definition_sha256=source.definition_sha256,
            trigger_kind="manual",
            check_mode="full",
            status="queued",
            queued_at=previous_run.queued_at + timedelta(seconds=1),
        )
    )
    db.commit()
    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "syncing"

    queued = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.source_id == source.id,
            ReferenceSyncRun.status == "queued",
        )
    )
    db.delete(queued)
    source.enabled = False
    db.commit()
    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "disabled"


def test_status_ignores_obsolete_runs_and_fails_closed_on_unservable_state(
    db,
) -> None:
    layer, _, source, active_run, version, _ = seed_local_delivery(db)
    obsolete_at = version.created_at + timedelta(seconds=1)
    db.add(
        ReferenceSyncRun(
            provider_key=layer.provider_key,
            layer_id=layer.id,
            source_id=source.id,
            source_definition_json={"obsolete": True},
            source_definition_sha256=source.definition_sha256,
            trigger_kind="retry",
            check_mode="conditional",
            status="failed",
            expected_active_generation=0,
            started_at=obsolete_at,
            finished_at=obsolete_at,
            error_code="obsolete_failure",
            error_summary="must not shadow the current generation",
        )
    )
    db.commit()

    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "active"
    assert status.last_error_code is None

    active_run.source_definition_json = {
        **active_run.source_definition_json,
        "endpoint_url": "https://tampered.invalid/source",
    }
    db.commit()
    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]
    assert status.status == "error"
    assert status.active_version_id is None
    assert status.active_generation is None


def test_group_and_unconfigured_layer_statuses_are_explicit(db) -> None:
    definition = ReferenceCatalogDefinition(
        provider_key="mirror-status-unconfigured",
        source_url="https://example.test/catalog.json",
        raw_catalog={"revision": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service",
                title="Service",
                upstream_protocol="wms",
                base_url="https://example.test/wms",
                license_status="pending",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="group",
                node_type="group",
                title="Group",
            ),
            ReferenceLayerDefinition(
                source_key="layer",
                node_type="layer",
                title="Layer",
                parent_key="group",
                service_key="service",
                remote_name="test:layer",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=NOW,
    )
    apply_catalog_definition(db, definition)
    layers = list(
        db.scalars(
            select(ReferenceLayer).where(
                ReferenceLayer.provider_key == definition.provider_key
            )
        )
    )

    statuses = catalog_mirror_statuses(
        db,
        provider_key=definition.provider_key,
        layers=layers,
    )
    by_kind = {item.node_type: statuses[item.id].status for item in layers}
    assert by_kind == {"group": "not_applicable", "layer": "legacy"}


def test_tile_delivery_status_uses_catalog_styles_for_servability(db) -> None:
    layer, _, _, _, version, _ = seed_local_delivery(db, kind="tiles")

    status = catalog_mirror_statuses(
        db,
        provider_key=layer.provider_key,
        layers=[layer],
    )[layer.id]

    assert status.status == "active"
    assert status.active_version_id == version.id
