from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Barrier
import time

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.mirror_lifecycle import (
    MirrorLeaseLostError,
    MirrorLifecycleError,
    MirrorPlanChangedError,
    MirrorPromotionConflict,
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
    claim_next_sync_run,
    deactivate_delivery,
    enqueue_due_sources,
    enqueue_fallback_source,
    finish_sync_run,
    heartbeat_sync_run,
    promote_delivery_version,
    reactivate_delivery,
    rollback_delivery_version,
    stored_promotion_hash_is_valid,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerMirrorStrategy,
    ReferenceLayerMirrorStrategyDependency,
    ReferenceLayerSource,
    ReferenceService,
    ReferenceSyncRun,
)

NOW = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def committed_reference_providers(engine):
    """Remove rows committed by concurrency tests using independent sessions."""

    provider_keys: list[str] = []
    yield provider_keys
    if not provider_keys:
        return
    with Session(engine) as cleanup:
        source_ids = select(ReferenceLayerSource.id).where(
            ReferenceLayerSource.provider_key.in_(provider_keys)
        )
        cleanup.execute(
            delete(ReferenceSyncRun).where(
                ReferenceSyncRun.source_id.in_(source_ids)
            )
        )
        strategy_ids = select(ReferenceLayerMirrorStrategy.id).where(
            ReferenceLayerMirrorStrategy.provider_key.in_(provider_keys)
        )
        cleanup.execute(
            delete(ReferenceLayerMirrorStrategyDependency).where(
                ReferenceLayerMirrorStrategyDependency.strategy_id.in_(strategy_ids)
            )
        )
        cleanup.execute(
            delete(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceLayer).where(
                ReferenceLayer.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceService).where(
                ReferenceService.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceCatalogSnapshot).where(
                ReferenceCatalogSnapshot.provider_key.in_(provider_keys)
            )
        )
        cleanup.commit()


def _definition(
    *,
    provider_key: str = "mirror-lifecycle-test",
    remote_name: str = "planning:zones",
) -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://example.test/catalog.json",
        raw_catalog={"revision": remote_name},
        services=(
            ReferenceServiceDefinition(
                source_key="service:planning",
                title="Planning service",
                upstream_protocol="wms",
                base_url="https://example.test/geoserver/planning/wms",
                default_format="image/png",
                license_status="approved",
                cache_policy="mirror",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:zones",
                node_type="layer",
                title="Planning zones",
                service_key="service:planning",
                remote_name=remote_name,
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                image_format="image/png",
                bounds={
                    "west": -7.1,
                    "south": 40.0,
                    "east": -1.7,
                    "north": 43.3,
                },
                min_zoom=6,
                max_zoom=18,
            ),
        ),
        retrieved_at=NOW,
    )


def _seed_bootstrap(db, *, provider_key: str = "mirror-lifecycle-test"):
    definition = _definition(provider_key=provider_key)
    apply_catalog_definition(db, definition)
    plan = build_mirror_bootstrap_plan(db, provider_key=provider_key)
    applied = apply_mirror_bootstrap_plan(db, plan)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.source_key == "layer:zones",
        )
    )
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )
    sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.provider_key == provider_key)
            .order_by(ReferenceLayerSource.priority)
        )
    )
    return definition, layer, snapshot, sources, applied


def _only_source_due(db, source, *, at: datetime = NOW):
    for item in db.scalars(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.provider_key == source.provider_key
        )
    ):
        item.enabled = item.id == source.id
        item.is_primary = False
        item.next_check_at = at - timedelta(seconds=1)
    db.flush()
    source.is_primary = True
    db.commit()


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _create_version(db, *, source, snapshot, lease, sequence_number: int):
    validation = {
        "passed": True,
        "checks": ["bounds", "schema", "primary_asset"],
    }
    version = ReferenceDeliveryVersion(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        sync_run_id=lease.run_id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        sequence_number=sequence_number,
        delivery_kind=source.target_kind,
        source_version=f"2026-07-{20 + sequence_number}",
        content_sha256=f"{sequence_number + 1:x}" * 64,
        manifest_sha256=f"{sequence_number + 3:x}" * 64,
        validation_sha256=_canonical_sha256(validation),
        reference_at=NOW,
        crs="EPSG:3857",
        bounds_json={
            "west": -7.1,
            "south": 40.0,
            "east": -1.7,
            "north": 43.3,
        },
        feature_count=10 * sequence_number,
        validation_json=validation,
        created_at=NOW + timedelta(minutes=sequence_number),
    )
    db.add(version)
    db.flush()
    asset_kind = {
        "vector": "vector_table",
        "raster": "raster_cog",
        "tiles": "tile_archive",
    }[source.target_kind]
    storage_backend = "postgres" if source.target_kind == "vector" else "filesystem"
    db.add(
        ReferenceDeliveryAsset(
            version_id=version.id,
            asset_key="primary",
            asset_kind=asset_kind,
            is_primary=True,
            storage_backend=storage_backend,
            storage_key=f"mirror/{source.id}/v{sequence_number}",
            media_type="application/octet-stream",
            sha256=version.content_sha256,
            size_bytes=None if storage_backend == "postgres" else 1024,
            metadata_json={},
        )
    )
    db.commit()
    return version


