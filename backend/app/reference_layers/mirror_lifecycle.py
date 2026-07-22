"""Transactional lifecycle for locally mirrored reference-layer deliveries.

This module deliberately stops at database coordination.  Workers perform
network and storage I/O outside these short transactions, holding only an
expiring lease token.  The token and the layer generation fence stale workers
from finishing or publishing obsolete work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from typing import Any, Callable, Literal

from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.reference_layers.catalog import (
    canonical_normalized_definition_sha256,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceService,
    ReferenceSyncRun,
)
from app.reference_layers.source_audit import (
    _layer_definition,
    _service_definition,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
)

AUTO_SOURCE_PREFIX = "auto:"
BOOTSTRAP_PLAN_SCHEMA = "siur-mirror-source-bootstrap-v1"
PROMOTION_EVENT_SCHEMA = "siur-mirror-promotion-event-v1"
_MIRROR_SOURCE_LOCK_DOMAIN = b"asistente/reference-mirror-sources/v1\0"
_MIRROR_LAYER_LOCK_DOMAIN = b"asistente/reference-mirror-layer/v1\0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEASE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

TerminalOutcome = Literal["unchanged", "succeeded", "rejected", "failed"]


class MirrorLifecycleError(RuntimeError):
    """Base error for a rejected mirror lifecycle transition."""


class MirrorBootstrapError(MirrorLifecycleError):
    """The current catalog cannot be bootstrapped safely."""


class MirrorPlanChangedError(MirrorBootstrapError):
    """The reviewed bootstrap plan is stale."""


class MirrorLeaseLostError(MirrorLifecycleError):
    """The worker no longer owns a live lease for the sync run."""


class MirrorPromotionConflict(MirrorLifecycleError):
    """A delivery transition failed optimistic or integrity checks."""


@dataclass(frozen=True)
class PlannedMirrorSource:
    layer_id: int
    source_key: str
    protocol: str
    target_kind: str
    endpoint_url: str | None
    remote_name: str | None
    source_format: str | None
    sync_strategy: str
    config_json: dict[str, Any]
    definition_sha256: str
    priority: int

    def identity(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "source_key": self.source_key,
            "protocol": self.protocol,
            "target_kind": self.target_kind,
            "endpoint_url": self.endpoint_url,
            "remote_name": self.remote_name,
            "source_format": self.source_format,
            "sync_strategy": self.sync_strategy,
            "config_json": self.config_json,
            "definition_sha256": self.definition_sha256,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class MirrorBootstrapPlan:
    provider_key: str
    snapshot_id: int
    catalog_definition_sha256: str
    base_state_sha256: str
    sources: tuple[PlannedMirrorSource, ...]
    new_source_keys: tuple[str, ...]
    updated_source_keys: tuple[str, ...]
    deactivated_source_keys: tuple[str, ...]
    unchanged_count: int

    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": BOOTSTRAP_PLAN_SCHEMA,
            "provider_key": self.provider_key,
            "snapshot_id": self.snapshot_id,
            "catalog_definition_sha256": self.catalog_definition_sha256,
            "base_state_sha256": self.base_state_sha256,
            "sources": [item.identity() for item in self.sources],
            "new_source_keys": list(self.new_source_keys),
            "updated_source_keys": list(self.updated_source_keys),
            "deactivated_source_keys": list(self.deactivated_source_keys),
            "unchanged_count": self.unchanged_count,
        }

    @property
    def plan_sha256(self) -> str:
        return _canonical_sha256(self.identity())


@dataclass(frozen=True)
class AppliedMirrorBootstrap:
    plan_sha256: str
    created_count: int
    updated_count: int
    deactivated_count: int


@dataclass(frozen=True)
class SyncRunLease:
    run_id: int
    source_id: int
    token: str
    attempt_no: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class PromotionResult:
    promotion_id: int
    provider_key: str
    layer_id: int
    action: str
    from_version_id: int | None
    to_version_id: int | None
    generation: int
    event_sha256: str


def build_mirror_bootstrap_plan(
    db: Session,
    *,
    provider_key: str,
) -> MirrorBootstrapPlan:
    """Build a deterministic, read-only plan for the current catalog."""

    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if snapshot is None:
        raise MirrorBootstrapError("current applied catalog is unavailable")
    if (
        canonical_normalized_definition_sha256(
            snapshot.normalized_definition_json
        )
        != snapshot.definition_sha256
    ):
        raise MirrorBootstrapError("current catalog definition hash is invalid")

    services = {
        item.id: item
        for item in db.scalars(
            select(ReferenceService).where(
                ReferenceService.provider_key == provider_key,
                ReferenceService.last_seen_snapshot_id == snapshot.id,
            )
        )
    }
    layers = list(
        db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == provider_key,
                ReferenceLayer.last_seen_snapshot_id == snapshot.id,
                ReferenceLayer.node_type == "layer",
            )
            .order_by(ReferenceLayer.id)
        )
    )
    if not layers:
        raise MirrorBootstrapError("current catalog has no leaf layers")

    planned: list[PlannedMirrorSource] = []
    for layer in layers:
        service = services.get(layer.service_id or -1)
        if service is None:
            raise MirrorBootstrapError(
                f"layer {layer.source_key!r} has no current catalog service"
            )
        try:
            candidates = acquisition_candidates(
                _service_definition(service),
                _layer_definition(layer),
            )
        except SourceDiscoveryError as exc:
            raise MirrorBootstrapError(
                f"cannot discover source for {layer.source_key!r}: {exc}"
            ) from exc
        if not candidates:
            raise MirrorBootstrapError(
                f"layer {layer.source_key!r} has no acquisition candidate"
            )
        for candidate in candidates:
            source_format = candidate.config.get("format")
            if source_format is not None and not isinstance(source_format, str):
                source_format = None
            endpoint_url = (
                None
                if candidate.protocol == "local"
                else candidate.endpoint_url
            )
            stored_definition = {
                "protocol": candidate.protocol,
                "target_kind": candidate.target_kind,
                "endpoint_url": endpoint_url,
                "remote_name": candidate.remote_name,
                "sync_strategy": candidate.sync_strategy,
                "priority": candidate.priority,
                "config": candidate.config,
            }
            planned.append(
                PlannedMirrorSource(
                    layer_id=layer.id,
                    source_key=candidate.source_key,
                    protocol=candidate.protocol,
                    target_kind=candidate.target_kind,
                    endpoint_url=endpoint_url,
                    remote_name=candidate.remote_name,
                    source_format=source_format,
                    sync_strategy=candidate.sync_strategy,
                    config_json=candidate.config,
                    definition_sha256=_canonical_sha256(stored_definition),
                    priority=candidate.priority,
                )
            )
    planned.sort(key=lambda item: (item.layer_id, item.priority, item.source_key))

    existing = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.provider_key == provider_key)
            .order_by(ReferenceLayerSource.layer_id, ReferenceLayerSource.source_key)
        )
    )
    base_state_sha256 = _canonical_sha256(
        [_stored_source_state(item) for item in existing]
    )
    existing_by_key = {
        (item.layer_id, item.source_key): item
        for item in existing
        if item.source_key.startswith(AUTO_SOURCE_PREFIX)
    }
    planned_by_key = {
        (item.layer_id, item.source_key): item for item in planned
    }
    new: list[str] = []
    updated: list[str] = []
    unchanged = 0
    for key, item in planned_by_key.items():
        record = existing_by_key.get(key)
        display_key = _display_source_key(item.layer_id, item.source_key)
        if record is None:
            new.append(display_key)
        elif not _source_matches_plan(record, item):
            updated.append(display_key)
        else:
            unchanged += 1
    deactivated = sorted(
        _display_source_key(layer_id, source_key)
        for (layer_id, source_key), record in existing_by_key.items()
        if (layer_id, source_key) not in planned_by_key
        and (record.enabled or record.is_primary)
    )
    return MirrorBootstrapPlan(
        provider_key=provider_key,
        snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        base_state_sha256=base_state_sha256,
        sources=tuple(planned),
        new_source_keys=tuple(sorted(new)),
        updated_source_keys=tuple(sorted(updated)),
        deactivated_source_keys=tuple(deactivated),
        unchanged_count=unchanged,
    )


def apply_mirror_bootstrap_plan(
    db: Session,
    reviewed_plan: MirrorBootstrapPlan,
) -> AppliedMirrorBootstrap:
    """Apply a reviewed plan after revalidation under a provider lock."""

    try:
        _lock_source_provider(db, reviewed_plan.provider_key)
        current = build_mirror_bootstrap_plan(
            db,
            provider_key=reviewed_plan.provider_key,
        )
        if current.plan_sha256 != reviewed_plan.plan_sha256:
            raise MirrorPlanChangedError(
                "mirror bootstrap state changed after the plan was reviewed"
            )

        existing = {
            (item.layer_id, item.source_key): item
            for item in db.scalars(
                select(ReferenceLayerSource).where(
                    ReferenceLayerSource.provider_key
                    == reviewed_plan.provider_key
                )
            )
        }
        planned_keys: set[tuple[int, str]] = set()
        created = 0
        updated_count = 0
        for item in reviewed_plan.sources:
            key = (item.layer_id, item.source_key)
            planned_keys.add(key)
            record = existing.get(key)
            if record is None:
                db.add(
                    ReferenceLayerSource(
                        provider_key=reviewed_plan.provider_key,
                        layer_id=item.layer_id,
                        source_key=item.source_key,
                        protocol=item.protocol,
                        target_kind=item.target_kind,
                        endpoint_url=item.endpoint_url,
                        remote_name=item.remote_name,
                        source_format=item.source_format,
                        sync_strategy=item.sync_strategy,
                        config_json=item.config_json,
                        definition_sha256=item.definition_sha256,
                        enabled=True,
                        is_primary=False,
                        priority=item.priority,
                    )
                )
                created += 1
            elif not _source_matches_plan(record, item):
                record.protocol = item.protocol
                record.target_kind = item.target_kind
                record.endpoint_url = item.endpoint_url
                record.remote_name = item.remote_name
                record.source_format = item.source_format
                record.sync_strategy = item.sync_strategy
                record.config_json = item.config_json
                record.definition_sha256 = item.definition_sha256
                record.enabled = True
                record.is_primary = False
                record.priority = item.priority
                updated_count += 1

        deactivated = 0
        for key, record in existing.items():
            if (
                record.source_key.startswith(AUTO_SOURCE_PREFIX)
                and key not in planned_keys
                and (record.enabled or record.is_primary)
            ):
                record.enabled = False
                record.is_primary = False
                deactivated += 1
        db.commit()
        return AppliedMirrorBootstrap(
            plan_sha256=reviewed_plan.plan_sha256,
            created_count=created,
            updated_count=updated_count,
            deactivated_count=deactivated,
        )
    except Exception:
        db.rollback()
        raise


def enqueue_due_sources(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> tuple[int, ...]:
    """Queue due enabled sources once, skipping rows owned by another caller."""

    moment = _moment(now)
    if not 1 <= limit <= 10_000:
        raise MirrorLifecycleError("enqueue limit must be between 1 and 10000")
    open_run = exists(
        select(ReferenceSyncRun.id).where(
            ReferenceSyncRun.source_id == ReferenceLayerSource.id,
            ReferenceSyncRun.status.in_(("queued", "running")),
        )
    )
    try:
        sources = list(
            db.scalars(
                select(ReferenceLayerSource)
                .where(
                    ReferenceLayerSource.enabled.is_(True),
                    ReferenceLayerSource.sync_strategy != "manual",
                    ReferenceLayerSource.next_check_at <= moment,
                    ~open_run,
                )
                .order_by(
                    ReferenceLayerSource.next_check_at,
                    ReferenceLayerSource.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        run_ids: list[int] = []
        for source in sources:
            definition = _stored_source_definition(source)
            if _canonical_sha256(definition) != source.definition_sha256:
                raise MirrorLifecycleError(
                    f"source {source.id} definition hash is invalid"
                )
            state_generation = db.scalar(
                select(ReferenceLayerDeliveryState.generation).where(
                    ReferenceLayerDeliveryState.provider_key
                    == source.provider_key,
                    ReferenceLayerDeliveryState.layer_id == source.layer_id,
                )
            )
            latest_full = db.scalar(
                select(func.max(ReferenceSyncRun.finished_at)).where(
                    ReferenceSyncRun.source_id == source.id,
                    ReferenceSyncRun.check_mode == "full",
                    ReferenceSyncRun.status.in_(("unchanged", "succeeded")),
                )
            )
            full_cutoff = moment - timedelta(
                seconds=source.full_refresh_interval_seconds
            )
            check_mode = (
                "full"
                if latest_full is None or latest_full <= full_cutoff
                else "conditional"
            )
            run = ReferenceSyncRun(
                source_id=source.id,
                source_definition_json=definition,
                source_definition_sha256=source.definition_sha256,
                trigger_kind="scheduled",
                check_mode=check_mode,
                status="queued",
                attempt_no=1,
                expected_active_generation=state_generation or 0,
                queued_at=moment,
                stats_json={},
            )
            db.add(run)
            db.flush()
            run_ids.append(run.id)
            source.next_check_at = moment + timedelta(
                seconds=source.check_interval_seconds
            )
        db.commit()
        return tuple(run_ids)
    except Exception:
        db.rollback()
        raise


def claim_next_sync_run(
    db: Session,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
    token_factory: Callable[[], str] | None = None,
) -> SyncRunLease | None:
    """Claim a queued run or reclaim an expired run with ``SKIP LOCKED``."""

    moment = _moment(now)
    _validate_lease_seconds(lease_seconds)
    token = (token_factory or (lambda: secrets.token_hex(32)))()
    if not isinstance(token, str) or not _LEASE_TOKEN_RE.fullmatch(token):
        raise MirrorLifecycleError("lease token must be 64 lowercase hex digits")
    try:
        run = db.scalar(
            select(ReferenceSyncRun)
            .where(
                or_(
                    ReferenceSyncRun.status == "queued",
                    and_(
                        ReferenceSyncRun.status == "running",
                        ReferenceSyncRun.lease_expires_at <= moment,
                    ),
                )
            )
            .order_by(
                case((ReferenceSyncRun.status == "queued", 0), else_=1),
                ReferenceSyncRun.queued_at,
                ReferenceSyncRun.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run is None:
            db.rollback()
            return None
        if run.status == "running":
            run.attempt_no += 1
        run.status = "running"
        run.started_at = moment
        run.finished_at = None
        run.lease_token = token
        run.heartbeat_at = moment
        run.lease_expires_at = moment + timedelta(seconds=lease_seconds)
        run.error_code = None
        run.error_summary = None
        db.commit()
        return SyncRunLease(
            run_id=run.id,
            source_id=run.source_id,
            token=token,
            attempt_no=run.attempt_no,
            lease_expires_at=run.lease_expires_at,
        )
    except Exception:
        db.rollback()
        raise


def heartbeat_sync_run(
    db: Session,
    lease: SyncRunLease,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
) -> SyncRunLease:
    """Extend a still-live lease; expired leases cannot be resurrected."""

    moment = _moment(now)
    _validate_lease(lease)
    _validate_lease_seconds(lease_seconds)
    expires_at = moment + timedelta(seconds=lease_seconds)
    try:
        row = db.execute(
            update(ReferenceSyncRun)
            .where(*_live_lease_predicates(lease, moment))
            .values(heartbeat_at=moment, lease_expires_at=expires_at)
            .returning(ReferenceSyncRun.id)
        ).first()
        if row is None:
            raise MirrorLeaseLostError("sync-run lease is absent or expired")
        db.commit()
        return SyncRunLease(
            run_id=lease.run_id,
            source_id=lease.source_id,
            token=lease.token,
            attempt_no=lease.attempt_no,
            lease_expires_at=expires_at,
        )
    except Exception:
        db.rollback()
        raise


def finish_sync_run(
    db: Session,
    lease: SyncRunLease,
    *,
    outcome: TerminalOutcome,
    now: datetime | None = None,
    observed_etag: str | None = None,
    observed_last_modified: datetime | None = None,
    observed_version: str | None = None,
    observed_manifest_sha256: str | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    stats_json: dict[str, Any] | None = None,
) -> int:
    """Finalize a live leased run with a fencing-safe conditional update."""

    moment = _moment(now)
    _validate_lease(lease)
    if outcome not in {"unchanged", "succeeded", "rejected", "failed"}:
        raise MirrorLifecycleError("unsupported sync-run outcome")
    if outcome in {"rejected", "failed"}:
        error_code = _bounded_required_text(error_code, "error_code", 64)
    else:
        error_code = None
        error_summary = None
    if observed_manifest_sha256 is not None and not _SHA256_RE.fullmatch(
        observed_manifest_sha256
    ):
        raise MirrorLifecycleError("observed manifest hash is invalid")
    if observed_last_modified is not None:
        observed_last_modified = _moment(observed_last_modified)
    stats = stats_json or {}
    _canonical_json(stats)
    try:
        row = db.execute(
            update(ReferenceSyncRun)
            .where(*_live_lease_predicates(lease, moment))
            .values(
                status=outcome,
                finished_at=moment,
                lease_token=None,
                lease_expires_at=None,
                observed_etag=observed_etag,
                observed_last_modified=observed_last_modified,
                observed_version=observed_version,
                observed_manifest_sha256=observed_manifest_sha256,
                error_code=error_code,
                error_summary=error_summary,
                stats_json=stats,
            )
            .returning(ReferenceSyncRun.id)
        ).first()
        if row is None:
            raise MirrorLeaseLostError("sync-run lease is absent or expired")
        db.commit()
        return row[0]
    except Exception:
        db.rollback()
        raise


def promote_delivery_version(
    db: Session,
    *,
    version_id: int,
    lease: SyncRunLease,
    expected_generation: int,
    reason: str,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> PromotionResult:
    """Atomically publish a validated version owned by a live worker lease."""

    moment = _moment(now)
    _validate_lease(lease)
    _validate_expected_generation(expected_generation)
    reason = _bounded_required_text(reason, "reason", 10_000)
    try:
        version = db.get(ReferenceDeliveryVersion, version_id)
        if version is None:
            raise MirrorPromotionConflict("delivery version does not exist")
        _lock_delivery_layer(db, version.provider_key, version.layer_id)
        version = db.scalar(
            select(ReferenceDeliveryVersion)
            .where(ReferenceDeliveryVersion.id == version_id)
            .with_for_update()
        )
        if version is None:
            raise MirrorPromotionConflict("delivery version does not exist")
        run = db.scalar(
            select(ReferenceSyncRun)
            .where(*_live_lease_predicates(lease, moment))
            .with_for_update()
        )
        if run is None:
            raise MirrorLeaseLostError("sync-run lease is absent or expired")
        if version.sync_run_id != run.id or version.source_id != run.source_id:
            raise MirrorPromotionConflict(
                "delivery version does not belong to the leased run"
            )
        if run.expected_active_generation != expected_generation:
            raise MirrorPromotionConflict(
                "sync run was queued for a different active generation"
            )
        if (
            _canonical_sha256(run.source_definition_json)
            != run.source_definition_sha256
        ):
            raise MirrorPromotionConflict(
                "sync run source-definition hash is invalid"
            )
        source = db.get(ReferenceLayerSource, version.source_id)
        if source is None or (
            source.provider_key != version.provider_key
            or source.layer_id != version.layer_id
        ):
            raise MirrorPromotionConflict("delivery source identity is invalid")
        if not source.enabled:
            raise MirrorPromotionConflict("delivery source is disabled")
        if (
            _canonical_sha256(_stored_source_definition(source))
            != source.definition_sha256
        ):
            raise MirrorPromotionConflict(
                "current delivery source-definition hash is invalid"
            )
        if run.source_definition_sha256 != source.definition_sha256:
            raise MirrorPromotionConflict(
                "delivery source changed while the run was in progress"
            )
        if source.target_kind != version.delivery_kind:
            raise MirrorPromotionConflict(
                "delivery kind does not match the acquisition source"
            )
        _validate_current_version_catalog(db, version)
        _validate_version_ready(db, version)

        state, latest = _locked_delivery_state_and_chain(
            db,
            provider_key=version.provider_key,
            layer_id=version.layer_id,
        )
        generation = state.generation if state is not None else 0
        if generation != expected_generation:
            raise MirrorPromotionConflict(
                "active delivery generation changed before promotion"
            )
        from_version_id = (
            state.active_version_id
            if state is not None and state.status == "active"
            else None
        )
        if from_version_id == version.id:
            raise MirrorPromotionConflict("delivery version is already active")
        promotion, new_generation = _append_promotion(
            db,
            provider_key=version.provider_key,
            layer_id=version.layer_id,
            action="promote",
            from_version_id=from_version_id,
            to_version_id=version.id,
            run_id=run.id,
            actor_id=actor_id,
            reason=reason,
            created_at=moment,
            state=state,
            latest=latest,
        )
        db.commit()
        return _promotion_result(promotion, new_generation)
    except Exception:
        db.rollback()
        raise


def rollback_delivery_version(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
    reason: str,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> PromotionResult:
    """Atomically point an active layer back to a prior immutable version."""

    moment = _moment(now)
    _validate_expected_generation(expected_generation)
    reason = _bounded_required_text(reason, "reason", 10_000)
    try:
        _lock_delivery_layer(db, provider_key, layer_id)
        state, latest = _locked_delivery_state_and_chain(
            db,
            provider_key=provider_key,
            layer_id=layer_id,
        )
        if state is None or state.status != "active":
            raise MirrorPromotionConflict("layer has no active delivery")
        if state.generation != expected_generation:
            raise MirrorPromotionConflict(
                "active delivery generation changed before rollback"
            )
        if state.active_version_id == to_version_id:
            raise MirrorPromotionConflict("rollback target is already active")
        version = db.scalar(
            select(ReferenceDeliveryVersion).where(
                ReferenceDeliveryVersion.id == to_version_id,
                ReferenceDeliveryVersion.provider_key == provider_key,
                ReferenceDeliveryVersion.layer_id == layer_id,
            )
        )
        if version is None:
            raise MirrorPromotionConflict(
                "rollback target is not a version of this layer"
            )
        was_active = db.scalar(
            select(ReferenceDeliveryPromotion.id)
            .where(
                ReferenceDeliveryPromotion.provider_key == provider_key,
                ReferenceDeliveryPromotion.layer_id == layer_id,
                ReferenceDeliveryPromotion.to_version_id == version.id,
                ReferenceDeliveryPromotion.action.in_(("promote", "rollback")),
            )
            .limit(1)
        )
        if was_active is None:
            raise MirrorPromotionConflict(
                "rollback target was never an active delivery"
            )
        _validate_version_ready(db, version)
        promotion, generation = _append_promotion(
            db,
            provider_key=provider_key,
            layer_id=layer_id,
            action="rollback",
            from_version_id=state.active_version_id,
            to_version_id=version.id,
            run_id=None,
            actor_id=actor_id,
            reason=reason,
            created_at=moment,
            state=state,
            latest=latest,
        )
        db.commit()
        return _promotion_result(promotion, generation)
    except Exception:
        db.rollback()
        raise


def deactivate_delivery(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    expected_generation: int,
    reason: str,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> PromotionResult:
    """Atomically deactivate a layer while retaining its immutable history."""

    moment = _moment(now)
    _validate_expected_generation(expected_generation)
    reason = _bounded_required_text(reason, "reason", 10_000)
    try:
        _lock_delivery_layer(db, provider_key, layer_id)
        state, latest = _locked_delivery_state_and_chain(
            db,
            provider_key=provider_key,
            layer_id=layer_id,
        )
        if state is None or state.status != "active":
            raise MirrorPromotionConflict("layer has no active delivery")
        if state.generation != expected_generation:
            raise MirrorPromotionConflict(
                "active delivery generation changed before deactivation"
            )
        promotion, generation = _append_promotion(
            db,
            provider_key=provider_key,
            layer_id=layer_id,
            action="deactivate",
            from_version_id=state.active_version_id,
            to_version_id=None,
            run_id=None,
            actor_id=actor_id,
            reason=reason,
            created_at=moment,
            state=state,
            latest=latest,
        )
        db.commit()
        return _promotion_result(promotion, generation)
    except Exception:
        db.rollback()
        raise


def canonical_promotion_event_sha256(
    *,
    provider_key: str,
    layer_id: int,
    sequence_number: int,
    action: str,
    from_version_id: int | None,
    to_version_id: int | None,
    run_id: int | None,
    actor_id: int | None,
    reason: str,
    previous_event_id: int | None,
    previous_event_sha256: str | None,
    created_at: datetime,
) -> str:
    """Hash the complete semantic contents of one promotion-chain event."""

    return _canonical_sha256(
        {
            "schema_version": PROMOTION_EVENT_SCHEMA,
            "provider_key": provider_key,
            "layer_id": layer_id,
            "sequence_number": sequence_number,
            "action": action,
            "from_version_id": from_version_id,
            "to_version_id": to_version_id,
            "run_id": run_id,
            "actor_id": actor_id,
            "reason": reason,
            "previous_event_id": previous_event_id,
            "previous_event_sha256": previous_event_sha256,
            "created_at": _utc_isoformat(created_at),
        }
    )


def stored_promotion_hash_is_valid(
    promotion: ReferenceDeliveryPromotion,
) -> bool:
    try:
        digest = canonical_promotion_event_sha256(
            provider_key=promotion.provider_key,
            layer_id=promotion.layer_id,
            sequence_number=promotion.sequence_number,
            action=promotion.action,
            from_version_id=promotion.from_version_id,
            to_version_id=promotion.to_version_id,
            run_id=promotion.run_id,
            actor_id=promotion.actor_id,
            reason=promotion.reason,
            previous_event_id=promotion.previous_event_id,
            previous_event_sha256=promotion.previous_event_sha256,
            created_at=promotion.created_at,
        )
    except (MirrorLifecycleError, TypeError, ValueError):
        return False
    return digest == promotion.event_sha256


def _append_promotion(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    action: str,
    from_version_id: int | None,
    to_version_id: int | None,
    run_id: int | None,
    actor_id: int | None,
    reason: str,
    created_at: datetime,
    state: ReferenceLayerDeliveryState | None,
    latest: ReferenceDeliveryPromotion | None,
) -> tuple[ReferenceDeliveryPromotion, int]:
    sequence_number = 1 if latest is None else latest.sequence_number + 1
    previous_event_id = None if latest is None else latest.id
    previous_event_sha256 = None if latest is None else latest.event_sha256
    event_sha256 = canonical_promotion_event_sha256(
        provider_key=provider_key,
        layer_id=layer_id,
        sequence_number=sequence_number,
        action=action,
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        run_id=run_id,
        actor_id=actor_id,
        reason=reason,
        previous_event_id=previous_event_id,
        previous_event_sha256=previous_event_sha256,
        created_at=created_at,
    )
    promotion = ReferenceDeliveryPromotion(
        provider_key=provider_key,
        layer_id=layer_id,
        sequence_number=sequence_number,
        action=action,
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        run_id=run_id,
        actor_id=actor_id,
        reason=reason,
        previous_event_id=previous_event_id,
        previous_event_sha256=previous_event_sha256,
        event_sha256=event_sha256,
        created_at=created_at,
    )
    db.add(promotion)
    db.flush()
    new_generation = sequence_number
    if state is None:
        state = ReferenceLayerDeliveryState(
            provider_key=provider_key,
            layer_id=layer_id,
            status="disabled" if action == "deactivate" else "active",
            active_version_id=None if action == "deactivate" else to_version_id,
            generation=new_generation,
            last_promotion_id=promotion.id,
            updated_at=created_at,
        )
        db.add(state)
    else:
        state.status = "disabled" if action == "deactivate" else "active"
        state.active_version_id = (
            None if action == "deactivate" else to_version_id
        )
        state.generation = new_generation
        state.last_promotion_id = promotion.id
        state.updated_at = created_at
    db.flush()
    return promotion, new_generation


def _locked_delivery_state_and_chain(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
) -> tuple[
    ReferenceLayerDeliveryState | None,
    ReferenceDeliveryPromotion | None,
]:
    state = db.scalar(
        select(ReferenceLayerDeliveryState)
        .where(
            ReferenceLayerDeliveryState.provider_key == provider_key,
            ReferenceLayerDeliveryState.layer_id == layer_id,
        )
        .with_for_update()
    )
    latest = db.scalar(
        select(ReferenceDeliveryPromotion)
        .where(
            ReferenceDeliveryPromotion.provider_key == provider_key,
            ReferenceDeliveryPromotion.layer_id == layer_id,
        )
        .order_by(ReferenceDeliveryPromotion.sequence_number.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is None:
        if state is not None:
            raise MirrorPromotionConflict(
                "delivery state exists without a promotion chain"
            )
    else:
        if not stored_promotion_hash_is_valid(latest):
            raise MirrorPromotionConflict("latest promotion event hash is invalid")
        if state is None or state.last_promotion_id != latest.id:
            raise MirrorPromotionConflict(
                "delivery state does not point to the promotion-chain head"
            )
        expected_status = (
            "disabled" if latest.action == "deactivate" else "active"
        )
        expected_version_id = (
            None if latest.action == "deactivate" else latest.to_version_id
        )
        if (
            state.generation != latest.sequence_number
            or state.status != expected_status
            or state.active_version_id != expected_version_id
        ):
            raise MirrorPromotionConflict(
                "delivery state does not match the promotion-chain head"
            )
    return state, latest


def _validate_current_version_catalog(
    db: Session,
    version: ReferenceDeliveryVersion,
) -> None:
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.id == version.catalog_snapshot_id,
            ReferenceCatalogSnapshot.provider_key == version.provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if snapshot is None or (
        snapshot.definition_sha256 != version.catalog_definition_sha256
        or canonical_normalized_definition_sha256(
            snapshot.normalized_definition_json
        )
        != snapshot.definition_sha256
    ):
        raise MirrorPromotionConflict(
            "delivery version is not bound to the current valid catalog"
        )


def _validate_version_ready(
    db: Session,
    version: ReferenceDeliveryVersion,
) -> None:
    if not isinstance(version.validation_json, dict) or (
        version.validation_json.get("passed") is not True
    ):
        raise MirrorPromotionConflict("delivery version did not pass validation")
    if _canonical_sha256(version.validation_json) != version.validation_sha256:
        raise MirrorPromotionConflict("delivery validation hash is invalid")
    primary = db.scalar(
        select(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.version_id == version.id,
            ReferenceDeliveryAsset.is_primary.is_(True),
        )
    )
    if primary is None:
        raise MirrorPromotionConflict("delivery version has no primary asset")
    valid_kinds = {
        "vector": {"vector_table"},
        "raster": {"raster_cog"},
        "tiles": {"tile_archive", "tile_prefix"},
    }
    if primary.asset_kind not in valid_kinds.get(version.delivery_kind, set()):
        raise MirrorPromotionConflict(
            "primary asset is incompatible with the delivery kind"
        )


def _promotion_result(
    promotion: ReferenceDeliveryPromotion,
    generation: int,
) -> PromotionResult:
    return PromotionResult(
        promotion_id=promotion.id,
        provider_key=promotion.provider_key,
        layer_id=promotion.layer_id,
        action=promotion.action,
        from_version_id=promotion.from_version_id,
        to_version_id=promotion.to_version_id,
        generation=generation,
        event_sha256=promotion.event_sha256,
    )


def _stored_source_definition(source: ReferenceLayerSource) -> dict[str, Any]:
    """Return the canonical definition frozen into each sync run."""

    return {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": source.priority,
        "config": source.config_json,
    }


def _stored_source_state(source: ReferenceLayerSource) -> dict[str, Any]:
    return {
        "layer_id": source.layer_id,
        "source_key": source.source_key,
        "definition": _stored_source_definition(source),
        "source_format": source.source_format,
        "definition_sha256": source.definition_sha256,
        "enabled": source.enabled,
        "is_primary": source.is_primary,
        "check_interval_seconds": source.check_interval_seconds,
        "full_refresh_interval_seconds": (
            source.full_refresh_interval_seconds
        ),
        "next_check_at": _utc_isoformat(source.next_check_at),
    }


def _source_matches_plan(
    source: ReferenceLayerSource,
    planned: PlannedMirrorSource,
) -> bool:
    definition = {
        "protocol": planned.protocol,
        "target_kind": planned.target_kind,
        "endpoint_url": planned.endpoint_url,
        "remote_name": planned.remote_name,
        "sync_strategy": planned.sync_strategy,
        "priority": planned.priority,
        "config": planned.config_json,
    }
    return (
        source.enabled
        and source.protocol == planned.protocol
        and source.target_kind == planned.target_kind
        and source.endpoint_url == planned.endpoint_url
        and source.remote_name == planned.remote_name
        and source.source_format == planned.source_format
        and source.sync_strategy == planned.sync_strategy
        and source.config_json == planned.config_json
        and source.definition_sha256 == planned.definition_sha256
        and source.priority == planned.priority
        and _canonical_sha256(definition) == planned.definition_sha256
    )


def _live_lease_predicates(
    lease: SyncRunLease,
    moment: datetime,
) -> tuple[Any, ...]:
    return (
        ReferenceSyncRun.id == lease.run_id,
        ReferenceSyncRun.source_id == lease.source_id,
        ReferenceSyncRun.attempt_no == lease.attempt_no,
        ReferenceSyncRun.status == "running",
        ReferenceSyncRun.lease_token == lease.token,
        ReferenceSyncRun.lease_expires_at > moment,
    )


def _validate_lease(lease: SyncRunLease) -> None:
    if (
        not isinstance(lease, SyncRunLease)
        or lease.run_id <= 0
        or lease.source_id <= 0
        or lease.attempt_no <= 0
        or not isinstance(lease.token, str)
        or not _LEASE_TOKEN_RE.fullmatch(lease.token)
    ):
        raise MirrorLifecycleError("sync-run lease identity is invalid")


def _validate_lease_seconds(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 3600:
        raise MirrorLifecycleError(
            "lease duration must be between 30 and 3600 seconds"
        )


def _validate_expected_generation(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MirrorPromotionConflict("expected generation must be non-negative")


def _bounded_required_text(
    value: str | None,
    field: str,
    maximum: int,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MirrorLifecycleError(f"{field} must be non-empty and bounded")
    return value.strip()


def _lock_source_provider(db: Session, provider_key: str) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _advisory_key(_MIRROR_SOURCE_LOCK_DOMAIN, provider_key)},
    )


def _lock_delivery_layer(
    db: Session,
    provider_key: str,
    layer_id: int,
) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {
            "lock_key": _advisory_key(
                _MIRROR_LAYER_LOCK_DOMAIN,
                f"{provider_key}\0{layer_id}",
            )
        },
    )


def _advisory_key(domain: bytes, identity: str) -> int:
    digest = hashlib.sha256(domain + identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _display_source_key(layer_id: int, source_key: str) -> str:
    return f"{layer_id}:{source_key}"


def _moment(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise MirrorLifecycleError("timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _utc_isoformat(value: datetime) -> str:
    return _moment(value).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise MirrorLifecycleError("value is not canonical JSON") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
