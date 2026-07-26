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
from typing import Any, Callable, Literal, Protocol

from sqlalchemy import and_, case, exists, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.reference_layers.catalog import (
    canonical_normalized_definition_sha256,
)
from app.reference_layers.local_metadata_contract import (
    LOCAL_METADATA_ASSET_KEY,
    local_metadata_asset_descriptor,
    local_metadata_gate_matches,
)
from app.reference_layers.mirror_strategy import (
    apply_mirror_strategy_plan,
    build_mirror_strategy_plan,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_current_source_authorization,
    require_version_local_service_authorization,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryStyleParity,
    ReferenceDeliveryStyleResource,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceService,
    ReferenceStyleParityPlan,
    ReferenceStyleParityPlanItem,
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
BOOTSTRAP_PLAN_SCHEMA = "siur-mirror-source-bootstrap-v3"
PROMOTION_EVENT_SCHEMA = "siur-mirror-promotion-event-v1"
DEFAULT_SOURCE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_SOURCE_FULL_REFRESH_INTERVAL_SECONDS = 30 * 24 * 60 * 60
# A daily deterministic pixel probe catches changes at stable capabilities
# endpoints.  This weekly full rebuild is the durable upper bound for changes
# outside that sample, so a tile mirror is never trusted unchanged for 30 days.
TILE_SOURCE_FULL_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60
_MIRROR_SOURCE_LOCK_DOMAIN = b"asistente/reference-mirror-sources/v1\0"
_MIRROR_LAYER_LOCK_DOMAIN = b"asistente/reference-mirror-layer/v1\0"
_PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LEASE_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ETAG_LENGTH = 4_096
_MAX_VERSION_LENGTH = 2_048
_MAX_ERROR_SUMMARY_LENGTH = 4_096
_MAX_STATS_JSON_BYTES = 1_048_576
_MANUAL_SYNC_AUDIT_KEY = "manual_enqueue"

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
    is_primary: bool
    check_interval_seconds: int
    full_refresh_interval_seconds: int

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
            "is_primary": self.is_primary,
            "check_interval_seconds": self.check_interval_seconds,
            "full_refresh_interval_seconds": (
                self.full_refresh_interval_seconds
            ),
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
class ManualSyncEnqueueResult:
    provider_key: str
    layer_id: int
    source_id: int
    source_definition_sha256: str
    expected_generation: int
    check_mode: Literal["conditional", "full"]
    requested_by_id: int
    reason: str
    run_id: int | None


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


@dataclass(frozen=True)
class DeliveryTransitionPreview:
    provider_key: str
    layer_id: int
    action: Literal["rollback", "reactivate"]
    from_version_id: int | None
    to_version_id: int
    expected_generation: int
    resulting_generation: int


@dataclass(frozen=True)
class LocalMetadataTransitionVerification:
    asset_id: int
    document_sha256: str
    document_size_bytes: int


class LocalMetadataTransitionVerifier(Protocol):
    def __call__(
        self,
        db: Session,
        version: ReferenceDeliveryVersion,
    ) -> LocalMetadataTransitionVerification: ...


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
            continue
        try:
            candidates = acquisition_candidates(
                _service_definition(service),
                _layer_definition(layer),
            )
        except SourceDiscoveryError:
            continue
        if not candidates:
            continue
        preferred_source_key = min(
            candidates,
            key=lambda item: (item.priority, item.source_key),
        ).source_key
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
                    is_primary=candidate.source_key == preferred_source_key,
                    check_interval_seconds=(
                        DEFAULT_SOURCE_CHECK_INTERVAL_SECONDS
                    ),
                    full_refresh_interval_seconds=(
                        TILE_SOURCE_FULL_REFRESH_INTERVAL_SECONDS
                        if candidate.target_kind == "tiles"
                        else DEFAULT_SOURCE_FULL_REFRESH_INTERVAL_SECONDS
                    ),
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
    administratively_disabled_layer_ids = set(
        db.scalars(
            select(ReferenceLayerDeliveryState.layer_id).where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.status == "disabled",
            )
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
        elif not _source_matches_plan(
            record,
            item,
            expected_enabled=(
                item.layer_id not in administratively_disabled_layer_ids
            ),
        ):
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
        administratively_disabled_layer_ids = set(
            db.scalars(
                select(ReferenceLayerDeliveryState.layer_id).where(
                    ReferenceLayerDeliveryState.provider_key
                    == reviewed_plan.provider_key,
                    ReferenceLayerDeliveryState.status == "disabled",
                )
            )
        )
        planned_keys: set[tuple[int, str]] = set()
        requires_update: set[tuple[int, str]] = set()
        for item in reviewed_plan.sources:
            key = (item.layer_id, item.source_key)
            record = existing.get(key)
            if record is not None and not _source_matches_plan(
                record,
                item,
                expected_enabled=(
                    item.layer_id
                    not in administratively_disabled_layer_ids
                ),
            ):
                requires_update.add(key)

        planned_layer_ids = {item.layer_id for item in reviewed_plan.sources}
        if planned_layer_ids:
            db.execute(
                update(ReferenceLayerSource)
                .where(
                    ReferenceLayerSource.provider_key
                    == reviewed_plan.provider_key,
                    ReferenceLayerSource.layer_id.in_(planned_layer_ids),
                    ReferenceLayerSource.is_primary.is_(True),
                )
                .values(is_primary=False),
                execution_options={"synchronize_session": "fetch"},
            )
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
                        enabled=(
                            item.layer_id
                            not in administratively_disabled_layer_ids
                        ),
                        is_primary=(
                            item.is_primary
                            and item.layer_id
                            not in administratively_disabled_layer_ids
                        ),
                        priority=item.priority,
                        check_interval_seconds=item.check_interval_seconds,
                        full_refresh_interval_seconds=(
                            item.full_refresh_interval_seconds
                        ),
                    )
                )
                created += 1
            else:
                expected_enabled = (
                    item.layer_id not in administratively_disabled_layer_ids
                )
                if key in requires_update:
                    record.protocol = item.protocol
                    record.target_kind = item.target_kind
                    record.endpoint_url = item.endpoint_url
                    record.remote_name = item.remote_name
                    record.source_format = item.source_format
                    record.sync_strategy = item.sync_strategy
                    record.config_json = item.config_json
                    record.definition_sha256 = item.definition_sha256
                    record.enabled = expected_enabled
                    record.priority = item.priority
                    record.check_interval_seconds = (
                        item.check_interval_seconds
                    )
                    record.full_refresh_interval_seconds = (
                        item.full_refresh_interval_seconds
                    )
                    updated_count += 1
                record.is_primary = item.is_primary and expected_enabled

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
        strategy_plan = build_mirror_strategy_plan(
            db,
            provider_key=reviewed_plan.provider_key,
        )
        apply_mirror_strategy_plan(db, strategy_plan)
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
    """Queue only preferred sources, with at most one open run per layer."""

    moment = _moment(now)
    if not 1 <= limit <= 10_000:
        raise MirrorLifecycleError("enqueue limit must be between 1 and 10000")
    open_run = exists(
        select(ReferenceSyncRun.id).where(
            ReferenceSyncRun.provider_key
            == ReferenceLayerSource.provider_key,
            ReferenceSyncRun.layer_id == ReferenceLayerSource.layer_id,
            ReferenceSyncRun.status.in_(("queued", "running")),
        )
    )
    administratively_disabled = exists(
        select(ReferenceLayerDeliveryState.layer_id).where(
            ReferenceLayerDeliveryState.provider_key
            == ReferenceLayerSource.provider_key,
            ReferenceLayerDeliveryState.layer_id
            == ReferenceLayerSource.layer_id,
            ReferenceLayerDeliveryState.status == "disabled",
        )
    )
    try:
        candidate_ids = list(
            db.scalars(
                select(ReferenceLayerSource.id)
                .where(
                    ReferenceLayerSource.enabled.is_(True),
                    ReferenceLayerSource.is_primary.is_(True),
                    ReferenceLayerSource.sync_strategy != "manual",
                    ReferenceLayerSource.next_check_at <= moment,
                    ~open_run,
                    ~administratively_disabled,
                )
                .order_by(
                    ReferenceLayerSource.next_check_at,
                    ReferenceLayerSource.id,
                )
                .limit(limit)
            )
        )
        run_ids: list[int] = []
        for source_id in candidate_ids:
            preliminary = db.get(ReferenceLayerSource, source_id)
            if preliminary is None:
                continue
            _lock_delivery_layer(
                db,
                preliminary.provider_key,
                preliminary.layer_id,
            )
            source = db.scalar(
                select(ReferenceLayerSource)
                .where(ReferenceLayerSource.id == source_id)
                .with_for_update()
            )
            if source is None or (
                source.provider_key != preliminary.provider_key
                or source.layer_id != preliminary.layer_id
                or not source.enabled
                or not source.is_primary
                or source.sync_strategy == "manual"
                or source.next_check_at > moment
            ):
                continue
            state = db.scalar(
                select(ReferenceLayerDeliveryState)
                .where(
                    ReferenceLayerDeliveryState.provider_key
                    == source.provider_key,
                    ReferenceLayerDeliveryState.layer_id == source.layer_id,
                )
                .with_for_update()
            )
            if state is not None and state.status == "disabled":
                continue
            layer_has_open_run = bool(
                db.scalar(
                    select(
                        exists().where(
                            ReferenceSyncRun.provider_key
                            == source.provider_key,
                            ReferenceSyncRun.layer_id == source.layer_id,
                            ReferenceSyncRun.status.in_(("queued", "running")),
                        )
                    )
                )
            )
            if layer_has_open_run:
                continue
            definition = _stored_source_definition(source)
            if _canonical_sha256(definition) != source.definition_sha256:
                raise MirrorLifecycleError(
                    f"source {source.id} definition hash is invalid"
                )
            run = ReferenceSyncRun(
                provider_key=source.provider_key,
                layer_id=source.layer_id,
                source_id=source.id,
                parent_run_id=None,
                fallback_depth=0,
                source_definition_json=definition,
                source_definition_sha256=source.definition_sha256,
                trigger_kind="scheduled",
                check_mode=_check_mode_for_source(db, source, moment),
                status="queued",
                attempt_no=1,
                expected_active_generation=(
                    state.generation if state is not None else 0
                ),
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


def preview_manual_sync_run(
    db: Session,
    *,
    provider_key: str,
    source_id: int,
    expected_source_definition_sha256: str,
    expected_generation: int,
    check_mode: Literal["conditional", "full"],
    requested_by_id: int,
    reason: str,
) -> ManualSyncEnqueueResult:
    """Validate one exact operator-selected source without queuing it."""

    try:
        arguments = _validated_manual_sync_arguments(
            provider_key=provider_key,
            source_id=source_id,
            expected_source_definition_sha256=(
                expected_source_definition_sha256
            ),
            expected_generation=expected_generation,
            check_mode=check_mode,
            requested_by_id=requested_by_id,
            reason=reason,
        )
        source = _lock_and_validate_manual_sync_source(db, **arguments)
        return _manual_sync_result(source, run_id=None, **arguments)
    finally:
        db.rollback()


def enqueue_manual_sync_run(
    db: Session,
    *,
    provider_key: str,
    source_id: int,
    expected_source_definition_sha256: str,
    expected_generation: int,
    check_mode: Literal["conditional", "full"],
    requested_by_id: int,
    reason: str,
    now: datetime | None = None,
) -> ManualSyncEnqueueResult:
    """Queue one exact reviewed source, independent of its due timestamp."""

    try:
        arguments = _validated_manual_sync_arguments(
            provider_key=provider_key,
            source_id=source_id,
            expected_source_definition_sha256=(
                expected_source_definition_sha256
            ),
            expected_generation=expected_generation,
            check_mode=check_mode,
            requested_by_id=requested_by_id,
            reason=reason,
        )
        source = _lock_and_validate_manual_sync_source(db, **arguments)
        run = ReferenceSyncRun(
            provider_key=source.provider_key,
            layer_id=source.layer_id,
            source_id=source.id,
            parent_run_id=None,
            fallback_depth=0,
            requested_by_id=requested_by_id,
            source_definition_json=_stored_source_definition(source),
            source_definition_sha256=source.definition_sha256,
            trigger_kind="manual",
            check_mode=check_mode,
            status="queued",
            attempt_no=1,
            expected_active_generation=expected_generation,
            queued_at=_moment(now),
            stats_json={
                _MANUAL_SYNC_AUDIT_KEY: {
                    "reason": reason,
                    "requested_by_id": requested_by_id,
                }
            },
        )
        db.add(run)
        db.flush()
        result = _manual_sync_result(
            source,
            run_id=run.id,
            **arguments,
        )
        db.commit()
        return result
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

    _validate_lease(lease)
    _validate_lease_seconds(lease_seconds)
    try:
        run = _lock_leased_run(db, lease)
        moment = _fresh_lease_moment(db, now)
        _require_live_lease(run, lease, moment)
        expires_at = moment + timedelta(seconds=lease_seconds)
        run.heartbeat_at = moment
        run.lease_expires_at = expires_at
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

    _validate_lease(lease)
    if outcome not in {"unchanged", "succeeded", "rejected", "failed"}:
        raise MirrorLifecycleError("unsupported sync-run outcome")
    if outcome in {"rejected", "failed"}:
        error_code = _bounded_required_text(error_code, "error_code", 64)
        error_summary = _bounded_optional_text(
            error_summary,
            "error_summary",
            _MAX_ERROR_SUMMARY_LENGTH,
            strip=True,
        )
    else:
        error_code = None
        error_summary = None
    observed_etag = _bounded_optional_text(
        observed_etag,
        "observed_etag",
        _MAX_ETAG_LENGTH,
    )
    observed_version = _bounded_optional_text(
        observed_version,
        "observed_version",
        _MAX_VERSION_LENGTH,
        strip=True,
    )
    if observed_manifest_sha256 is not None and not _SHA256_RE.fullmatch(
        observed_manifest_sha256
    ):
        raise MirrorLifecycleError("observed manifest hash is invalid")
    if observed_last_modified is not None:
        observed_last_modified = _moment(observed_last_modified)
    stats = _bounded_json_object(
        stats_json,
        "stats_json",
        _MAX_STATS_JSON_BYTES,
    )
    try:
        state: ReferenceLayerDeliveryState | None = None
        layer_sources: list[ReferenceLayerSource] = []
        preliminary_source: ReferenceLayerSource | None = None
        if outcome in {"rejected", "failed"}:
            preliminary_source = db.get(ReferenceLayerSource, lease.source_id)
            if preliminary_source is None:
                raise MirrorLeaseLostError(
                    "sync-run lease source is absent"
                )
            _lock_delivery_layer(
                db,
                preliminary_source.provider_key,
                preliminary_source.layer_id,
            )
        run = _lock_leased_run(db, lease)
        if outcome in {"rejected", "failed"}:
            if (
                preliminary_source is None
                or run.provider_key != preliminary_source.provider_key
                or run.layer_id != preliminary_source.layer_id
            ):
                raise MirrorLifecycleError(
                    "sync-run layer identity is inconsistent"
                )
            state, layer_sources = _lock_fallback_context(
                db,
                provider_key=run.provider_key,
                layer_id=run.layer_id,
            )
        moment = _fresh_lease_moment(db, now)
        _require_live_lease(run, lease, moment)
        run.status = outcome
        run.finished_at = moment
        run.lease_token = None
        run.lease_expires_at = None
        run.observed_etag = observed_etag
        run.observed_last_modified = observed_last_modified
        run.observed_version = observed_version
        run.observed_manifest_sha256 = observed_manifest_sha256
        run.error_code = error_code
        run.error_summary = error_summary
        run.stats_json = preserve_manual_sync_audit(run, stats)
        if outcome in {"rejected", "failed"}:
            db.flush()
            _enqueue_fallback_for_locked_failure(
                db,
                failed_run=run,
                state=state,
                layer_sources=layer_sources,
                queued_at=moment,
            )
        db.commit()
        return run.id
    except Exception:
        db.rollback()
        raise


def enqueue_fallback_source(
    db: Session,
    *,
    failed_run_id: int,
    now: datetime | None = None,
) -> int | None:
    """Queue the next untried source in one failed run's fallback chain.

    The operation is idempotent for ``failed_run_id``.  A concurrent caller
    returns the already-created child, while a stale generation, disabled
    layer, open unrelated run, or exhausted candidate list returns ``None``.
    """

    try:
        preliminary = db.get(ReferenceSyncRun, failed_run_id)
        if preliminary is None:
            raise MirrorLifecycleError("failed sync run does not exist")
        _lock_delivery_layer(
            db,
            preliminary.provider_key,
            preliminary.layer_id,
        )
        failed_run = db.scalar(
            select(ReferenceSyncRun)
            .where(ReferenceSyncRun.id == failed_run_id)
            .with_for_update()
        )
        if failed_run is None or (
            failed_run.provider_key != preliminary.provider_key
            or failed_run.layer_id != preliminary.layer_id
        ):
            raise MirrorLifecycleError(
                "failed sync-run layer identity changed"
            )
        if failed_run.status not in {"rejected", "failed"}:
            raise MirrorLifecycleError(
                "fallback requires a rejected or failed sync run"
            )
        state, layer_sources = _lock_fallback_context(
            db,
            provider_key=failed_run.provider_key,
            layer_id=failed_run.layer_id,
        )
        moment = _fresh_lease_moment(db, now)
        fallback_id = _enqueue_fallback_for_locked_failure(
            db,
            failed_run=failed_run,
            state=state,
            layer_sources=layer_sources,
            queued_at=moment,
        )
        db.commit()
        return fallback_id
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
    metadata_verifier: LocalMetadataTransitionVerifier,
    actor_id: int | None = None,
    observed_etag: str | None = None,
    observed_last_modified: datetime | None = None,
    observed_version: str | None = None,
    observed_manifest_sha256: str | None = None,
    stats_json: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> PromotionResult:
    """Atomically publish a validated version and finish its leased run.

    A promoted version must never point at a run that is still ``running``:
    delivery resolution intentionally rejects such versions.  Finalizing the
    run in this transaction also prevents a worker crash between promotion and
    ``finish_sync_run`` from leaving an active but permanently unusable map.
    """

    _validate_lease(lease)
    _validate_expected_generation(expected_generation)
    _require_metadata_transition_verifier(metadata_verifier)
    reason = _bounded_required_text(reason, "reason", 10_000)
    observed_etag = _bounded_optional_text(
        observed_etag,
        "observed_etag",
        _MAX_ETAG_LENGTH,
    )
    observed_version = _bounded_optional_text(
        observed_version,
        "observed_version",
        _MAX_VERSION_LENGTH,
        strip=True,
    )
    if observed_last_modified is not None:
        observed_last_modified = _moment(observed_last_modified)
    if observed_manifest_sha256 is not None and (
        not isinstance(observed_manifest_sha256, str)
        or _SHA256_RE.fullmatch(observed_manifest_sha256) is None
    ):
        raise ValueError("observed_manifest_sha256 must be a lowercase SHA-256")
    stats = _bounded_json_object(
        stats_json,
        "stats_json",
        _MAX_STATS_JSON_BYTES,
    )
    try:
        preliminary_version = db.get(ReferenceDeliveryVersion, version_id)
        if preliminary_version is None:
            raise MirrorPromotionConflict("delivery version does not exist")
        _lock_delivery_layer(
            db,
            preliminary_version.provider_key,
            preliminary_version.layer_id,
        )
        version = db.scalar(
            select(ReferenceDeliveryVersion)
            .where(ReferenceDeliveryVersion.id == version_id)
            .with_for_update()
        )
        if version is None:
            raise MirrorPromotionConflict("delivery version does not exist")
        state, latest = _locked_delivery_state_and_chain(
            db,
            provider_key=version.provider_key,
            layer_id=version.layer_id,
        )
        source = db.scalar(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.id == version.source_id)
            .with_for_update()
        )
        run = _lock_leased_run(db, lease)
        moment = _fresh_lease_moment(db, now)
        _require_live_lease(run, lease, moment)
        if state is not None and state.status == "disabled":
            raise MirrorPromotionConflict(
                "delivery layer is administratively disabled"
            )
        generation = state.generation if state is not None else 0
        if generation != expected_generation:
            raise MirrorPromotionConflict(
                "active delivery generation changed before promotion"
            )
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
        _validate_version_mirror_authorization(
            db,
            version=version,
            source=source,
            run=run,
        )
        _validate_current_version_catalog(db, version)
        metadata_asset = _validate_version_ready(db, version)
        from_version_id = (
            state.active_version_id
            if state is not None and state.status == "active"
            else None
        )
        if from_version_id == version.id:
            raise MirrorPromotionConflict("delivery version is already active")
        _verify_transition_local_metadata(
            db,
            version=version,
            metadata_asset=metadata_asset,
            metadata_verifier=metadata_verifier,
        )
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
        run.status = "succeeded"
        run.finished_at = moment
        run.lease_token = None
        run.lease_expires_at = None
        run.observed_etag = observed_etag
        run.observed_last_modified = observed_last_modified
        run.observed_version = observed_version
        run.observed_manifest_sha256 = observed_manifest_sha256
        run.error_code = None
        run.error_summary = None
        run.stats_json = preserve_manual_sync_audit(run, stats)
        db.commit()
        return _promotion_result(promotion, new_generation)
    except Exception:
        db.rollback()
        raise


