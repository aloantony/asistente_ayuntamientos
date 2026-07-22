from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceDeliveryVersionArtifact,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)


def _seed_catalog(db) -> ReferenceLayer:
    definition = ReferenceCatalogDefinition(
        provider_key="mirror-schema-test",
        source_url="https://example.test/settings.json",
        raw_catalog={"version": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service:wms",
                title="Mirror schema WMS",
                upstream_protocol="wms",
                base_url="https://example.test/geoserver/wms",
                license_status="pending",
                cache_policy="mirror",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:mirror",
                node_type="layer",
                title="Mirror schema layer",
                service_key="service:wms",
                remote_name="test:layer",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    apply_catalog_definition(db, definition)
    return db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == definition.provider_key,
            ReferenceLayer.source_key == "layer:mirror",
        )
    )


def _seed_source(db, layer: ReferenceLayer) -> ReferenceLayerSource:
    source = ReferenceLayerSource(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_key="source:wfs",
        protocol="wfs",
        target_kind="vector",
        endpoint_url="https://example.test/geoserver/wfs",
        remote_name="test:layer",
        source_format="application/json",
        sync_strategy="paged_snapshot",
        config_json={"page_size": 1000},
        definition_sha256="a" * 64,
        enabled=True,
        is_primary=True,
    )
    db.add(source)
    db.commit()
    return source


def _finished_run(db, source: ReferenceLayerSource, number: int) -> ReferenceSyncRun:
    finished_at = datetime(2026, 7, 22, 10, number, tzinfo=timezone.utc)
    run = ReferenceSyncRun(
        source_id=source.id,
        source_definition_json={"revision": number},
        source_definition_sha256=f"{number:x}" * 64,
        trigger_kind="manual",
        check_mode="full",
        status="succeeded",
        attempt_no=1,
        expected_active_generation=number - 1,
        started_at=finished_at,
        finished_at=finished_at,
        stats_json={"features": number},
    )
    db.add(run)
    db.flush()
    return run


def _seed_mirror_graph(db):
    layer = _seed_catalog(db)
    source = _seed_source(db, layer)
    snapshot = layer.last_seen_snapshot
    artifact = ReferenceSourceArtifact(
        source_id=source.id,
        artifact_kind="dataset",
        source_url="https://example.test/data.zip",
        final_url="https://example.test/data.zip",
        source_version="2026-07-22",
        media_type="application/zip",
        storage_backend="filesystem",
        storage_key="reference/sha256/bb/data.zip",
        size_bytes=1024,
        sha256="b" * 64,
        metadata_json={},
        retrieved_at=datetime(2026, 7, 22, 10, tzinfo=timezone.utc),
    )
    db.add(artifact)
    db.flush()

    runs = []
    versions = []
    assets = []
    for number in (1, 2):
        run = _finished_run(db, source, number)
        db.add(
            ReferenceSyncRunArtifact(
                source_id=source.id,
                run_id=run.id,
                artifact_id=artifact.id,
                role="input",
            )
        )
        version = ReferenceDeliveryVersion(
            provider_key=layer.provider_key,
            layer_id=layer.id,
            source_id=source.id,
            sync_run_id=run.id,
            catalog_snapshot_id=snapshot.id,
            catalog_definition_sha256=snapshot.definition_sha256,
            sequence_number=number,
            delivery_kind="vector",
            source_version=f"2026-07-2{number}",
            content_sha256=f"{number + 1:x}" * 64,
            manifest_sha256=f"{number + 3:x}" * 64,
            validation_sha256=f"{number + 5:x}" * 64,
            reference_at=datetime(
                2026,
                7,
                20 + number,
                tzinfo=timezone.utc,
            ),
            crs="EPSG:3857",
            bounds_json={
                "west": -7,
                "south": 40,
                "east": -1,
                "north": 44,
            },
            feature_count=number,
            validation_json={"passed": True},
        )
        db.add(version)
        db.flush()
        db.add(
            ReferenceDeliveryVersionArtifact(
                source_id=source.id,
                version_id=version.id,
                artifact_id=artifact.id,
                role="input",
            )
        )
        asset = ReferenceDeliveryAsset(
            version_id=version.id,
            asset_key="primary",
            asset_kind="vector_table",
            is_primary=True,
            storage_backend="postgres",
            storage_key=f"reference_data.layer_{layer.id}_v{number}",
            media_type="application/x-postgis-table",
            sha256=f"{number + 7:x}" * 64,
            metadata_json={"table_version": number},
        )
        db.add(asset)
        db.flush()
        runs.append(run)
        versions.append(version)
        assets.append(asset)

    first_promotion = ReferenceDeliveryPromotion(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        sequence_number=1,
        action="promote",
        to_version_id=versions[0].id,
        run_id=runs[0].id,
        reason="Initial validated version",
        event_sha256="c" * 64,
    )
    db.add(first_promotion)
    db.flush()
    second_promotion = ReferenceDeliveryPromotion(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        sequence_number=2,
        action="promote",
        from_version_id=versions[0].id,
        to_version_id=versions[1].id,
        run_id=runs[1].id,
        reason="New validated version",
        previous_event_id=first_promotion.id,
        previous_event_sha256=first_promotion.event_sha256,
        event_sha256="d" * 64,
    )
    db.add(second_promotion)
    db.flush()
    rollback = ReferenceDeliveryPromotion(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        sequence_number=3,
        action="rollback",
        from_version_id=versions[1].id,
        to_version_id=versions[0].id,
        reason="Second version failed smoke checks",
        previous_event_id=second_promotion.id,
        previous_event_sha256=second_promotion.event_sha256,
        event_sha256="e" * 64,
    )
    db.add(rollback)
    db.flush()
    state = ReferenceLayerDeliveryState(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        status="active",
        active_version_id=versions[0].id,
        generation=3,
        last_promotion_id=rollback.id,
    )
    db.add(state)
    db.commit()
    return layer, source, artifact, runs, versions, assets, rollback, state