def _promote_cross_snapshot_versions(db):
    _, layer, snapshot_v1, sources, _ = _seed_bootstrap(db)
    source_v1 = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source_v1)
    enqueue_due_sources(db, now=NOW)
    lease_v1 = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "1" * 64,
    )
    version_v1 = _create_version(
        db,
        source=source_v1,
        snapshot=snapshot_v1,
        lease=lease_v1,
        sequence_number=1,
    )
    promote_delivery_version(
        db,
        version_id=version_v1.id,
        lease=lease_v1,
        expected_generation=0,
        reason="catalog v1",
        now=NOW + timedelta(seconds=1),
    )

    snapshot_v2, _ = apply_catalog_definition(
        db,
        _definition(remote_name="planning:zones-v2"),
    )
    apply_mirror_bootstrap_plan(
        db,
        build_mirror_bootstrap_plan(
            db,
            provider_key=layer.provider_key,
        ),
    )
    source_v2 = db.scalar(
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.provider_key == layer.provider_key,
            ReferenceLayerSource.target_kind == "vector",
            ReferenceLayerSource.enabled.is_(True),
        )
        .order_by(
            ReferenceLayerSource.is_primary.desc(),
            ReferenceLayerSource.priority,
            ReferenceLayerSource.id,
        )
    )
    assert source_v2 is not None
    _only_source_due(
        db,
        source_v2,
        at=NOW + timedelta(seconds=2),
    )
    enqueue_due_sources(db, now=NOW + timedelta(seconds=2))
    lease_v2 = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "2" * 64,
    )
    version_v2 = _create_version(
        db,
        source=source_v2,
        snapshot=snapshot_v2,
        lease=lease_v2,
        sequence_number=2,
    )
    promote_delivery_version(
        db,
        version_id=version_v2.id,
        lease=lease_v2,
        expected_generation=1,
        reason="catalog v2",
        now=NOW + timedelta(seconds=3),
    )
    return (
        layer,
        snapshot_v1,
        snapshot_v2,
        version_v1,
        version_v2,
    )


def test_bootstrap_is_dry_run_idempotent_and_preserves_manual_sources(db) -> None:
    definition = _definition()
    apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == definition.provider_key,
            ReferenceLayer.source_key == "layer:zones",
        )
    )
    manual = ReferenceLayerSource(
        provider_key=definition.provider_key,
        layer_id=layer.id,
        source_key="operator:manual",
        protocol="local",
        target_kind="vector",
        endpoint_url=None,
        remote_name="zones",
        sync_strategy="manual",
        config_json={},
        definition_sha256="a" * 64,
        enabled=False,
        is_primary=False,
        priority=1,
        next_check_at=NOW,
    )
    db.add(manual)
    db.commit()

    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )

    assert len(plan.sources) == 3
    assert len(plan.new_source_keys) == 3
    assert db.scalar(
        select(func.count(ReferenceLayerSource.id)).where(
            ReferenceLayerSource.source_key.like("auto:%")
        )
    ) == 0

    result = apply_mirror_bootstrap_plan(db, plan)
    assert result.created_count == 3
    assert result.updated_count == 0
    auto_sources = list(
        db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.source_key.like("auto:%")
            )
        )
    )
    assert all(source.enabled for source in auto_sources)
    primary = [source for source in auto_sources if source.is_primary]
    assert len(primary) == 1
    assert primary[0].id == min(
        auto_sources,
        key=lambda item: (item.priority, item.source_key),
    ).id

    repeated = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    assert repeated.new_source_keys == ()
    assert repeated.updated_source_keys == ()
    assert repeated.deactivated_source_keys == ()
    assert repeated.unchanged_count == 3
    no_op = apply_mirror_bootstrap_plan(db, repeated)
    assert no_op.created_count == no_op.updated_count == 0
    assert db.get(ReferenceLayerSource, manual.id).enabled is False