def preview_rollback_delivery_version(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
    reason: str,
    metadata_verifier: LocalMetadataTransitionVerifier,
) -> DeliveryTransitionPreview:
    """Validate an operator rollback without appending an event.

    This entrypoint always rolls the transaction back, including advisory and
    row locks.  It is intentionally suitable only for a standalone dry-run;
    callers must not mix unrelated pending changes into the same session.
    """

    _validate_expected_generation(expected_generation)
    _require_metadata_transition_verifier(metadata_verifier)
    _bounded_required_text(reason, "reason", 10_000)
    try:
        state, _, version, metadata_asset = (
            _lock_and_validate_rollback_target(
                db,
                provider_key=provider_key,
                layer_id=layer_id,
                to_version_id=to_version_id,
                expected_generation=expected_generation,
            )
        )
        _verify_transition_local_metadata(
            db,
            version=version,
            metadata_asset=metadata_asset,
            metadata_verifier=metadata_verifier,
        )
        return DeliveryTransitionPreview(
            provider_key=provider_key,
            layer_id=layer_id,
            action="rollback",
            from_version_id=state.active_version_id,
            to_version_id=version.id,
            expected_generation=expected_generation,
            resulting_generation=expected_generation + 1,
        )
    finally:
        db.rollback()


def rollback_delivery_version(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
    reason: str,
    metadata_verifier: LocalMetadataTransitionVerifier,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> PromotionResult:
    """Atomically point an active layer back to a prior immutable version."""

    moment = _moment(now)
    _validate_expected_generation(expected_generation)
    _require_metadata_transition_verifier(metadata_verifier)
    reason = _bounded_required_text(reason, "reason", 10_000)
    try:
        state, latest, version, metadata_asset = (
            _lock_and_validate_rollback_target(
                db,
                provider_key=provider_key,
                layer_id=layer_id,
                to_version_id=to_version_id,
                expected_generation=expected_generation,
            )
        )
        _verify_transition_local_metadata(
            db,
            version=version,
            metadata_asset=metadata_asset,
            metadata_verifier=metadata_verifier,
        )
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
        source_ids = select(ReferenceLayerSource.id).where(
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.layer_id == layer_id,
        )
        db.execute(
            update(ReferenceSyncRun)
            .where(
                ReferenceSyncRun.source_id.in_(source_ids),
                ReferenceSyncRun.status == "queued",
            )
            .values(
                status="cancelled",
                finished_at=moment,
                lease_token=None,
                lease_expires_at=None,
            )
        )
        db.execute(
            update(ReferenceLayerSource)
            .where(
                ReferenceLayerSource.provider_key == provider_key,
                ReferenceLayerSource.layer_id == layer_id,
            )
            .values(enabled=False, is_primary=False)
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


def preview_reactivate_delivery(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
    reason: str,
    metadata_verifier: LocalMetadataTransitionVerifier,
) -> DeliveryTransitionPreview:
    """Validate recovery of a disabled delivery without changing state."""

    _validate_expected_generation(expected_generation)
    _require_metadata_transition_verifier(metadata_verifier)
    _bounded_required_text(reason, "reason", 10_000)
    try:
        _, _, version, _, metadata_asset = (
            _lock_and_validate_reactivation_target(
                db,
                provider_key=provider_key,
                layer_id=layer_id,
                to_version_id=to_version_id,
                expected_generation=expected_generation,
            )
        )
        _verify_transition_local_metadata(
            db,
            version=version,
            metadata_asset=metadata_asset,
            metadata_verifier=metadata_verifier,
        )
        return DeliveryTransitionPreview(
            provider_key=provider_key,
            layer_id=layer_id,
            action="reactivate",
            from_version_id=None,
            to_version_id=version.id,
            expected_generation=expected_generation,
            resulting_generation=expected_generation + 1,
        )
    finally:
        db.rollback()


def reactivate_delivery(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
    reason: str,
    metadata_verifier: LocalMetadataTransitionVerifier,
    actor_id: int | None = None,
    now: datetime | None = None,
) -> PromotionResult:
    """Explicitly reactivate one previously served, still-valid version.

    Deactivation disables every acquisition source.  Reactivation therefore
    revalidates the complete immutable delivery first and enables only the
    source that produced the selected version; a later bootstrap may restore
    other current acquisition candidates.
    """

    moment = _moment(now)
    _validate_expected_generation(expected_generation)
    _require_metadata_transition_verifier(metadata_verifier)
    reason = _bounded_required_text(reason, "reason", 10_000)
    try:
        state, latest, version, source, metadata_asset = (
            _lock_and_validate_reactivation_target(
                db,
                provider_key=provider_key,
                layer_id=layer_id,
                to_version_id=to_version_id,
                expected_generation=expected_generation,
            )
        )
        _verify_transition_local_metadata(
            db,
            version=version,
            metadata_asset=metadata_asset,
            metadata_verifier=metadata_verifier,
        )
        source.enabled = True
        source.is_primary = True
        source.next_check_at = moment
        promotion, generation = _append_promotion(
            db,
            provider_key=provider_key,
            layer_id=layer_id,
            action="reactivate",
            from_version_id=None,
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


def _lock_and_validate_rollback_target(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
) -> tuple[
    ReferenceLayerDeliveryState,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceDeliveryAsset,
]:
    _lock_delivery_layer(db, provider_key, layer_id)
    state, latest = _locked_delivery_state_and_chain(
        db,
        provider_key=provider_key,
        layer_id=layer_id,
    )
    if state is None or latest is None or state.status != "active":
        raise MirrorPromotionConflict("layer has no active delivery")
    if state.generation != expected_generation:
        raise MirrorPromotionConflict(
            "active delivery generation changed before rollback"
        )
    if state.active_version_id == to_version_id:
        raise MirrorPromotionConflict("rollback target is already active")
    version = _previously_active_version(
        db,
        provider_key=provider_key,
        layer_id=layer_id,
        version_id=to_version_id,
        action="rollback",
    )
    _, metadata_asset = _validate_stored_version_servability(
        db,
        version,
    )
    return state, latest, version, metadata_asset


def _lock_and_validate_reactivation_target(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    to_version_id: int,
    expected_generation: int,
) -> tuple[
    ReferenceLayerDeliveryState,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayerSource,
    ReferenceDeliveryAsset,
]:
    _lock_delivery_layer(db, provider_key, layer_id)
    state, latest = _locked_delivery_state_and_chain(
        db,
        provider_key=provider_key,
        layer_id=layer_id,
    )
    if state is None or latest is None or state.status != "disabled":
        raise MirrorPromotionConflict("layer is not disabled")
    if state.generation != expected_generation:
        raise MirrorPromotionConflict(
            "delivery generation changed before reactivation"
        )
    version = _previously_active_version(
        db,
        provider_key=provider_key,
        layer_id=layer_id,
        version_id=to_version_id,
        action="reactivation",
    )
    source, metadata_asset = _validate_stored_version_servability(
        db,
        version,
    )
    return state, latest, version, source, metadata_asset


def _previously_active_version(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    version_id: int,
    action: str,
) -> ReferenceDeliveryVersion:
    version = db.scalar(
        select(ReferenceDeliveryVersion).where(
            ReferenceDeliveryVersion.id == version_id,
            ReferenceDeliveryVersion.provider_key == provider_key,
            ReferenceDeliveryVersion.layer_id == layer_id,
        )
    )
    if version is None:
        raise MirrorPromotionConflict(
            f"{action} target is not a version of this layer"
        )
    was_active = db.scalar(
        select(ReferenceDeliveryPromotion.id)
        .where(
            ReferenceDeliveryPromotion.provider_key == provider_key,
            ReferenceDeliveryPromotion.layer_id == layer_id,
            ReferenceDeliveryPromotion.to_version_id == version.id,
            ReferenceDeliveryPromotion.action.in_(
                ("promote", "rollback", "reactivate")
            ),
        )
        .limit(1)
    )
    if was_active is None:
        raise MirrorPromotionConflict(
            f"{action} target was never an active delivery"
        )
    return version


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


def delivery_state_matches_promotion_head(
    state: ReferenceLayerDeliveryState,
    latest: ReferenceDeliveryPromotion | None,
) -> bool:
    """Return whether mutable delivery state faithfully projects its head."""

    if latest is None or not stored_promotion_hash_is_valid(latest):
        return False
    if (
        state.provider_key != latest.provider_key
        or state.layer_id != latest.layer_id
        or state.last_promotion_id != latest.id
        or state.generation != latest.sequence_number
    ):
        return False
    if latest.action == "deactivate":
        return state.status == "disabled" and state.active_version_id is None
    if latest.action not in {"promote", "rollback", "reactivate"}:
        return False
    return (
        latest.to_version_id is not None
        and state.status == "active"
        and state.active_version_id == latest.to_version_id
    )


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
        if not delivery_state_matches_promotion_head(state, latest):
            raise MirrorPromotionConflict(
                "delivery state does not match the promotion-chain head"
            )
    return state, latest


def _validate_current_version_catalog(
    db: Session,
    version: ReferenceDeliveryVersion,
) -> None:
    row = db.execute(
        select(ReferenceLayer, ReferenceCatalogSnapshot)
        .join(
            ReferenceCatalogSnapshot,
            and_(
                ReferenceCatalogSnapshot.id
                == ReferenceLayer.last_seen_snapshot_id,
                ReferenceCatalogSnapshot.provider_key
                == ReferenceLayer.provider_key,
            ),
        )
        .where(
            ReferenceLayer.id == version.layer_id,
            ReferenceLayer.provider_key == version.provider_key,
            ReferenceLayer.node_type == "layer",
            ReferenceLayer.status.in_(("active", "degraded")),
            ReferenceCatalogSnapshot.id == version.catalog_snapshot_id,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
        .with_for_update(of=ReferenceLayer)
    ).one_or_none()
    if row is None:
        raise MirrorPromotionConflict(
            "delivery version is not bound to the current valid catalog"
        )
    layer, snapshot = row
    if (
        snapshot.definition_sha256 != version.catalog_definition_sha256
        or not stored_catalog_snapshot_is_valid(snapshot)
        or not catalog_snapshot_contains_active_layer(snapshot, layer)
    ):
        raise MirrorPromotionConflict(
            "delivery version is not bound to the current valid catalog"
        )


def _validate_version_ready(
    db: Session,
    version: ReferenceDeliveryVersion,
) -> ReferenceDeliveryAsset:
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
    if (
        _SHA256_RE.fullmatch(version.content_sha256) is None
        or _SHA256_RE.fullmatch(version.manifest_sha256) is None
        or _SHA256_RE.fullmatch(primary.sha256) is None
        or primary.sha256 != version.content_sha256
    ):
        raise MirrorPromotionConflict("delivery content hash is invalid")
    metadata_assets = tuple(
        db.scalars(
            select(ReferenceDeliveryAsset).where(
                ReferenceDeliveryAsset.version_id == version.id,
                ReferenceDeliveryAsset.asset_kind == "metadata",
            )
        )
    )
    if len(metadata_assets) != 1:
        raise MirrorPromotionConflict(
            "delivery version has no unique verified metadata asset"
        )
    metadata = metadata_assets[0]
    descriptor = metadata.metadata_json
    binding = (
        descriptor.get("binding")
        if isinstance(descriptor, dict)
        else None
    )
    expected_descriptor = (
        local_metadata_asset_descriptor(
            document_sha256=metadata.sha256,
            document_size_bytes=metadata.size_bytes or 0,
            binding=binding,
        )
        if isinstance(binding, dict)
        else None
    )
    if (
        metadata.asset_key != LOCAL_METADATA_ASSET_KEY
        or metadata.is_primary
        or metadata.storage_backend != "filesystem"
        or metadata.media_type != "application/json"
        or metadata.size_bytes is None
        or metadata.size_bytes < 1
        or descriptor != expected_descriptor
        or not local_metadata_gate_matches(
            version.validation_json,
            document_sha256=metadata.sha256,
            document_size_bytes=metadata.size_bytes,
            descriptor=descriptor,
            binding=binding,
        )
    ):
        raise MirrorPromotionConflict(
            "delivery local metadata precommit gate is invalid"
        )
    _validate_version_style_parity(db, version)
    return metadata


def _require_metadata_transition_verifier(
    metadata_verifier: LocalMetadataTransitionVerifier,
) -> None:
    if not callable(metadata_verifier):
        raise MirrorPromotionConflict(
            "local metadata transition verifier is required"
        )


def _verify_transition_local_metadata(
    db: Session,
    *,
    version: ReferenceDeliveryVersion,
    metadata_asset: ReferenceDeliveryAsset,
    metadata_verifier: LocalMetadataTransitionVerifier,
) -> None:
    try:
        verification = metadata_verifier(db, version)
    except Exception as error:
        raise MirrorPromotionConflict(
            "delivery local metadata failed transition verification"
        ) from error
    if (
        not isinstance(
            verification,
            LocalMetadataTransitionVerification,
        )
        or verification.asset_id != metadata_asset.id
        or verification.document_sha256 != metadata_asset.sha256
        or verification.document_size_bytes
        != metadata_asset.size_bytes
    ):
        raise MirrorPromotionConflict(
            "delivery local metadata transition verification is invalid"
        )


def _validate_version_style_parity(
    db: Session,
    version: ReferenceDeliveryVersion,
) -> None:
    plan = db.scalar(
        select(ReferenceStyleParityPlan).where(
            ReferenceStyleParityPlan.source_id == version.source_id,
            ReferenceStyleParityPlan.sync_run_id == version.sync_run_id,
        )
    )
    if (
        plan is None
        or not plan.complete
        or plan.missing_style_count != 0
        or plan.provider_key != version.provider_key
        or plan.layer_id != version.layer_id
        or plan.catalog_snapshot_id != version.catalog_snapshot_id
        or plan.catalog_definition_sha256
        != version.catalog_definition_sha256
        or plan.delivery_kind != version.delivery_kind
        or _canonical_sha256(plan.evidence_json) != plan.evidence_sha256
    ):
        raise MirrorPromotionConflict(
            "delivery style parity plan is absent or incomplete"
        )
    items = tuple(
        db.scalars(
            select(ReferenceStyleParityPlanItem)
            .where(ReferenceStyleParityPlanItem.plan_id == plan.id)
            .order_by(ReferenceStyleParityPlanItem.id)
        )
    )
    parities = tuple(
        db.scalars(
            select(ReferenceDeliveryStyleParity)
            .join(
                ReferenceStyleParityPlanItem,
                ReferenceStyleParityPlanItem.id
                == ReferenceDeliveryStyleParity.plan_item_id,
            )
            .where(
                ReferenceDeliveryStyleParity.version_id == version.id,
                ReferenceStyleParityPlanItem.plan_id == plan.id,
            )
            .order_by(ReferenceDeliveryStyleParity.id)
        )
    )
    if (
        len(items) != plan.required_style_count
        or len(parities) != len(items)
        or {item.id for item in items}
        != {parity.plan_item_id for parity in parities}
        or any(
            item.parity_kind == "missing"
            or not item.verified
            or _canonical_sha256(item.evidence_json)
            != item.evidence_sha256
            for item in items
        )
    ):
        raise MirrorPromotionConflict(
            "delivery style parity coverage is partial"
        )
    assets = {
        asset.id: asset
        for asset in db.scalars(
            select(ReferenceDeliveryAsset).where(
                ReferenceDeliveryAsset.version_id == version.id
            )
        )
    }
    resource_counts = {
        parity_id: count
        for parity_id, count in db.execute(
            select(
                ReferenceDeliveryStyleResource.delivery_parity_id,
                func.count(),
            )
            .where(
                ReferenceDeliveryStyleResource.delivery_parity_id.in_(
                    [item.id for item in parities]
                )
            )
            .group_by(
                ReferenceDeliveryStyleResource.delivery_parity_id
            )
        )
    }
    item_by_id = {item.id: item for item in items}
    for parity in parities:
        item = item_by_id[parity.plan_item_id]
        asset = assets.get(parity.delivery_asset_id)
        expected_kind = {
            "exact": "style_sld",
            "adapted": "style_package",
            "baked": "tile_archive",
        }.get(item.parity_kind)
        if (
            not parity.verified
            or parity.parity_kind != item.parity_kind
            or asset is None
            or asset.asset_kind != expected_kind
            or parity.resource_count
            != resource_counts.get(parity.id, 0)
            or parity.resource_count != item.resource_count
            or _canonical_sha256(parity.evidence_json)
            != parity.evidence_sha256
        ):
            raise MirrorPromotionConflict(
                "delivery style parity evidence is invalid"
            )
    gate = version.validation_json.get("style_parity_gate")
    if (
        not isinstance(gate, dict)
        or gate.get("schema_version")
        != "reference-delivery-style-parity-gate/v1"
        or gate.get("passed") is not True
        or gate.get("plan_id") != plan.id
        or gate.get("plan_evidence_sha256") != plan.evidence_sha256
        or gate.get("required_style_count") != len(items)
        or gate.get("verified_style_count") != len(items)
        or gate.get("missing_style_count") != 0
    ):
        raise MirrorPromotionConflict(
            "delivery validation omits its style parity gate"
        )


def _validate_stored_version_servability(
    db: Session,
    version: ReferenceDeliveryVersion,
) -> tuple[ReferenceLayerSource, ReferenceDeliveryAsset]:
    source = db.scalar(
        select(ReferenceLayerSource)
        .where(ReferenceLayerSource.id == version.source_id)
        .with_for_update()
    )
    if source is None or (
        source.provider_key != version.provider_key
        or source.layer_id != version.layer_id
    ):
        raise MirrorPromotionConflict("delivery source identity is invalid")
    run = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.id == version.sync_run_id,
            ReferenceSyncRun.source_id == version.source_id,
        )
    )
    if run is None or run.status != "succeeded":
        raise MirrorPromotionConflict(
            "delivery sync run did not finish successfully"
        )
    if not sync_run_source_definition_is_valid(run):
        raise MirrorPromotionConflict(
            "delivery sync-run source-definition hash is invalid"
        )
    if (
        run.provider_key != version.provider_key
        or run.layer_id != version.layer_id
        or not isinstance(run.source_definition_json, dict)
        or run.source_definition_json.get("target_kind")
        != version.delivery_kind
    ):
        raise MirrorPromotionConflict(
            "delivery sync-run identity is invalid"
        )
    layer, current_snapshot = _locked_current_layer_catalog(
        db,
        provider_key=version.provider_key,
        layer_id=version.layer_id,
    )
    frozen_snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.id == version.catalog_snapshot_id,
            ReferenceCatalogSnapshot.provider_key == version.provider_key,
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if (
        frozen_snapshot is None
        or frozen_snapshot.definition_sha256
        != version.catalog_definition_sha256
        or not stored_catalog_snapshot_is_valid(frozen_snapshot)
        or not catalog_snapshot_contains_active_layer(
            frozen_snapshot,
            layer,
        )
        or not stored_catalog_snapshot_is_valid(current_snapshot)
        or not catalog_snapshot_contains_active_layer(
            current_snapshot,
            layer,
        )
    ):
        raise MirrorPromotionConflict(
            "delivery version catalog evidence is invalid"
        )
    _validate_version_mirror_authorization(
        db,
        version=version,
        source=source,
        run=run,
    )
    metadata_asset = _validate_version_ready(db, version)
    return source, metadata_asset


def _validate_version_mirror_authorization(
    db: Session,
    *,
    version: ReferenceDeliveryVersion,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
) -> None:
    try:
        require_version_local_service_authorization(
            db,
            version=version,
            source=source,
            run=run,
        )
    except MirrorAuthorizationError as error:
        raise MirrorPromotionConflict(
            f"mirror authorization rejected delivery transition: {error.code}"
        ) from error


def _locked_current_layer_catalog(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
) -> tuple[ReferenceLayer, ReferenceCatalogSnapshot]:
    row = db.execute(
        select(ReferenceLayer, ReferenceCatalogSnapshot)
        .join(
            ReferenceCatalogSnapshot,
            and_(
                ReferenceCatalogSnapshot.id
                == ReferenceLayer.last_seen_snapshot_id,
                ReferenceCatalogSnapshot.provider_key
                == ReferenceLayer.provider_key,
            ),
        )
        .where(
            ReferenceLayer.id == layer_id,
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.node_type == "layer",
            ReferenceLayer.status.in_(("active", "degraded")),
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
        .with_for_update(of=ReferenceLayer)
    ).one_or_none()
    if row is None:
        raise MirrorPromotionConflict(
            "delivery layer is not present and active in the current catalog"
        )
    return row


def _validated_manual_sync_arguments(
    *,
    provider_key: str,
    source_id: int,
    expected_source_definition_sha256: str,
    expected_generation: int,
    check_mode: Literal["conditional", "full"],
    requested_by_id: int,
    reason: str,
) -> dict[str, Any]:
    if (
        not isinstance(provider_key, str)
        or _PROVIDER_KEY_RE.fullmatch(provider_key) is None
    ):
        raise MirrorLifecycleError("provider key is invalid")
    if (
        not isinstance(source_id, int)
        or isinstance(source_id, bool)
        or source_id <= 0
    ):
        raise MirrorLifecycleError("source id must be a positive integer")
    if (
        not isinstance(expected_source_definition_sha256, str)
        or _SHA256_RE.fullmatch(expected_source_definition_sha256) is None
    ):
        raise MirrorLifecycleError(
            "expected source-definition hash is invalid"
        )
    _validate_expected_generation(expected_generation)
    if check_mode not in {"conditional", "full"}:
        raise MirrorLifecycleError("manual check mode is invalid")
    if (
        not isinstance(requested_by_id, int)
        or isinstance(requested_by_id, bool)
        or requested_by_id <= 0
    ):
        raise MirrorLifecycleError(
            "requesting actor id must be a positive integer"
        )
    return {
        "provider_key": provider_key,
        "source_id": source_id,
        "expected_source_definition_sha256": (
            expected_source_definition_sha256
        ),
        "expected_generation": expected_generation,
        "check_mode": check_mode,
        "requested_by_id": requested_by_id,
        "reason": _bounded_required_text(reason, "reason", 1_024),
    }


def _lock_and_validate_manual_sync_source(
    db: Session,
    *,
    provider_key: str,
    source_id: int,
    expected_source_definition_sha256: str,
    expected_generation: int,
    check_mode: Literal["conditional", "full"],
    requested_by_id: int,
    reason: str,
) -> ReferenceLayerSource:
    del check_mode, requested_by_id, reason
    preliminary = db.get(ReferenceLayerSource, source_id)
    if preliminary is None or preliminary.provider_key != provider_key:
        raise MirrorLifecycleError(
            "manual sync source is unavailable for this provider"
        )
    _lock_delivery_layer(db, provider_key, preliminary.layer_id)
    source = db.scalar(
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.id == source_id,
            ReferenceLayerSource.provider_key == provider_key,
        )
        .with_for_update()
    )
    if source is None or source.layer_id != preliminary.layer_id:
        raise MirrorLifecycleError("manual sync source identity changed")
    if (
        not source.enabled
        or source.protocol == "local"
        or source.sync_strategy == "manual"
    ):
        raise MirrorLifecycleError(
            "manual sync source is disabled or not worker-supported"
        )
    if (
        source.definition_sha256
        != expected_source_definition_sha256
        or not stored_source_definition_is_valid(source)
    ):
        raise MirrorLifecycleError(
            "manual sync source-definition hash is stale or invalid"
        )
    layer, snapshot = _locked_current_layer_catalog(
        db,
        provider_key=provider_key,
        layer_id=source.layer_id,
    )
    if (
        not stored_catalog_snapshot_is_valid(snapshot)
        or not catalog_snapshot_contains_active_layer(snapshot, layer)
    ):
        raise MirrorLifecycleError(
            "manual sync current catalog evidence is invalid"
        )
    state, _ = _locked_delivery_state_and_chain(
        db,
        provider_key=provider_key,
        layer_id=source.layer_id,
    )
    generation = state.generation if state is not None else 0
    if state is not None and state.status == "disabled":
        raise MirrorLifecycleError(
            "manual sync delivery is administratively disabled"
        )
    if generation != expected_generation:
        raise MirrorPromotionConflict(
            "manual sync expected generation is stale"
        )
    layer_has_open_run = bool(
        db.scalar(
            select(
                exists().where(
                    ReferenceSyncRun.provider_key == provider_key,
                    ReferenceSyncRun.layer_id == source.layer_id,
                    ReferenceSyncRun.status.in_(("queued", "running")),
                )
            )
        )
    )
    if layer_has_open_run:
        raise MirrorLifecycleError(
            "manual sync layer already has an open run"
        )
    try:
        require_current_source_authorization(
            db,
            source=source,
            require_acquisition=True,
        )
    except MirrorAuthorizationError as error:
        raise MirrorLifecycleError(
            f"manual sync authorization rejected: {error.code}"
        ) from error
    return source


def _manual_sync_result(
    source: ReferenceLayerSource,
    *,
    provider_key: str,
    source_id: int,
    expected_source_definition_sha256: str,
    expected_generation: int,
    check_mode: Literal["conditional", "full"],
    requested_by_id: int,
    reason: str,
    run_id: int | None,
) -> ManualSyncEnqueueResult:
    if (
        source.provider_key != provider_key
        or source.id != source_id
        or source.definition_sha256
        != expected_source_definition_sha256
    ):
        raise MirrorLifecycleError("manual sync result identity is invalid")
    return ManualSyncEnqueueResult(
        provider_key=provider_key,
        layer_id=source.layer_id,
        source_id=source_id,
        source_definition_sha256=expected_source_definition_sha256,
        expected_generation=expected_generation,
        check_mode=check_mode,
        requested_by_id=requested_by_id,
        reason=reason,
        run_id=run_id,
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


def stored_source_definition_is_valid(source: ReferenceLayerSource) -> bool:
    try:
        return _canonical_sha256(_stored_source_definition(source)) == (
            source.definition_sha256
        )
    except (MirrorLifecycleError, TypeError, ValueError):
        return False


def sync_run_source_definition_is_valid(run: ReferenceSyncRun) -> bool:
    try:
        return _canonical_sha256(run.source_definition_json) == (
            run.source_definition_sha256
        )
    except (MirrorLifecycleError, TypeError, ValueError, RecursionError):
        return False


def stored_catalog_snapshot_is_valid(
    snapshot: ReferenceCatalogSnapshot,
) -> bool:
    """Validate one immutable catalog snapshot without requiring it be current."""

    try:
        normalized = snapshot.normalized_definition_json
        raw_catalog = snapshot.raw_catalog_json
        if (
            snapshot.status != "applied"
            or not isinstance(normalized, dict)
            or not isinstance(raw_catalog, dict)
            or normalized.get("provider_key") != snapshot.provider_key
            or normalized.get("source_url") != snapshot.source_url
            or normalized.get("unresolved_count") != snapshot.unresolved_count
        ):
            return False
        services = normalized.get("services")
        layers = normalized.get("layers")
        if not isinstance(services, list) or not isinstance(layers, list):
            return False
        if (
            len(services) != snapshot.service_count
            or sum(
                isinstance(item, dict) and item.get("node_type") == "group"
                for item in layers
            )
            != snapshot.group_count
            or sum(
                isinstance(item, dict) and item.get("node_type") == "layer"
                for item in layers
            )
            != snapshot.layer_count
        ):
            return False
        return (
            _canonical_sha256(raw_catalog) == snapshot.content_sha256
            and canonical_normalized_definition_sha256(normalized)
            == snapshot.definition_sha256
        )
    except (
        MirrorLifecycleError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        return False


def catalog_snapshot_contains_active_layer(
    snapshot: ReferenceCatalogSnapshot,
    layer: ReferenceLayer,
) -> bool:
    """Return whether a valid snapshot froze this logical layer as deliverable."""

    try:
        if (
            snapshot.provider_key != layer.provider_key
            or not stored_catalog_snapshot_is_valid(snapshot)
        ):
            return False
        layers = snapshot.normalized_definition_json.get("layers")
        if not isinstance(layers, list):
            return False
        matches = [
            item
            for item in layers
            if isinstance(item, dict)
            and item.get("source_key") == layer.source_key
        ]
        return (
            len(matches) == 1
            and matches[0].get("node_type") == "layer"
            and matches[0].get("status") in {"active", "degraded"}
        )
    except (TypeError, ValueError, RecursionError):
        return False


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
    *,
    expected_enabled: bool = True,
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
        source.enabled == expected_enabled
        and source.is_primary == (planned.is_primary and expected_enabled)
        and source.protocol == planned.protocol
        and source.target_kind == planned.target_kind
        and source.endpoint_url == planned.endpoint_url
        and source.remote_name == planned.remote_name
        and source.source_format == planned.source_format
        and source.sync_strategy == planned.sync_strategy
        and source.config_json == planned.config_json
        and source.definition_sha256 == planned.definition_sha256
        and source.priority == planned.priority
        and source.check_interval_seconds == planned.check_interval_seconds
        and source.full_refresh_interval_seconds
        == planned.full_refresh_interval_seconds
        and _canonical_sha256(definition) == planned.definition_sha256
    )


def _lock_fallback_context(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
) -> tuple[
    ReferenceLayerDeliveryState | None,
    list[ReferenceLayerSource],
]:
    state = db.scalar(
        select(ReferenceLayerDeliveryState)
        .where(
            ReferenceLayerDeliveryState.provider_key == provider_key,
            ReferenceLayerDeliveryState.layer_id == layer_id,
        )
        .with_for_update()
    )
    sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(
                ReferenceLayerSource.provider_key == provider_key,
                ReferenceLayerSource.layer_id == layer_id,
            )
            .order_by(
                ReferenceLayerSource.priority,
                ReferenceLayerSource.source_key,
                ReferenceLayerSource.id,
            )
            .with_for_update()
        )
    )
    return state, sources


def _enqueue_fallback_for_locked_failure(
    db: Session,
    *,
    failed_run: ReferenceSyncRun,
    state: ReferenceLayerDeliveryState | None,
    layer_sources: list[ReferenceLayerSource],
    queued_at: datetime,
) -> int | None:
    if failed_run.status not in {"rejected", "failed"}:
        raise MirrorLifecycleError(
            "fallback requires a rejected or failed sync run"
        )
    if failed_run.trigger_kind == "manual":
        return None
    if state is not None and state.status == "disabled":
        return None
    active_generation = state.generation if state is not None else 0
    if active_generation != failed_run.expected_active_generation:
        return None
    if state is not None and state.status != "active":
        return None

    existing_child = db.scalar(
        select(ReferenceSyncRun)
        .where(ReferenceSyncRun.parent_run_id == failed_run.id)
        .with_for_update()
    )
    if existing_child is not None:
        if (
            existing_child.provider_key != failed_run.provider_key
            or existing_child.layer_id != failed_run.layer_id
            or existing_child.fallback_depth != failed_run.fallback_depth + 1
        ):
            raise MirrorLifecycleError("fallback child lineage is invalid")
        return existing_child.id

    attempted_source_ids = _fallback_attempted_source_ids(db, failed_run)
    source_by_id = {source.id: source for source in layer_sources}
    failed_source = source_by_id.get(failed_run.source_id)
    if failed_source is None or (
        failed_source.provider_key != failed_run.provider_key
        or failed_source.layer_id != failed_run.layer_id
    ):
        raise MirrorLifecycleError("failed run source identity is invalid")

    layer_has_open_run = bool(
        db.scalar(
            select(
                exists().where(
                    ReferenceSyncRun.provider_key == failed_run.provider_key,
                    ReferenceSyncRun.layer_id == failed_run.layer_id,
                    ReferenceSyncRun.status.in_(("queued", "running")),
                )
            )
        )
    )
    if layer_has_open_run:
        return None

    candidates = [
        source
        for source in layer_sources
        if source.enabled
        and source.sync_strategy != "manual"
        and source.id not in attempted_source_ids
    ]
    candidate = next(
        (
            source
            for source in candidates
            if stored_source_definition_is_valid(source)
        ),
        None,
    )
    if candidate is None:
        return None
    definition = _stored_source_definition(candidate)
    child = ReferenceSyncRun(
        provider_key=candidate.provider_key,
        layer_id=candidate.layer_id,
        source_id=candidate.id,
        parent_run_id=failed_run.id,
        fallback_depth=failed_run.fallback_depth + 1,
        source_definition_json=definition,
        source_definition_sha256=candidate.definition_sha256,
        trigger_kind="retry",
        check_mode=_check_mode_for_source(db, candidate, queued_at),
        status="queued",
        attempt_no=1,
        expected_active_generation=failed_run.expected_active_generation,
        queued_at=queued_at,
        stats_json={},
    )
    db.add(child)
    db.flush()
    return child.id


def _fallback_attempted_source_ids(
    db: Session,
    failed_run: ReferenceSyncRun,
) -> set[int]:
    attempted: set[int] = set()
    seen_run_ids: set[int] = set()
    cursor = failed_run
    while True:
        if cursor.id in seen_run_ids:
            raise MirrorLifecycleError("fallback chain contains a cycle")
        seen_run_ids.add(cursor.id)
        if (
            cursor.provider_key != failed_run.provider_key
            or cursor.layer_id != failed_run.layer_id
            or cursor.status not in {"rejected", "failed"}
        ):
            raise MirrorLifecycleError("fallback chain lineage is invalid")
        attempted.add(cursor.source_id)
        if cursor.parent_run_id is None:
            if cursor.fallback_depth != 0:
                raise MirrorLifecycleError("fallback root depth is invalid")
            return attempted
        parent = db.scalar(
            select(ReferenceSyncRun)
            .where(ReferenceSyncRun.id == cursor.parent_run_id)
            .with_for_update()
        )
        if parent is None or cursor.fallback_depth != parent.fallback_depth + 1:
            raise MirrorLifecycleError("fallback parent lineage is invalid")
        cursor = parent


def _check_mode_for_source(
    db: Session,
    source: ReferenceLayerSource,
    moment: datetime,
) -> str:
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
    return (
        "full"
        if latest_full is None or latest_full <= full_cutoff
        else "conditional"
    )


def _lock_leased_run(
    db: Session,
    lease: SyncRunLease,
) -> ReferenceSyncRun:
    run = db.scalar(
        select(ReferenceSyncRun)
        .where(
            ReferenceSyncRun.id == lease.run_id,
            ReferenceSyncRun.source_id == lease.source_id,
            ReferenceSyncRun.attempt_no == lease.attempt_no,
        )
        .with_for_update()
    )
    if run is None:
        raise MirrorLeaseLostError("sync-run lease is absent or expired")
    return run


def _require_live_lease(
    run: ReferenceSyncRun,
    lease: SyncRunLease,
    moment: datetime,
) -> None:
    expires_at = run.lease_expires_at
    if (
        run.status != "running"
        or run.lease_token != lease.token
        or expires_at is None
        or expires_at <= moment
    ):
        raise MirrorLeaseLostError("sync-run lease is absent or expired")


def _fresh_lease_moment(
    db: Session,
    injected: datetime | None,
) -> datetime:
    """Read time only after all transition locks have been acquired."""

    if injected is not None:
        return _moment(injected)
    database_now = db.scalar(select(func.clock_timestamp()))
    if not isinstance(database_now, datetime):
        raise MirrorLifecycleError("database clock is unavailable")
    return _moment(database_now)


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


def _bounded_optional_text(
    value: str | None,
    field: str,
    maximum: int,
    *,
    strip: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise MirrorLifecycleError(f"{field} must be bounded text")
    normalized = value.strip() if strip else value
    return normalized or None


def _bounded_json_object(
    value: dict[str, Any] | None,
    field: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MirrorLifecycleError(f"{field} must be a JSON object")
    encoded = _canonical_json(value).encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise MirrorLifecycleError(f"{field} is too large")
    return value


def preserve_manual_sync_audit(
    run: ReferenceSyncRun,
    replacement: dict[str, Any],
) -> dict[str, Any]:
    """Keep operator identity/reason immutable across run checkpoints."""

    result = dict(replacement)
    if run.trigger_kind != "manual":
        if _MANUAL_SYNC_AUDIT_KEY in result:
            raise MirrorLifecycleError(
                "manual enqueue audit key is reserved"
            )
        return _bounded_json_object(
            result,
            "stats_json",
            _MAX_STATS_JSON_BYTES,
        )
    existing = (
        run.stats_json.get(_MANUAL_SYNC_AUDIT_KEY)
        if isinstance(run.stats_json, dict)
        else None
    )
    expected = {
        "reason": (
            existing.get("reason")
            if isinstance(existing, dict)
            else None
        ),
        "requested_by_id": (
            existing.get("requested_by_id")
            if isinstance(existing, dict)
            else None
        ),
    }
    if (
        not isinstance(expected["reason"], str)
        or not expected["reason"].strip()
        or len(expected["reason"]) > 1_024
        or not isinstance(expected["requested_by_id"], int)
        or isinstance(expected["requested_by_id"], bool)
        or expected["requested_by_id"] <= 0
        or run.requested_by_id != expected["requested_by_id"]
    ):
        raise MirrorLifecycleError(
            "manual enqueue audit evidence is invalid"
        )
    supplied = result.get(_MANUAL_SYNC_AUDIT_KEY)
    if supplied is not None and supplied != expected:
        raise MirrorLifecycleError(
            "manual enqueue audit evidence cannot be replaced"
        )
    result[_MANUAL_SYNC_AUDIT_KEY] = expected
    return _bounded_json_object(
        result,
        "stats_json",
        _MAX_STATS_JSON_BYTES,
    )


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