def test_versioned_mirror_graph_supports_append_only_rollback(db) -> None:
    layer, source, artifact, runs, versions, assets, rollback, state = (
        _seed_mirror_graph(db)
    )

    assert source.layer_id == layer.id
    assert artifact.source_id == source.id
    assert [version.sequence_number for version in versions] == [1, 2]
    assert all(asset.is_primary for asset in assets)
    assert rollback.action == "rollback"
    assert rollback.from_version_id == versions[1].id
    assert rollback.to_version_id == versions[0].id
    assert state.active_version_id == versions[0].id
    assert state.generation == 3
    assert [run.status for run in runs] == ["succeeded", "succeeded"]


def test_mirror_append_only_records_reject_updates_and_deletes(db) -> None:
    _, _, artifact, runs, versions, assets, rollback, _ = _seed_mirror_graph(db)
    statements = (
        update(ReferenceSourceArtifact)
        .where(ReferenceSourceArtifact.id == artifact.id)
        .values(media_type="text/plain"),
        delete(ReferenceSourceArtifact).where(
            ReferenceSourceArtifact.id == artifact.id
        ),
        update(ReferenceSyncRunArtifact)
        .where(ReferenceSyncRunArtifact.run_id == runs[0].id)
        .values(role="metadata"),
        delete(ReferenceSyncRunArtifact).where(
            ReferenceSyncRunArtifact.run_id == runs[0].id
        ),
        update(ReferenceDeliveryVersion)
        .where(ReferenceDeliveryVersion.id == versions[0].id)
        .values(crs="EPSG:4326"),
        delete(ReferenceDeliveryVersion).where(
            ReferenceDeliveryVersion.id == versions[0].id
        ),
        update(ReferenceDeliveryVersionArtifact)
        .where(
            ReferenceDeliveryVersionArtifact.version_id == versions[0].id
        )
        .values(role="metadata"),
        delete(ReferenceDeliveryVersionArtifact).where(
            ReferenceDeliveryVersionArtifact.version_id == versions[0].id
        ),
        update(ReferenceDeliveryAsset)
        .where(ReferenceDeliveryAsset.id == assets[0].id)
        .values(asset_key="tampered"),
        delete(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.id == assets[0].id
        ),
        update(ReferenceDeliveryPromotion)
        .where(ReferenceDeliveryPromotion.id == rollback.id)
        .values(reason="tampered"),
        delete(ReferenceDeliveryPromotion).where(
            ReferenceDeliveryPromotion.id == rollback.id
        ),
    )
    for statement in statements:
        with pytest.raises(DBAPIError, match="immutable"):
            with db.begin_nested():
                db.execute(statement)


def test_sync_run_constraints_allow_only_one_open_run_per_source(db) -> None:
    source = _seed_source(db, _seed_catalog(db))
    db.add(
        ReferenceSyncRun(
            source_id=source.id,
            source_definition_json={},
            source_definition_sha256="f" * 64,
            trigger_kind="scheduled",
            check_mode="conditional",
            status="queued",
        )
    )
    db.commit()

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(
                ReferenceSyncRun(
                    source_id=source.id,
                    source_definition_json={},
                    source_definition_sha256="1" * 64,
                    trigger_kind="manual",
                    check_mode="full",
                    status="queued",
                )
            )
            db.flush()

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(
                ReferenceSyncRun(
                    source_id=source.id,
                    source_definition_json={},
                    source_definition_sha256="2" * 64,
                    trigger_kind="manual",
                    check_mode="full",
                    status="running",
                )
            )
            db.flush()


def test_content_addressed_blob_can_be_shared_by_multiple_sources(db) -> None:
    layer = _seed_catalog(db)
    first_source = _seed_source(db, layer)
    second_source = ReferenceLayerSource(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_key="source:wms-fallback",
        protocol="wms_tiles",
        target_kind="tiles",
        endpoint_url="https://example.test/geoserver/wms",
        remote_name="test:layer",
        source_format="image/png",
        sync_strategy="tile_seed",
        config_json={"max_zoom": 12},
        definition_sha256="9" * 64,
        enabled=True,
        is_primary=False,
    )
    db.add(second_source)
    db.flush()

    shared = {
        "artifact_kind": "capabilities",
        "source_url": "https://example.test/geoserver/ows",
        "final_url": "https://example.test/geoserver/ows",
        "media_type": "application/xml",
        "storage_backend": "filesystem",
        "storage_key": f"blobs/sha256/{'a' * 2}/{'a' * 64}",
        "size_bytes": 128,
        "sha256": "a" * 64,
        "metadata_json": {},
        "retrieved_at": datetime(2026, 7, 23, tzinfo=timezone.utc),
    }
    db.add_all(
        [
            ReferenceSourceArtifact(source_id=first_source.id, **shared),
            ReferenceSourceArtifact(source_id=second_source.id, **shared),
        ]
    )
    db.commit()

    artifacts = list(
        db.scalars(
            select(ReferenceSourceArtifact).where(
                ReferenceSourceArtifact.storage_key == shared["storage_key"]
            )
        )
    )
    assert {artifact.source_id for artifact in artifacts} == {
        first_source.id,
        second_source.id,
    }