def test_bootstrap_rejects_stale_plan_and_deactivates_superseded_auto_sources(
    db,
) -> None:
    definition, _, _, old_sources, _ = _seed_bootstrap(db)
    stale = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    old_sources[0].next_check_at = NOW + timedelta(days=2)
    db.commit()
    with pytest.raises(MirrorPlanChangedError):
        apply_mirror_bootstrap_plan(db, stale)

    changed = replace(
        definition,
        raw_catalog={"revision": 2},
        layers=(
            replace(
                definition.layers[0],
                remote_name="planning:updated-zones",
            ),
        ),
        retrieved_at=NOW + timedelta(days=1),
    )
    apply_catalog_definition(db, changed)
    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    assert len(plan.new_source_keys) == 3
    assert len(plan.deactivated_source_keys) == 3
    result = apply_mirror_bootstrap_plan(db, plan)
    assert result.created_count == 3
    assert result.deactivated_count == 3
    assert all(
        not db.get(ReferenceLayerSource, item.id).enabled
        for item in old_sources
    )


def test_due_sources_enqueue_once_with_frozen_definition_and_generation(db) -> None:
    _, layer, _, sources, _ = _seed_bootstrap(db)
    for source in sources:
        source.next_check_at = NOW - timedelta(seconds=1)
    db.commit()

    run_ids = enqueue_due_sources(db, now=NOW)
    assert len(run_ids) == 1
    assert enqueue_due_sources(db, now=NOW) == ()
    runs = list(
        db.scalars(
            select(ReferenceSyncRun)
            .where(ReferenceSyncRun.id.in_(run_ids))
            .order_by(ReferenceSyncRun.id)
        )
    )
    assert all(run.status == "queued" for run in runs)
    assert all(run.check_mode == "full" for run in runs)
    assert all(run.expected_active_generation == 0 for run in runs)
    assert all(run.parent_run_id is None for run in runs)
    assert all(run.fallback_depth == 0 for run in runs)
    assert all(
        db.get(ReferenceLayerSource, run.source_id).is_primary
        for run in runs
    )
    assert all(
        _canonical_sha256(run.source_definition_json)
        == run.source_definition_sha256
        for run in runs
    )
    assert db.get(ReferenceLayer, layer.id) is not None


def test_failed_sources_fall_back_in_priority_order_and_exhaust_once(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    primary = next(source for source in sources if source.is_primary)
    primary.next_check_at = NOW - timedelta(seconds=1)
    db.commit()

    [root_id] = enqueue_due_sources(db, now=NOW)
    root_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "1" * 64,
    )
    finish_sync_run(
        db,
        root_lease,
        outcome="failed",
        error_code="primary_failed",
        now=NOW + timedelta(seconds=1),
    )
    first_child = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.parent_run_id == root_id
        )
    )
    assert first_child is not None
    assert first_child.fallback_depth == 1
    assert db.get(ReferenceLayerSource, first_child.source_id).priority == 30
    assert enqueue_fallback_source(
        db,
        failed_run_id=root_id,
        now=NOW + timedelta(seconds=2),
    ) == first_child.id

    first_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "2" * 64,
    )
    assert first_lease.run_id == first_child.id
    finish_sync_run(
        db,
        first_lease,
        outcome="rejected",
        error_code="fallback_rejected",
        now=NOW + timedelta(seconds=3),
    )
    second_child = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.parent_run_id == first_child.id
        )
    )
    assert second_child is not None
    assert second_child.fallback_depth == 2
    assert db.get(ReferenceLayerSource, second_child.source_id).priority == 90

    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=4),
        token_factory=lambda: "3" * 64,
    )
    assert second_lease.run_id == second_child.id
    finish_sync_run(
        db,
        second_lease,
        outcome="failed",
        error_code="fallback_exhausted",
        now=NOW + timedelta(seconds=5),
    )
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.provider_key == primary.provider_key,
            ReferenceSyncRun.layer_id == primary.layer_id,
        )
    ) == 3
    assert enqueue_fallback_source(
        db,
        failed_run_id=second_child.id,
        now=NOW + timedelta(seconds=6),
    ) is None
    assert next(source for source in sources if source.is_primary).id == primary.id


