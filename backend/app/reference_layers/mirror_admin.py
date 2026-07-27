"""Read-only SIUR operations report and generation-fenced recovery CLI.

Published delivery history is immutable.  This module deliberately offers no
artifact/blob deletion: only stale, unlocked staging files have an apply mode.
Rollback and reactivation delegate every integrity check and mutation to the
mirror lifecycle instead of maintaining a second transition implementation.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.catalog import (
    canonical_normalized_definition_sha256,
)
from app.reference_layers.delivery_builder import (
    DeliveryBuildError,
    canonical_json_sha256,
)
from app.reference_layers.local_metadata import (
    verify_local_metadata_transition,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_current_source_authorization,
)
from app.reference_layers.mirror_lifecycle import (
    DeliveryTransitionPreview,
    ManualSyncEnqueueResult,
    MirrorLifecycleError,
    PromotionResult,
    delivery_state_matches_promotion_head,
    enqueue_manual_sync_run,
    preview_manual_sync_run,
    preview_reactivate_delivery,
    preview_rollback_delivery_version,
    reactivate_delivery,
    rollback_delivery_version,
    stored_promotion_hash_is_valid,
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
    ReferenceSourceArtifact,
    ReferenceSyncRun,
    ReferenceService,
)
from app.reference_layers.tile_capacity_preflight import (
    TileCapacityPreflightInputError,
    aggregate_tile_capacity_preflight,
    tile_capacity_preflight_exit_code,
)
from app.reference_layers.tile_seed import DEFAULT_PREFLIGHT_SAMPLES
from app.users.models import User


PROVIDER_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$")
PUBLISHED_RETENTION_INVARIANT = (
    "All published versions, delivery assets, acquisition artifacts and "
    "promotion evidence are retained; content-addressed blob deletion is "
    "disabled until a maintenance fence can close the blob/DB commit window."
)


class MirrorAdminInputError(ValueError):
    """An operator request is incomplete or addresses unsafe state."""


def operational_status(
    db: Session,
    *,
    provider_key: str,
    layer_id: int | None = None,
) -> dict[str, Any]:
    """Build a bounded operational projection without changing database state."""

    _validate_provider_key(provider_key)
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if snapshot is None:
        raise MirrorAdminInputError(
            "current applied catalog is unavailable for this provider"
        )
    try:
        snapshot_valid = (
            canonical_normalized_definition_sha256(
                snapshot.normalized_definition_json
            )
            == snapshot.definition_sha256
        )
    except (TypeError, ValueError):
        snapshot_valid = False
    if not snapshot_valid:
        raise MirrorAdminInputError(
            "current catalog definition evidence is invalid"
        )

    layer_query = (
        select(ReferenceLayer)
        .where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.last_seen_snapshot_id == snapshot.id,
            ReferenceLayer.node_type == "layer",
        )
        .order_by(ReferenceLayer.id)
    )
    if layer_id is not None:
        layer_query = layer_query.where(ReferenceLayer.id == layer_id)
    layers = list(db.scalars(layer_query))
    if layer_id is not None and not layers:
        raise MirrorAdminInputError(
            "layer is not a current leaf of this provider"
        )

    layer_ids = [layer.id for layer in layers]
    service_ids = sorted(
        {
            layer.service_id
            for layer in layers
            if layer.service_id is not None
        }
    )
    services = {
        service.id: service
        for service in db.scalars(
            select(ReferenceService).where(
                ReferenceService.provider_key == provider_key,
                ReferenceService.last_seen_snapshot_id == snapshot.id,
                ReferenceService.id.in_(service_ids),
            )
        )
    }
    authorization = _service_authorization(
        db,
        snapshot=snapshot,
        services=services,
    )
    statuses = catalog_mirror_statuses(
        db,
        provider_key=provider_key,
        layers=layers,
    )
    sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(
                ReferenceLayerSource.provider_key == provider_key,
                ReferenceLayerSource.layer_id.in_(layer_ids),
            )
            .order_by(
                ReferenceLayerSource.layer_id,
                ReferenceLayerSource.priority.desc(),
                ReferenceLayerSource.id,
            )
        )
    )
    sources_by_layer: dict[int, list[ReferenceLayerSource]] = defaultdict(list)
    for source in sources:
        sources_by_layer[source.layer_id].append(source)
    latest_source_runs = _latest_runs_by_source(
        db,
        [source.id for source in sources],
    )

    states = {
        state.layer_id: state
        for state in db.scalars(
            select(ReferenceLayerDeliveryState).where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.layer_id.in_(layer_ids),
            )
        )
    }
    promotions_by_layer = _promotions_by_layer(
        db,
        provider_key=provider_key,
        layer_ids=layer_ids,
    )
    version_ids: set[int] = set()
    layer_history: dict[int, dict[str, Any]] = {}
    for layer in layers:
        state = states.get(layer.id)
        promotions = promotions_by_layer.get(layer.id, [])
        chain_valid = _promotion_chain_is_valid(promotions)
        latest = promotions[-1] if promotions else None
        state_valid = bool(
            state is not None
            and chain_valid
            and delivery_state_matches_promotion_head(state, latest)
        )
        active_id = (
            state.active_version_id
            if state_valid and state is not None and state.status == "active"
            else None
        )
        previous_id = _previous_served_version_id(
            promotions if chain_valid else [],
            active_version_id=active_id,
        )
        if active_id is not None:
            version_ids.add(active_id)
        if previous_id is not None:
            version_ids.add(previous_id)
        layer_history[layer.id] = {
            "state": state,
            "state_valid": state_valid,
            "chain_valid": chain_valid,
            "latest": latest,
            "active_id": active_id,
            "previous_id": previous_id,
        }

    versions, version_runs = _versions_and_runs(db, version_ids)
    assets_by_version = _assets_by_version(db, version_ids)
    layer_rows: list[dict[str, Any]] = []
    for layer in layers:
        mirror = statuses[layer.id]
        history = layer_history[layer.id]
        state = history["state"]
        active = _version_summary(
            versions.get(history["active_id"]),
            version_runs.get(history["active_id"]),
            assets_by_version.get(history["active_id"], []),
            current_snapshot_id=snapshot.id,
        )
        previous = _version_summary(
            versions.get(history["previous_id"]),
            version_runs.get(history["previous_id"]),
            assets_by_version.get(history["previous_id"], []),
            current_snapshot_id=snapshot.id,
        )
        service_authorization = authorization.get(
            layer.service_id,
            _missing_service_authorization(),
        )
        recovery = _recovery_candidate(
            state=state,
            state_valid=history["state_valid"],
            active=active,
            previous=previous,
        )
        layer_rows.append(
            {
                "layer_id": layer.id,
                "source_key": layer.source_key,
                "title": layer.title,
                "catalog_status": layer.status,
                "service_id": layer.service_id,
                "mirror_status": mirror.status,
                "mirror_authorization": service_authorization,
                "delivery_state_integrity": {
                    "valid": history["state_valid"],
                    "promotion_chain_valid": history["chain_valid"],
                    "generation": (
                        state.generation if state is not None else None
                    ),
                    "status": state.status if state is not None else None,
                    "last_promotion_id": (
                        state.last_promotion_id
                        if state is not None
                        else None
                    ),
                },
                "active": active,
                "previous": previous,
                "last_run": {
                    "id": mirror.last_run_id,
                    "status": mirror.last_run_status,
                    "started_at": mirror.last_run_started_at,
                    "finished_at": mirror.last_checked_at,
                    "duration_seconds": (
                        mirror.last_run_duration_seconds
                    ),
                    "error_code": mirror.last_error_code,
                    "error_summary": mirror.last_error_summary,
                },
                "next_check_at": mirror.next_check_at,
                "recovery": recovery,
                "sources": [
                    _source_summary(
                        source,
                        latest_source_runs.get(source.id),
                        expected_generation=(
                            state.generation if state is not None else 0
                        ),
                    )
                    for source in sources_by_layer.get(layer.id, [])
                ],
            }
        )

    status_counts = Counter(row["mirror_status"] for row in layer_rows)
    authorization_missing = sum(
        not row["mirror_authorization"]["mirror_authorized"]
        for row in layer_rows
    )
    service_authorizations = list(authorization.values())
    license_status_counts = Counter(
        service.license_status for service in services.values()
    )
    complete_bytes = sum(
        row["active"]["size_bytes"]
        for row in layer_rows
        if row["active"] is not None
        and row["active"]["size_bytes_complete"]
    )
    generated_at = db.scalar(select(func.now()))
    return {
        "ok": True,
        "provider_key": provider_key,
        "catalog_snapshot_id": snapshot.id,
        "catalog_definition_sha256": snapshot.definition_sha256,
        "generated_at": generated_at,
        "summary": {
            "layer_count": len(layer_rows),
            "service_count": len(services),
            "mirror_status_counts": dict(sorted(status_counts.items())),
            "mirror_authorization_missing_count": authorization_missing,
            "mirror_authorization_missing_service_count": sum(
                not item["mirror_authorized"]
                for item in service_authorizations
            ),
            "license_review_service_count": sum(
                item["license_review_id"] is not None
                for item in service_authorizations
            ),
            "delivery_attestation_service_count": sum(
                item["attestation_id"] is not None
                for item in service_authorizations
            ),
            "catalog_license_status_counts": dict(
                sorted(license_status_counts.items())
            ),
            "known_complete_active_bytes": complete_bytes,
            "active_size_incomplete_count": sum(
                row["active"] is not None
                and not row["active"]["size_bytes_complete"]
                for row in layer_rows
            ),
        },
        "retention": _retention_status(db, provider_key=provider_key),
        "layers": layer_rows,
    }


def _service_authorization(
    db: Session,
    *,
    snapshot: ReferenceCatalogSnapshot,
    services: dict[int, ReferenceService],
) -> dict[int, dict[str, Any]]:
    service_ids = list(services)
    if not service_ids:
        return {}
    sources_by_service: dict[int, list[ReferenceLayerSource]] = defaultdict(
        list
    )
    for source, service_id in db.execute(
        select(ReferenceLayerSource, ReferenceLayer.service_id)
        .join(
            ReferenceLayer,
            (ReferenceLayer.id == ReferenceLayerSource.layer_id)
            & (
                ReferenceLayer.provider_key
                == ReferenceLayerSource.provider_key
            ),
        )
        .where(
            ReferenceLayer.provider_key == snapshot.provider_key,
            ReferenceLayer.last_seen_snapshot_id == snapshot.id,
            ReferenceLayer.service_id.in_(service_ids),
            ReferenceLayer.node_type == "layer",
            ReferenceLayerSource.enabled.is_(True),
        )
        .order_by(
            ReferenceLayer.service_id,
            ReferenceLayerSource.layer_id,
            ReferenceLayerSource.is_primary.desc(),
            ReferenceLayerSource.priority,
            ReferenceLayerSource.id,
        )
    ):
        sources_by_service[service_id].append(source)
    result: dict[int, dict[str, Any]] = {}
    for service_id, service in services.items():
        service_sources = sources_by_service.get(service_id, [])
        blockers: list[str] = []
        review_ids: list[int] = []
        if not service_sources:
            blockers.append("mirror_authorization_missing")
        for source in service_sources:
            try:
                review = require_current_source_authorization(
                    db,
                    source=source,
                    require_acquisition=True,
                )
            except MirrorAuthorizationError as error:
                blockers.append(error.code)
            else:
                review_ids.append(review.id)
        blockers = list(dict.fromkeys(blockers))
        result[service_id] = {
            "mirror_authorized": not blockers,
            "authorization_status": (
                "authorized" if not blockers else "blocked"
            ),
            "blocking_reasons": blockers,
            "service_id": service.id,
            "service_source_key": service.source_key,
            "catalog_license_status": service.license_status,
            "mirror_review_count": len(review_ids),
            "reviewed_source_count": len(review_ids),
            "enabled_source_count": len(service_sources),
            # Legacy WMS evidence remains intentionally separate and does not
            # authorize local retention or service.
            "license_review_id": None,
            "license_review_valid": False,
            "review_decision": None,
            "allow_cache": False,
            "explicit_allow_mirror": not blockers,
            "attestation_id": None,
            "attestation_valid": False,
        }
    return result


def _missing_service_authorization() -> dict[str, Any]:
    return {
        "mirror_authorized": False,
        "authorization_status": "missing",
        "blocking_reasons": ["catalog_service_missing"],
        "service_id": None,
        "service_source_key": None,
        "catalog_license_status": None,
        "mirror_review_count": 0,
        "reviewed_source_count": 0,
        "enabled_source_count": 0,
        "license_review_id": None,
        "license_review_valid": False,
        "review_decision": None,
        "allow_cache": False,
        "explicit_allow_mirror": None,
        "attestation_id": None,
        "attestation_valid": False,
    }


def _latest_runs_by_source(
    db: Session,
    source_ids: list[int],
) -> dict[int, ReferenceSyncRun]:
    if not source_ids:
        return {}
    return {
        source_id: run
        for source_id, run in db.execute(
            select(ReferenceSyncRun.source_id, ReferenceSyncRun)
            .where(ReferenceSyncRun.source_id.in_(source_ids))
            .distinct(ReferenceSyncRun.source_id)
            .order_by(
                ReferenceSyncRun.source_id,
                ReferenceSyncRun.queued_at.desc(),
                ReferenceSyncRun.id.desc(),
            )
        )
    }


def _promotions_by_layer(
    db: Session,
    *,
    provider_key: str,
    layer_ids: list[int],
) -> dict[int, list[ReferenceDeliveryPromotion]]:
    result: dict[int, list[ReferenceDeliveryPromotion]] = defaultdict(list)
    if not layer_ids:
        return result
    for promotion in db.scalars(
        select(ReferenceDeliveryPromotion)
        .where(
            ReferenceDeliveryPromotion.provider_key == provider_key,
            ReferenceDeliveryPromotion.layer_id.in_(layer_ids),
        )
        .order_by(
            ReferenceDeliveryPromotion.layer_id,
            ReferenceDeliveryPromotion.sequence_number,
        )
    ):
        result[promotion.layer_id].append(promotion)
    return result


def _promotion_chain_is_valid(
    promotions: list[ReferenceDeliveryPromotion],
) -> bool:
    if not promotions:
        return True
    previous = None
    for index, promotion in enumerate(promotions, start=1):
        if (
            promotion.sequence_number != index
            or not stored_promotion_hash_is_valid(promotion)
        ):
            return False
        if previous is None:
            if (
                promotion.previous_event_id is not None
                or promotion.previous_event_sha256 is not None
            ):
                return False
        elif (
            promotion.previous_event_id != previous.id
            or promotion.previous_event_sha256 != previous.event_sha256
        ):
            return False
        previous = promotion
    return True


def _previous_served_version_id(
    promotions: list[ReferenceDeliveryPromotion],
    *,
    active_version_id: int | None,
) -> int | None:
    for promotion in reversed(promotions):
        candidate = promotion.to_version_id
        if candidate is not None and candidate != active_version_id:
            return candidate
    return None


def _versions_and_runs(
    db: Session,
    version_ids: set[int],
) -> tuple[
    dict[int, ReferenceDeliveryVersion],
    dict[int, ReferenceSyncRun],
]:
    versions: dict[int, ReferenceDeliveryVersion] = {}
    runs: dict[int, ReferenceSyncRun] = {}
    if not version_ids:
        return versions, runs
    for version, run in db.execute(
        select(ReferenceDeliveryVersion, ReferenceSyncRun)
        .join(
            ReferenceSyncRun,
            ReferenceSyncRun.id == ReferenceDeliveryVersion.sync_run_id,
        )
        .where(ReferenceDeliveryVersion.id.in_(version_ids))
    ):
        versions[version.id] = version
        runs[version.id] = run
    return versions, runs


def _assets_by_version(
    db: Session,
    version_ids: set[int],
) -> dict[int, list[ReferenceDeliveryAsset]]:
    result: dict[int, list[ReferenceDeliveryAsset]] = defaultdict(list)
    if not version_ids:
        return result
    for asset in db.scalars(
        select(ReferenceDeliveryAsset)
        .where(ReferenceDeliveryAsset.version_id.in_(version_ids))
        .order_by(
            ReferenceDeliveryAsset.version_id,
            ReferenceDeliveryAsset.id,
        )
    ):
        result[asset.version_id].append(asset)
    return result


def _version_summary(
    version: ReferenceDeliveryVersion | None,
    run: ReferenceSyncRun | None,
    assets: list[ReferenceDeliveryAsset],
    *,
    current_snapshot_id: int,
) -> dict[str, Any] | None:
    if version is None or run is None:
        return None
    sizes_complete = bool(assets) and all(
        asset.size_bytes is not None for asset in assets
    )
    size_bytes = (
        sum(int(asset.size_bytes or 0) for asset in assets)
        if sizes_complete
        else None
    )
    try:
        validation_hash_valid = bool(
            isinstance(version.validation_json, dict)
            and canonical_json_sha256(version.validation_json)
            == version.validation_sha256
        )
    except DeliveryBuildError:
        validation_hash_valid = False
    return {
        "version_id": version.id,
        "sequence_number": version.sequence_number,
        "delivery_kind": version.delivery_kind,
        "source_id": version.source_id,
        "source_version": version.source_version,
        "reference_at": version.reference_at,
        "created_at": version.created_at,
        "catalog_snapshot_id": version.catalog_snapshot_id,
        "uses_current_catalog_snapshot": (
            version.catalog_snapshot_id == current_snapshot_id
        ),
        "content_sha256": version.content_sha256,
        "manifest_sha256": version.manifest_sha256,
        "size_bytes": size_bytes,
        "size_bytes_complete": sizes_complete,
        "asset_count": len(assets),
        "validation": {
            "passed": bool(
                validation_hash_valid
                and version.validation_json.get("passed") is True
            ),
            "hash_valid": validation_hash_valid,
            "sha256": version.validation_sha256,
            "validated_at": run.finished_at,
        },
        "sync_run": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": _duration_seconds(run),
        },
    }


def _source_summary(
    source: ReferenceLayerSource,
    run: ReferenceSyncRun | None,
    *,
    expected_generation: int,
) -> dict[str, Any]:
    return {
        "source_id": source.id,
        "source_key": source.source_key,
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "sync_strategy": source.sync_strategy,
        "definition_sha256": source.definition_sha256,
        "expected_active_generation": expected_generation,
        "enabled": source.enabled,
        "is_primary": source.is_primary,
        "priority": source.priority,
        "next_check_at": source.next_check_at,
        "check_interval_seconds": source.check_interval_seconds,
        "last_run": (
            {
                "id": run.id,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "duration_seconds": _duration_seconds(run),
                "error_code": run.error_code,
                "error_summary": run.error_summary,
            }
            if run is not None
            else None
        ),
    }


def _duration_seconds(run: ReferenceSyncRun) -> float | None:
    if (
        run.started_at is None
        or run.finished_at is None
        or run.finished_at < run.started_at
    ):
        return None
    return (run.finished_at - run.started_at).total_seconds()


def _recovery_candidate(
    *,
    state: ReferenceLayerDeliveryState | None,
    state_valid: bool,
    active: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if (
        state is None
        or not state_valid
        or previous is None
        or not previous["validation"]["passed"]
    ):
        return None
    action = "rollback" if state.status == "active" and active else "reactivate"
    return {
        "action": action,
        "target_version_id": previous["version_id"],
        "expected_generation": state.generation,
        "requires_explicit_actor_reason_and_dry_run": True,
    }


def _retention_status(
    db: Session,
    *,
    provider_key: str,
) -> dict[str, Any]:
    version_count = db.scalar(
        select(func.count(ReferenceDeliveryVersion.id)).where(
            ReferenceDeliveryVersion.provider_key == provider_key
        )
    )
    delivery_asset_count = db.scalar(
        select(func.count(ReferenceDeliveryAsset.id))
        .join(
            ReferenceDeliveryVersion,
            ReferenceDeliveryVersion.id
            == ReferenceDeliveryAsset.version_id,
        )
        .where(ReferenceDeliveryVersion.provider_key == provider_key)
    )
    source_artifact_count = db.scalar(
        select(func.count(ReferenceSourceArtifact.id))
        .join(
            ReferenceLayerSource,
            ReferenceLayerSource.id == ReferenceSourceArtifact.source_id,
        )
        .where(ReferenceLayerSource.provider_key == provider_key)
    )
    return {
        "policy": "retain_all_published_history",
        "invariant": PUBLISHED_RETENTION_INVARIANT,
        "published_blob_gc_enabled": False,
        "protected_delivery_version_count": int(version_count or 0),
        "protected_delivery_asset_count": int(delivery_asset_count or 0),
        "protected_source_artifact_count": int(
            source_artifact_count or 0
        ),
        "staging_gc": {
            "supported": True,
            "default_retention_seconds": (
                settings.reference_staging_retention_seconds
            ),
            "scope": "stale_unlocked_partial_files_only",
            "default_mode": "dry-run",
        },
    }


def _require_active_actor(db: Session, actor_user_id: int) -> User:
    # FOR SHARE fences is_active changes through the lifecycle commit while
    # remaining compatible with the FK key-share lock taken by the event row.
    actor = db.scalar(
        select(User)
        .where(User.id == actor_user_id)
        .with_for_update(read=True)
    )
    if actor is None:
        raise MirrorAdminInputError("actor user does not exist")
    if not actor.is_active:
        raise MirrorAdminInputError("actor user is inactive")
    return actor


def execute_transition(
    db: Session,
    *,
    store: ReferenceBlobStore,
    action: str,
    provider_key: str,
    layer_id: int,
    target_version_id: int,
    expected_generation: int,
    actor_user_id: int,
    reason: str,
    apply: bool,
) -> dict[str, Any]:
    """Validate actor identity and delegate the transition to lifecycle."""

    _validate_provider_key(provider_key)
    actor = _require_active_actor(db, actor_user_id)
    arguments = {
        "provider_key": provider_key,
        "layer_id": layer_id,
        "to_version_id": target_version_id,
        "expected_generation": expected_generation,
        "reason": reason,
        "metadata_verifier": (
            lambda transition_db, version: (
                verify_local_metadata_transition(
                    store,
                    transition_db,
                    version,
                )
            )
        ),
    }
    if action == "rollback":
        result: DeliveryTransitionPreview | PromotionResult
        if apply:
            result = rollback_delivery_version(
                db,
                actor_id=actor.id,
                **arguments,
            )
        else:
            result = preview_rollback_delivery_version(db, **arguments)
    elif action == "reactivate":
        if apply:
            result = reactivate_delivery(
                db,
                actor_id=actor.id,
                **arguments,
            )
        else:
            result = preview_reactivate_delivery(db, **arguments)
    else:
        raise MirrorAdminInputError("unsupported transition")
    return {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "applied": apply,
        "actor_user_id": actor.id,
        "transition": asdict(result),
    }


def execute_manual_enqueue(
    db: Session,
    *,
    provider_key: str,
    source_id: int,
    expected_source_definition_sha256: str,
    expected_generation: int,
    check_mode: str,
    actor_user_id: int,
    reason: str,
    apply: bool,
) -> dict[str, Any]:
    """Validate and optionally queue one exact operator-selected source."""

    _validate_provider_key(provider_key)
    actor = _require_active_actor(db, actor_user_id)
    actor_id = actor.id
    arguments = {
        "provider_key": provider_key,
        "source_id": source_id,
        "expected_source_definition_sha256": (
            expected_source_definition_sha256
        ),
        "expected_generation": expected_generation,
        "check_mode": check_mode,
        "requested_by_id": actor_id,
        "reason": reason,
    }
    result: ManualSyncEnqueueResult
    if apply:
        result = enqueue_manual_sync_run(db, **arguments)
    else:
        result = preview_manual_sync_run(db, **arguments)
    return {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "applied": apply,
        "actor_user_id": actor_id,
        "manual_sync": asdict(result),
    }


def staging_gc(
    *,
    apply: bool,
    older_than_seconds: int | None = None,
    store: ReferenceBlobStore | None = None,
) -> dict[str, Any]:
    """Inspect or remove stale unlocked partials, never published blobs."""

    retention = (
        settings.reference_staging_retention_seconds
        if older_than_seconds is None
        else older_than_seconds
    )
    if isinstance(retention, bool) or retention < 3_600:
        raise MirrorAdminInputError(
            "staging retention must be at least 3600 seconds"
        )
    owned_store = store is None
    instance = store or ReferenceBlobStore(
        settings.reference_storage_root,
        max_blob_bytes=settings.reference_blob_max_bytes,
        quota_bytes=settings.reference_storage_quota_bytes,
        min_free_bytes=settings.reference_storage_min_free_bytes,
    )
    try:
        if apply:
            result = instance.cleanup_staging(
                older_than_seconds=retention,
            )
            eligible_count = result.deleted_count
            eligible_bytes = result.deleted_bytes
            skipped_count = result.skipped_count
        else:
            result = instance.inspect_staging(
                older_than_seconds=retention,
            )
            eligible_count = result.eligible_count
            eligible_bytes = result.eligible_bytes
            skipped_count = result.skipped_count
    finally:
        if owned_store:
            instance.close()
    return {
        "ok": True,
        "mode": "apply" if apply else "dry-run",
        "applied": apply,
        "older_than_seconds": retention,
        "eligible_count": eligible_count,
        "eligible_bytes": eligible_bytes,
        "deleted_count": eligible_count if apply else 0,
        "deleted_bytes": eligible_bytes if apply else 0,
        "skipped_count": skipped_count,
        "published_blobs_deleted": 0,
        "scope": "stale_unlocked_partial_files_only",
        "published_history_policy": "retain_all",
    }


def _validate_provider_key(provider_key: str) -> None:
    if (
        not isinstance(provider_key, str)
        or PROVIDER_KEY_RE.fullmatch(provider_key) is None
    ):
        raise MirrorAdminInputError("provider key is invalid")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and safely recover local SIUR mirrors",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status_parser = commands.add_parser(
        "status",
        help="emit operational, authorization and retention status",
    )
    status_parser.add_argument("--provider-key", default="siur")
    status_parser.add_argument("--layer-id", type=int)

    for command in ("rollback", "reactivate"):
        transition = commands.add_parser(
            command,
            help=f"validate or apply a generation-fenced {command}",
        )
        transition.add_argument("--provider-key", required=True)
        transition.add_argument("--layer-id", type=int, required=True)
        transition.add_argument(
            "--target-version-id",
            type=int,
            required=True,
        )
        transition.add_argument(
            "--expected-generation",
            type=int,
            required=True,
        )
        transition.add_argument(
            "--actor-user-id",
            type=int,
            required=True,
        )
        transition.add_argument("--reason", required=True)
        mode = transition.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")

    enqueue_parser = commands.add_parser(
        "enqueue",
        help="validate or queue one exact source independently of its schedule",
    )
    enqueue_parser.add_argument("--provider-key", default="siur")
    enqueue_parser.add_argument("--source-id", type=int, required=True)
    enqueue_parser.add_argument(
        "--expected-source-definition-sha256",
        required=True,
    )
    enqueue_parser.add_argument(
        "--expected-generation",
        type=int,
        required=True,
    )
    enqueue_parser.add_argument(
        "--check-mode",
        choices=("conditional", "full"),
        default="full",
    )
    enqueue_parser.add_argument(
        "--actor-user-id",
        type=int,
        required=True,
    )
    enqueue_parser.add_argument("--reason", required=True)
    enqueue_mode = enqueue_parser.add_mutually_exclusive_group(required=True)
    enqueue_mode.add_argument("--dry-run", action="store_true")
    enqueue_mode.add_argument("--apply", action="store_true")

    tile_preflight = commands.add_parser(
        "tile-preflight",
        help="project all current tile archives against aggregate CAS capacity",
    )
    tile_preflight.add_argument("--provider-key", default="siur")
    tile_preflight.add_argument("--layer-id", type=int)
    tile_preflight.add_argument("--source-id", type=int)
    tile_preflight.add_argument("--source-key")
    tile_preflight.add_argument(
        "--sample-limit",
        type=int,
        default=DEFAULT_PREFLIGHT_SAMPLES,
    )
    tile_preflight.add_argument(
        "--concurrency",
        type=int,
        default=settings.reference_tile_concurrency,
    )
    tile_preflight.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
    )

    gc_parser = commands.add_parser(
        "staging-gc",
        help="inspect or delete stale unlocked staging partials only",
    )
    gc_parser.add_argument("--older-than-seconds", type=int)
    gc_mode = gc_parser.add_mutually_exclusive_group(required=True)
    gc_mode.add_argument("--dry-run", action="store_true")
    gc_mode.add_argument("--apply", action="store_true")
    return parser


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        normalized = (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )
        return normalized.isoformat().replace("+00:00", "Z")
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _print_json(value: dict[str, Any]) -> None:
    print(
        json.dumps(
            value,
            default=_json_default,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    command_exit_code = 0
    try:
        if arguments.command == "staging-gc":
            result = staging_gc(
                apply=arguments.apply,
                older_than_seconds=arguments.older_than_seconds,
            )
        else:
            register_all_models()
            with SessionLocal() as db:
                if arguments.command == "status":
                    result = operational_status(
                        db,
                        provider_key=arguments.provider_key,
                        layer_id=arguments.layer_id,
                    )
                elif arguments.command == "tile-preflight":
                    with ReferenceBlobStore(
                        settings.reference_storage_root,
                        max_blob_bytes=(
                            settings.reference_blob_max_bytes
                        ),
                        quota_bytes=(
                            settings.reference_storage_quota_bytes
                        ),
                        min_free_bytes=(
                            settings.reference_storage_min_free_bytes
                        ),
                        read_only=True,
                    ) as store:
                        result = aggregate_tile_capacity_preflight(
                            db,
                            store,
                            provider_key=arguments.provider_key,
                            layer_id=arguments.layer_id,
                            source_id=arguments.source_id,
                            source_key=arguments.source_key,
                            sample_limit=arguments.sample_limit,
                            concurrency=arguments.concurrency,
                            max_archive_bytes=(
                                settings.reference_tile_archive_max_bytes
                            ),
                            max_tile_count=(
                                settings.reference_tile_max_count
                            ),
                        )
                    command_exit_code = tile_capacity_preflight_exit_code(
                        result
                    )
                elif arguments.command == "enqueue":
                    result = execute_manual_enqueue(
                        db,
                        provider_key=arguments.provider_key,
                        source_id=arguments.source_id,
                        expected_source_definition_sha256=(
                            arguments.expected_source_definition_sha256
                        ),
                        expected_generation=(
                            arguments.expected_generation
                        ),
                        check_mode=arguments.check_mode,
                        actor_user_id=arguments.actor_user_id,
                        reason=arguments.reason,
                        apply=arguments.apply,
                    )
                else:
                    with ReferenceBlobStore(
                        settings.reference_storage_root,
                        max_blob_bytes=(
                            settings.reference_blob_max_bytes
                        ),
                        quota_bytes=(
                            settings.reference_storage_quota_bytes
                        ),
                        min_free_bytes=(
                            settings.reference_storage_min_free_bytes
                        ),
                    ) as store:
                        result = execute_transition(
                            db,
                            store=store,
                            action=arguments.command,
                            provider_key=arguments.provider_key,
                            layer_id=arguments.layer_id,
                            target_version_id=(
                                arguments.target_version_id
                            ),
                            expected_generation=(
                                arguments.expected_generation
                            ),
                            actor_user_id=arguments.actor_user_id,
                            reason=arguments.reason,
                            apply=arguments.apply,
                        )
        _print_json(result)
        return command_exit_code
    except (
        MirrorAdminInputError,
        MirrorLifecycleError,
        TileCapacityPreflightInputError,
    ) as error:
        _print_json(
            {
                "ok": False,
                "error_code": "operator_request_rejected",
                "error_summary": str(error),
            }
        )
        return 2
    except (ReferenceBlobStoreError, SQLAlchemyError) as error:
        _print_json(
            {
                "ok": False,
                "error_code": "operator_service_unavailable",
                "error_summary": type(error).__name__,
            }
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by operators
    raise SystemExit(main())