def test_successful_fallback_keeps_preferred_daily_source(db) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    primary = next(source for source in sources if source.is_primary)
    primary.next_check_at = NOW - timedelta(seconds=1)
    db.commit()

    enqueue_due_sources(db, now=NOW)
    primary_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "4" * 64,
    )
    finish_sync_run(
        db,
        primary_lease,
        outcome="failed",
        error_code="primary_failed",
        now=NOW + timedelta(seconds=1),
    )
    fallback_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "5" * 64,
    )
    fallback_source = db.get(ReferenceLayerSource, fallback_lease.source_id)
    assert fallback_source.id != primary.id
    fallback_version = _create_version(
        db,
        source=fallback_source,
        snapshot=snapshot,
        lease=fallback_lease,
        sequence_number=1,
    )
    promote_delivery_version(
        db,
        version_id=fallback_version.id,
        lease=fallback_lease,
        expected_generation=0,
        reason="validated fallback delivery",
        now=NOW + timedelta(seconds=3),
    )

    assert db.get(ReferenceLayerSource, primary.id).is_primary is True
    assert db.get(ReferenceLayerSource, fallback_source.id).is_primary is False
    [daily_id] = enqueue_due_sources(
        db,
        now=NOW + timedelta(days=1, seconds=1),
    )
    daily = db.get(ReferenceSyncRun, daily_id)
    assert daily.source_id == primary.id
    assert daily.parent_run_id is None
    assert daily.fallback_depth == 0
    assert daily.expected_active_generation == 1
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == fallback_version.id


def test_concurrent_fallback_enqueue_is_linear_and_idempotent(
    engine,
    committed_reference_providers,
) -> None:
    provider_key = "mirror-fallback-race-test"
    committed_reference_providers.append(provider_key)
    current = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as setup:
        _, _, _, sources, _ = _seed_bootstrap(
            setup,
            provider_key=provider_key,
        )
        primary = next(source for source in sources if source.is_primary)
        _only_source_due(setup, primary, at=current)
        enqueue_due_sources(setup, now=current)
        lease = claim_next_sync_run(
            setup,
            now=current,
            token_factory=lambda: "6" * 64,
        )
        finish_sync_run(
            setup,
            lease,
            outcome="failed",
            error_code="primary_failed",
            now=current + timedelta(seconds=1),
        )
        assert setup.scalar(
            select(func.count(ReferenceSyncRun.id)).where(
                ReferenceSyncRun.parent_run_id == lease.run_id
            )
        ) == 0
        for source in sources:
            source.enabled = True
        setup.commit()

    barrier = Barrier(2)

    def enqueue_concurrently() -> int | None:
        with Session(engine, expire_on_commit=False) as worker:
            barrier.wait(timeout=5)
            return enqueue_fallback_source(
                worker,
                failed_run_id=lease.run_id,
                now=current + timedelta(seconds=2),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue_concurrently(), range(2)))
    assert results[0] is not None and results[0] == results[1]
    with Session(engine) as check:
        assert check.scalar(
            select(func.count(ReferenceSyncRun.id)).where(
                ReferenceSyncRun.parent_run_id == lease.run_id
            )
        ) == 1
        assert check.scalar(
            select(func.count(ReferenceSyncRun.id)).where(
                ReferenceSyncRun.provider_key == provider_key,
                ReferenceSyncRun.status.in_(("queued", "running")),
            )
        ) == 1


def test_concurrent_schedulers_enqueue_only_one_primary_run_per_layer(
    engine,
    committed_reference_providers,
) -> None:
    provider_key = "mirror-scheduler-race-test"
    committed_reference_providers.append(provider_key)
    current = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as setup:
        _, _, _, sources, _ = _seed_bootstrap(
            setup,
            provider_key=provider_key,
        )
        primary = next(source for source in sources if source.is_primary)
        primary.next_check_at = current - timedelta(seconds=1)
        setup.commit()

    barrier = Barrier(2)

    def schedule_concurrently() -> tuple[int, ...]:
        with Session(engine, expire_on_commit=False) as worker:
            barrier.wait(timeout=5)
            return enqueue_due_sources(worker, now=current)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: schedule_concurrently(), range(2)))
    assert sorted(len(result) for result in results) == [0, 1]
    with Session(engine) as check:
        [run] = list(
            check.scalars(
                select(ReferenceSyncRun).where(
                    ReferenceSyncRun.provider_key == provider_key,
                    ReferenceSyncRun.status.in_(("queued", "running")),
                )
            )
        )
        assert run.source_id == primary.id
        assert run.parent_run_id is None


def test_claim_skips_a_queued_run_locked_by_another_worker(
    engine,
    committed_reference_providers,
) -> None:
    provider_keys = (
        "mirror-skip-locked-test-a",
        "mirror-skip-locked-test-b",
    )
    committed_reference_providers.extend(provider_keys)
    with Session(engine, expire_on_commit=False) as setup:
        for provider_key in provider_keys:
            _seed_bootstrap(
                setup,
                provider_key=provider_key,
            )
        for primary in setup.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key.in_(provider_keys),
                ReferenceLayerSource.is_primary.is_(True),
            )
        ):
            primary.next_check_at = NOW - timedelta(seconds=1)
        setup.commit()
        run_ids = enqueue_due_sources(setup, now=NOW)
        assert len(run_ids) == 2

    with Session(engine, expire_on_commit=False) as locker:
        locked_id = locker.scalar(
            select(ReferenceSyncRun.id)
            .where(ReferenceSyncRun.id.in_(run_ids))
            .order_by(ReferenceSyncRun.id)
            .limit(1)
            .with_for_update()
        )
        with Session(engine, expire_on_commit=False) as worker:
            lease = claim_next_sync_run(
                worker,
                now=NOW,
                token_factory=lambda: "9" * 64,
            )
            assert lease is not None
            assert lease.run_id != locked_id
        locker.rollback()

    with Session(engine, expire_on_commit=False) as cleanup:
        finish_sync_run(
            cleanup,
            lease,
            outcome="unchanged",
            now=NOW + timedelta(seconds=1),
        )
        remaining = claim_next_sync_run(
            cleanup,
            now=NOW + timedelta(seconds=1),
            token_factory=lambda: "a" * 64,
        )
        assert remaining is not None and remaining.run_id == locked_id
        finish_sync_run(
            cleanup,
            remaining,
            outcome="unchanged",
            now=NOW + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    [
        ("unchanged", None),
        ("succeeded", None),
        ("rejected", "validation_failed"),
        ("failed", "upstream_timeout"),
    ],
)
def test_claim_heartbeat_and_all_worker_terminal_outcomes(
    db,
    outcome: str,
    error_code: str | None,
) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    source = sources[0]
    _only_source_due(db, source)
    [run_id] = enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=60,
        token_factory=lambda: "1" * 64,
    )
    assert lease is not None and lease.run_id == run_id
    renewed = heartbeat_sync_run(
        db,
        lease,
        now=NOW + timedelta(seconds=20),
        lease_seconds=60,
    )
    assert renewed.lease_expires_at == NOW + timedelta(seconds=80)

    finished_id = finish_sync_run(
        db,
        renewed,
        outcome=outcome,
        now=NOW + timedelta(seconds=30),
        error_code=error_code,
        error_summary="bounded failure" if error_code else None,
        observed_manifest_sha256="b" * 64,
        stats_json={"items": 10},
    )
    run = db.get(ReferenceSyncRun, finished_id)
    assert run.status == outcome
    assert run.lease_token is None
    assert run.lease_expires_at is None
    assert run.heartbeat_at == NOW + timedelta(seconds=20)


def test_failed_outcome_requires_error_code(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    _only_source_due(db, sources[0])
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "2" * 64,
    )
    with pytest.raises(MirrorLifecycleError):
        finish_sync_run(
            db,
            lease,
            outcome="failed",
            now=NOW + timedelta(seconds=1),
        )


def test_expired_worker_is_fenced_from_finish_and_promotion(db) -> None:
    _, _, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    expired = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=30,
        token_factory=lambda: "3" * 64,
    )
    version = _create_version(
        db,
        source=source,
        snapshot=snapshot,
        lease=expired,
        sequence_number=1,
    )

    after_expiry = NOW + timedelta(seconds=31)
    with pytest.raises(MirrorLeaseLostError):
        finish_sync_run(
            db,
            expired,
            outcome="succeeded",
            now=after_expiry,
        )
    replacement = claim_next_sync_run(
        db,
        now=after_expiry,
        lease_seconds=60,
        token_factory=lambda: "4" * 64,
    )
    assert replacement.run_id == expired.run_id
    assert replacement.attempt_no == 2

    with pytest.raises(MirrorLeaseLostError):
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=expired,
            expected_generation=0,
            reason="stale worker must not publish",
            now=after_expiry + timedelta(seconds=1),
        )
    with pytest.raises(MirrorLeaseLostError):
        finish_sync_run(
            db,
            expired,
            outcome="succeeded",
            now=after_expiry + timedelta(seconds=1),
        )

    promoted = promote_delivery_version(
        db,
        version_id=version.id,
        lease=replacement,
        expected_generation=0,
        reason="replacement worker validated version",
        observed_manifest_sha256="a" * 64,
        stats_json={"features": 10},
        now=after_expiry + timedelta(seconds=1),
    )
    assert promoted.generation == 1
    promoted_run = db.get(ReferenceSyncRun, replacement.run_id)
    assert promoted_run.status == "succeeded"
    assert promoted_run.lease_token is None
    assert promoted_run.observed_manifest_sha256 == "a" * 64
    assert promoted_run.stats_json == {"features": 10}
    with pytest.raises(MirrorLeaseLostError):
        finish_sync_run(
            db,
            replacement,
            outcome="succeeded",
            now=after_expiry + timedelta(seconds=2),
        )


def test_reclaimed_attempt_is_fenced_even_if_a_token_is_reused(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    _only_source_due(db, sources[0])
    enqueue_due_sources(db, now=NOW)
    token = "d" * 64
    expired = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=30,
        token_factory=lambda: token,
    )
    replacement = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=31),
        lease_seconds=60,
        token_factory=lambda: token,
    )
    assert expired.attempt_no == 1
    assert replacement.attempt_no == 2

    with pytest.raises(MirrorLeaseLostError):
        heartbeat_sync_run(
            db,
            expired,
            now=NOW + timedelta(seconds=32),
        )
    finish_sync_run(
        db,
        replacement,
        outcome="unchanged",
        now=NOW + timedelta(seconds=32),
    )


def test_finish_rechecks_database_clock_after_waiting_for_the_run_lock(
    engine,
    committed_reference_providers,
) -> None:
    provider_key = "mirror-fresh-clock-fencing-test"
    committed_reference_providers.append(provider_key)
    current = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as setup:
        _, _, _, sources, _ = _seed_bootstrap(
            setup,
            provider_key=provider_key,
        )
        _only_source_due(setup, sources[0], at=current)
        enqueue_due_sources(setup, now=current)
        lease = claim_next_sync_run(
            setup,
            now=current,
            lease_seconds=30,
            token_factory=lambda: "f" * 64,
        )
        setup.execute(
            text(
                "UPDATE reference_sync_runs "
                "SET lease_expires_at = clock_timestamp() + "
                "INTERVAL '300 milliseconds' WHERE id = :run_id"
            ),
            {"run_id": lease.run_id},
        )
        setup.commit()

    def finish_after_lock() -> int:
        with Session(engine, expire_on_commit=False) as worker:
            return finish_sync_run(
                worker,
                lease,
                outcome="unchanged",
                now=None,
            )

    with Session(engine, expire_on_commit=False) as locker:
        locker.scalar(
            select(ReferenceSyncRun)
            .where(ReferenceSyncRun.id == lease.run_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finish_after_lock)
            time.sleep(0.6)
            locker.commit()
            with pytest.raises(MirrorLeaseLostError):
                future.result(timeout=5)


def test_promotion_rejects_corrupt_current_source_definition(db) -> None:
    _, _, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "e" * 64,
    )
    version = _create_version(
        db,
        source=source,
        snapshot=snapshot,
        lease=lease,
        sequence_number=1,
    )
    source.endpoint_url = "https://other.example.test/wfs"
    db.commit()

    with pytest.raises(
        MirrorPromotionConflict,
        match="current delivery source-definition hash is invalid",
    ):
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=0,
            reason="corrupt mutable source must not publish",
            now=NOW + timedelta(seconds=1),
        )


def test_promotion_rollback_and_deactivation_are_generation_fenced_hash_chain(
    db,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)

    enqueue_due_sources(db, now=NOW)
    first_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "5" * 64,
    )
    first_version = _create_version(
        db,
        source=source,
        snapshot=snapshot,
        lease=first_lease,
        sequence_number=1,
    )
    first = promote_delivery_version(
        db,
        version_id=first_version.id,
        lease=first_lease,
        expected_generation=0,
        reason="initial validated mirror",
        now=NOW + timedelta(seconds=1),
    )
    assert first.generation == 1
    assert db.get(ReferenceSyncRun, first_lease.run_id).status == "succeeded"

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    state.generation = 9
    db.commit()
    with pytest.raises(
        MirrorPromotionConflict,
        match="does not match the promotion-chain head",
    ):
        deactivate_delivery(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            expected_generation=9,
            reason="corrupt state must fail closed",
            now=NOW + timedelta(seconds=2),
        )
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    state.generation = 1
    db.commit()

    source.next_check_at = NOW + timedelta(seconds=3)
    db.commit()
    enqueue_due_sources(db, now=NOW + timedelta(seconds=3))
    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=3),
        token_factory=lambda: "6" * 64,
    )
    second_version = _create_version(
        db,
        source=source,
        snapshot=snapshot,
        lease=second_lease,
        sequence_number=2,
    )
    with pytest.raises(MirrorPromotionConflict):
        promote_delivery_version(
            db,
            version_id=second_version.id,
            lease=second_lease,
            expected_generation=0,
            reason="stale generation",
            now=NOW + timedelta(seconds=4),
        )
    with pytest.raises(
        MirrorPromotionConflict,
        match="rollback target was never an active delivery",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=second_version.id,
            expected_generation=1,
            reason="unpublished versions cannot bypass promotion",
            now=NOW + timedelta(seconds=4),
        )
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 1

    second = promote_delivery_version(
        db,
        version_id=second_version.id,
        lease=second_lease,
        expected_generation=1,
        reason="second validated mirror",
        now=NOW + timedelta(seconds=4),
    )
    assert second.from_version_id == first_version.id
    assert second.generation == 2
    assert db.get(ReferenceSyncRun, second_lease.run_id).status == "succeeded"

    rollback = rollback_delivery_version(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        to_version_id=first_version.id,
        expected_generation=2,
        reason="rollback after smoke-test regression",
        now=NOW + timedelta(seconds=6),
    )
    assert rollback.generation == 3
    assert rollback.to_version_id == first_version.id
    deactivated = deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=3,
        reason="operator disabled delivery",
        now=NOW + timedelta(seconds=7),
    )
    assert deactivated.generation == 4
    state = db.get(ReferenceLayerDeliveryState, (layer.provider_key, layer.id))
    assert state.status == "disabled"
    assert state.active_version_id is None
    assert state.last_promotion_id == deactivated.promotion_id

    events = list(
        db.scalars(
            select(ReferenceDeliveryPromotion)
            .where(
                ReferenceDeliveryPromotion.provider_key == layer.provider_key,
                ReferenceDeliveryPromotion.layer_id == layer.id,
            )
            .order_by(ReferenceDeliveryPromotion.sequence_number)
        )
    )
    assert [item.action for item in events] == [
        "promote",
        "promote",
        "rollback",
        "deactivate",
    ]
    assert all(stored_promotion_hash_is_valid(item) for item in events)
    assert all(
        events[index].previous_event_id == events[index - 1].id
        and events[index].previous_event_sha256
        == events[index - 1].event_sha256
        for index in range(1, len(events))
    )


def test_deactivation_stops_scheduling_and_requires_explicit_reactivation(
    db,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    first_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "7" * 64,
    )
    first_version = _create_version(
        db,
        source=source,
        snapshot=snapshot,
        lease=first_lease,
        sequence_number=1,
    )
    promote_delivery_version(
        db,
        version_id=first_version.id,
        lease=first_lease,
        expected_generation=0,
        reason="initial version",
        now=NOW + timedelta(seconds=1),
    )

    source.next_check_at = NOW + timedelta(seconds=2)
    db.commit()
    enqueue_due_sources(db, now=NOW + timedelta(seconds=2))
    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "8" * 64,
    )
    second_version = _create_version(
        db,
        source=source,
        snapshot=snapshot,
        lease=second_lease,
        sequence_number=2,
    )
    deactivated = deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=1,
        reason="administrative stop",
        now=NOW + timedelta(seconds=3),
    )
    run_count = db.scalar(select(func.count(ReferenceSyncRun.id)))
    assert all(
        not item.enabled and not item.is_primary
        for item in db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == layer.provider_key,
                ReferenceLayerSource.layer_id == layer.id,
            )
        )
    )
    assert enqueue_due_sources(db, now=NOW + timedelta(days=2)) == ()
    assert db.scalar(select(func.count(ReferenceSyncRun.id))) == run_count
    disabled_plan = build_mirror_bootstrap_plan(
        db,
        provider_key=layer.provider_key,
    )
    apply_mirror_bootstrap_plan(db, disabled_plan)
    assert all(
        not item.enabled
        for item in db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == layer.provider_key,
                ReferenceLayerSource.layer_id == layer.id,
            )
        )
    )
    with pytest.raises(
        MirrorPromotionConflict,
        match="administratively disabled",
    ):
        promote_delivery_version(
            db,
            version_id=second_version.id,
            lease=second_lease,
            expected_generation=1,
            reason="implicit reactivation is forbidden",
            now=NOW + timedelta(seconds=4),
        )

    reactivated = reactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        to_version_id=first_version.id,
        expected_generation=deactivated.generation,
        reason="operator explicitly restored the last known good version",
        now=NOW + timedelta(seconds=5),
    )
    assert reactivated.action == "reactivate"
    assert reactivated.generation == 3
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.status == "active"
    assert state.active_version_id == first_version.id
    assert db.get(ReferenceLayerSource, source.id).enabled is True
    finish_sync_run(
        db,
        second_lease,
        outcome="rejected",
        error_code="superseded_by_operator",
        now=NOW + timedelta(seconds=6),
    )
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.parent_run_id == second_lease.run_id
        )
    ) == 0
    [queued_id] = enqueue_due_sources(
        db,
        now=NOW + timedelta(seconds=6),
    )
    assert db.get(ReferenceSyncRun, queued_id).expected_active_generation == 3


def test_rollback_revalidates_the_original_run_and_current_source(db) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)

    versions = []
    for sequence_number, token in ((1, "a" * 64), (2, "b" * 64)):
        source.next_check_at = NOW + timedelta(seconds=sequence_number)
        db.commit()
        enqueue_due_sources(db, now=NOW + timedelta(seconds=sequence_number))
        lease = claim_next_sync_run(
            db,
            now=NOW + timedelta(seconds=sequence_number),
            token_factory=lambda token=token: token,
        )
        version = _create_version(
            db,
            source=source,
            snapshot=snapshot,
            lease=lease,
            sequence_number=sequence_number,
        )
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=sequence_number - 1,
            reason=f"version {sequence_number}",
            now=NOW + timedelta(seconds=sequence_number, milliseconds=100),
        )
        versions.append(version)

    original_run = db.get(ReferenceSyncRun, versions[0].sync_run_id)
    original_run.status = "failed"
    original_run.error_code = "post_publish_integrity_failure"
    original_run.error_summary = "operator marked the source evidence invalid"
    db.commit()
    with pytest.raises(
        MirrorPromotionConflict,
        match="did not finish successfully",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=versions[0].id,
            expected_generation=2,
            reason="must not revive invalid evidence",
            now=NOW + timedelta(seconds=5),
        )


def test_rollback_can_select_a_valid_version_from_a_previous_catalog_snapshot(
    db,
) -> None:
    (
        layer,
        snapshot_v1,
        snapshot_v2,
        version_v1,
        version_v2,
    ) = _promote_cross_snapshot_versions(db)
    run_v1 = db.get(ReferenceSyncRun, version_v1.sync_run_id)
    run_v2 = db.get(ReferenceSyncRun, version_v2.sync_run_id)

    rolled_back = rollback_delivery_version(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        to_version_id=version_v1.id,
        expected_generation=2,
        reason="catalog v2 renderer regression",
        now=NOW + timedelta(seconds=4),
    )

    assert snapshot_v1.id != snapshot_v2.id
    assert snapshot_v1.is_current is False
    assert snapshot_v2.is_current is True
    assert run_v1.source_definition_sha256 != run_v2.source_definition_sha256
    assert rolled_back.action == "rollback"
    assert rolled_back.from_version_id == version_v2.id
    assert rolled_back.to_version_id == version_v1.id
    assert rolled_back.generation == 3
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == version_v1.id


@pytest.mark.parametrize(
    "corruption",
    ("run_definition", "catalog_snapshot"),
)
def test_cross_snapshot_rollback_fails_closed_for_corrupt_frozen_evidence(
    db,
    corruption: str,
) -> None:
    (
        layer,
        snapshot_v1,
        _,
        version_v1,
        version_v2,
    ) = _promote_cross_snapshot_versions(db)
    if corruption == "run_definition":
        run = db.get(ReferenceSyncRun, version_v1.sync_run_id)
        run.source_definition_json = {
            **run.source_definition_json,
            "remote_name": "tampered:layer",
        }
    elif corruption == "catalog_snapshot":
        snapshot_v1.normalized_definition_json = {
            **snapshot_v1.normalized_definition_json,
            "unresolved_count": 99,
        }
    db.commit()

    with pytest.raises(MirrorPromotionConflict):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=version_v1.id,
            expected_generation=2,
            reason="corrupt historical evidence must never reactivate",
            now=NOW + timedelta(seconds=4),
        )

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 2
    assert state.active_version_id == version_v2.id


def test_published_asset_cannot_be_corrupted_before_cross_snapshot_rollback(
    db,
) -> None:
    (
        layer,
        _,
        _,
        version_v1,
        version_v2,
    ) = _promote_cross_snapshot_versions(db)
    asset = db.scalar(
        select(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.version_id == version_v1.id,
            ReferenceDeliveryAsset.is_primary.is_(True),
        )
    )
    asset.sha256 = "f" * 64

    with pytest.raises(
        OperationalError,
        match="reference mirror records are immutable",
    ):
        db.commit()
    db.rollback()

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 2
    assert state.active_version_id == version_v2.id
    assert db.get(ReferenceDeliveryAsset, asset.id).sha256 == (
        version_v1.content_sha256
    )


def test_worker_observation_metadata_is_bounded(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    _only_source_due(db, sources[0])
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "c" * 64,
    )
    invalid_arguments = (
        {"observed_etag": "e" * 4_097},
        {"observed_version": "v" * 2_049},
        {
            "outcome": "failed",
            "error_code": "failed",
            "error_summary": "x" * 4_097,
        },
        {"stats_json": {"payload": "x" * 1_048_576}},
    )
    for arguments in invalid_arguments:
        with pytest.raises(MirrorLifecycleError):
            finish_sync_run(
                db,
                lease,
                outcome=arguments.pop("outcome", "unchanged"),
                now=NOW + timedelta(seconds=1),
                **arguments,
            )
    finish_sync_run(
        db,
        lease,
        outcome="unchanged",
        now=NOW + timedelta(seconds=1),
    )
