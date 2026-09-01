"""Read-only readiness gate for the complete local SIUR mirror.

The operational catalog intentionally remains useful while individual layers
are being prepared.  Integration readiness is stricter: every current leaf
must have a reconciled, non-blocked strategy, current source authorization,
complete local styles and metadata, and a servable local delivery.  This
module reports those requirements without running watchers, opening upstream
connections, enqueueing work or mutating delivery state.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.delivery_builder import (
    DeliveryBuildError,
    canonical_json_sha256,
)
from app.reference_layers.local_delivery import (
    catalog_local_delivery_availability,
)
from app.reference_layers.local_metadata import (
    LocalMetadataError,
    catalog_local_metadata_availability,
    verify_local_metadata_transition,
)
from app.reference_layers.mirror_authorization import (
    source_authorization_blocker,
)
from app.reference_layers.mirror_lifecycle import (
    MirrorLifecycleError,
    stored_catalog_snapshot_is_valid,
    stored_source_definition_is_valid,
)
from app.reference_layers.mirror_reconcile import (
    MirrorReconciliationError,
    build_mirror_reconciliation_plan,
)
from app.reference_layers.mirror_status import catalog_mirror_statuses
from app.reference_layers.mirror_strategy import (
    MirrorStrategyError,
    current_mirror_strategies,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSyncRun,
)
from app.reference_layers.transition_servability import (
    DeliveryTransitionServabilityError,
    verify_delivery_transition_servability,
)


READINESS_SCHEMA_VERSION = "siur-mirror-readiness-v1"
MAX_DAILY_CHECK_INTERVAL_SECONDS = 86_400
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$", re.ASCII)
_SERVABLE_MIRROR_STATUSES = frozenset(
    {"active", "syncing", "serving_previous"}
)

PhysicalVerifier = Callable[
    [ReferenceBlobStore, Session, ReferenceDeliveryVersion],
    object,
]
MetadataVerifier = Callable[
    [ReferenceBlobStore, Session, ReferenceDeliveryVersion],
    object,
]


class MirrorReadinessError(RuntimeError):
    """The readiness report cannot be built from trustworthy local state."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def build_mirror_readiness_report(
    db: Session,
    store: ReferenceBlobStore,
    *,
    provider_key: str = "siur",
    remote_proxy_enabled: bool,
    physical: bool = False,
    physical_verifier: PhysicalVerifier = (
        verify_delivery_transition_servability
    ),
    metadata_verifier: MetadataVerifier = verify_local_metadata_transition,
) -> dict[str, Any]:
    """Build one deterministic, fail-closed readiness projection."""

    _validate_provider_key(provider_key)
    reconciliation = build_mirror_reconciliation_plan(
        db,
        provider_key=provider_key,
    )
    source_plan = reconciliation.source_plan
    strategy_plan = reconciliation.strategy_plan
    strategy_status = reconciliation.strategy_before
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.id == source_plan.snapshot_id,
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )
    if snapshot is None:
        raise MirrorReadinessError(
            "current catalog snapshot is unavailable",
            code="catalog_unavailable",
        )

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
    layer_ids = [layer.id for layer in layers]
    styles = list(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(
                ReferenceLayerStyle.provider_key == provider_key,
                ReferenceLayerStyle.last_seen_snapshot_id == snapshot.id,
                ReferenceLayerStyle.layer_id.in_(layer_ids),
                ReferenceLayerStyle.status.in_(("active", "degraded")),
            )
            .order_by(
                ReferenceLayerStyle.layer_id,
                ReferenceLayerStyle.id,
            )
        )
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
                ReferenceLayerSource.priority,
                ReferenceLayerSource.id,
            )
        )
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
    active_version_ids = [
        state.active_version_id
        for state in states.values()
        if state.status == "active" and state.active_version_id is not None
    ]
    active_versions = {
        version.id: version
        for version in db.scalars(
            select(ReferenceDeliveryVersion).where(
                ReferenceDeliveryVersion.provider_key == provider_key,
                ReferenceDeliveryVersion.id.in_(active_version_ids),
            )
        )
    }
    open_run_source_ids = set(
        db.scalars(
            select(ReferenceSyncRun.source_id).where(
                ReferenceSyncRun.provider_key == provider_key,
                ReferenceSyncRun.status.in_(("queued", "running")),
            )
        )
    )

    local_availability = catalog_local_delivery_availability(
        db,
        provider_key=provider_key,
        layers=layers,
        styles=styles,
    )
    metadata_availability = catalog_local_metadata_availability(
        db,
        store,
        provider_key=provider_key,
        layers=layers,
        local_availability=local_availability,
    )
    mirror_statuses = catalog_mirror_statuses(
        db,
        provider_key=provider_key,
        layers=layers,
        styles=styles,
        local_availability=local_availability,
    )
    try:
        persisted_strategies = current_mirror_strategies(
            db,
            provider_key=provider_key,
            snapshot_id=snapshot.id,
        )
    except MirrorStrategyError:
        persisted_strategies = {}

    assignments = {
        assignment.layer_id: assignment
        for assignment in strategy_plan.assignments
    }
    sources_by_layer: dict[int, list[ReferenceLayerSource]] = defaultdict(list)
    sources_by_identity: dict[
        tuple[int, str],
        ReferenceLayerSource,
    ] = {}
    for source in sources:
        sources_by_layer[source.layer_id].append(source)
        sources_by_identity[(source.layer_id, source.source_key)] = source
    styles_by_layer: dict[int, list[ReferenceLayerStyle]] = defaultdict(list)
    for style in styles:
        styles_by_layer[style.layer_id].append(style)

    source_drift_count = (
        len(source_plan.new_source_keys)
        + len(source_plan.updated_source_keys)
        + len(source_plan.deactivated_source_keys)
    )
    catalog_valid = bool(
        stored_catalog_snapshot_is_valid(snapshot)
        and snapshot.unresolved_count == 0
        and snapshot.layer_count == len(layers)
        and len(assignments) == len(layers)
    )
    strategy_ready = bool(
        strategy_status.complete
        and strategy_status.matches_plan
        and len(persisted_strategies) == len(layers)
    )

    enabled_source_count = 0
    unauthorized_enabled_source_count = 0
    invalid_source_count = 0
    overdue_without_open_run_count = 0
    schedule_invalid_count = 0
    physical_verified_count = 0
    layer_rows: list[dict[str, Any]] = []
    blocker_counts: Counter[str] = Counter()

    for layer in layers:
        assignment = assignments.get(layer.id)
        persisted = persisted_strategies.get(layer.id)
        availability = local_availability.get(layer.id)
        mirror_status = mirror_statuses[layer.id]
        state = states.get(layer.id)
        active_version = (
            active_versions.get(state.active_version_id)
            if state is not None and state.active_version_id is not None
            else None
        )
        layer_sources = sources_by_layer.get(layer.id, [])
        enabled_sources = [source for source in layer_sources if source.enabled]
        enabled_source_count += len(enabled_sources)
        source_blockers: dict[int, str] = {}
        invalid_source_ids: list[int] = []
        schedule_blockers: dict[int, str] = {}
        for source in enabled_sources:
            if not stored_source_definition_is_valid(source):
                invalid_source_ids.append(source.id)
                invalid_source_count += 1
                source_blockers[source.id] = "source_definition_invalid"
                continue
            blocker = source_authorization_blocker(
                db,
                source=source,
                require_acquisition=True,
            )
            if blocker is not None:
                source_blockers[source.id] = blocker
                unauthorized_enabled_source_count += 1
            schedule_blocker = _source_schedule_blocker(
                source,
                has_open_run=source.id in open_run_source_ids,
            )
            if schedule_blocker is not None:
                schedule_blockers[source.id] = schedule_blocker
                if schedule_blocker == "overdue_without_open_run":
                    overdue_without_open_run_count += 1
                else:
                    schedule_invalid_count += 1

        expected_style_ids = {
            style.id for style in styles_by_layer.get(layer.id, [])
        }
        available_style_ids = (
            set(availability.available_style_ids)
            if availability is not None
            else set()
        )
        delivery_available = bool(
            availability is not None and availability.delivery_available
        )
        styles_complete = bool(
            delivery_available
            and (
                not expected_style_ids
                or available_style_ids == expected_style_ids
            )
        )
        metadata_available = bool(metadata_availability.get(layer.id))
        active_version_valid = _active_version_is_valid(
            active_version,
            snapshot_id=snapshot.id,
        )
        strategy_source = (
            sources_by_identity.get((layer.id, assignment.source_key))
            if assignment is not None and assignment.source_key is not None
            else None
        )

        blockers: list[str] = []
        if source_drift_count:
            blockers.append("source_reconciliation_required")
        if not strategy_ready or assignment is None or persisted is None:
            blockers.append("strategy_reconciliation_required")
        elif assignment.strategy == "blocked":
            blockers.append("strategy_blocked")
        elif assignment.strategy == "composition":
            blockers.append("composition_not_materialized")
        elif (
            strategy_source is None
            or not strategy_source.enabled
            or not strategy_source.is_primary
        ):
            blockers.append("strategy_source_not_primary")
        if invalid_source_ids:
            blockers.append("source_definition_invalid")
        if source_blockers:
            blockers.append("authorization_required")
        if schedule_blockers:
            blockers.append("schedule_invalid")
        if availability is None:
            blockers.append("proxy_candidate")
        elif not delivery_available:
            blockers.append(availability.delivery_blocker or "sync_required")
        if not styles_complete:
            blockers.append("style_parity_incomplete")
        if not metadata_available:
            blockers.append("metadata_unavailable")
        if (
            mirror_status.status not in _SERVABLE_MIRROR_STATUSES
            or not active_version_valid
        ):
            blockers.append("delivery_invalid")

        physical_result: dict[str, Any] | None = None
        if physical:
            physical_result = {
                "checked": False,
                "passed": False,
                "blocker": "physical_delivery_unavailable",
            }
            if active_version is not None and delivery_available:
                physical_result["checked"] = True
                try:
                    metadata_verifier(store, db, active_version)
                    physical_verifier(store, db, active_version)
                except (
                    DeliveryTransitionServabilityError,
                    LocalMetadataError,
                    ReferenceBlobStoreError,
                    MirrorLifecycleError,
                ):
                    physical_result["blocker"] = (
                        "physical_delivery_invalid"
                    )
                else:
                    physical_result = {
                        "checked": True,
                        "passed": True,
                        "blocker": None,
                    }
                    physical_verified_count += 1
            if not physical_result["passed"]:
                blockers.append(physical_result["blocker"])

        blockers = list(dict.fromkeys(blockers))
        blocker_counts.update(blockers)
        layer_rows.append(
            {
                "layer_id": layer.id,
                "source_key": layer.source_key,
                "title": layer.title,
                "catalog_status": layer.status,
                "strategy": {
                    "kind": assignment.strategy if assignment else None,
                    "generation": (
                        persisted.generation if persisted is not None else None
                    ),
                    "source_id": (
                        persisted.source_id if persisted is not None else None
                    ),
                    "reason_code": (
                        assignment.reason_code if assignment else None
                    ),
                    "evidence_sha256": (
                        persisted.evidence_sha256
                        if persisted is not None
                        else None
                    ),
                },
                "authorization": {
                    "enabled_source_count": len(enabled_sources),
                    "authorized_source_count": (
                        len(enabled_sources) - len(source_blockers)
                    ),
                    "source_blockers": {
                        str(source_id): blocker
                        for source_id, blocker in sorted(
                            source_blockers.items()
                        )
                    },
                },
                "schedule": {
                    "blockers": {
                        str(source_id): blocker
                        for source_id, blocker in sorted(
                            schedule_blockers.items()
                        )
                    },
                },
                "mirror_status": mirror_status.status,
                "active_version_id": (
                    active_version.id if active_version is not None else None
                ),
                "active_generation": (
                    state.generation if state is not None else None
                ),
                "delivery": {
                    "available": delivery_available,
                    "blocker": (
                        availability.delivery_blocker
                        if availability is not None
                        else "proxy_candidate"
                    ),
                    "expected_style_ids": sorted(expected_style_ids),
                    "available_style_ids": sorted(available_style_ids),
                    "styles_complete": styles_complete,
                    "metadata_available": metadata_available,
                },
                "physical": physical_result,
                "readiness": "ready" if not blockers else blockers[0],
                "blockers": blockers,
            }
        )

    strategy_counts = Counter(
        assignment.strategy for assignment in strategy_plan.assignments
    )
    local_delivery_available_count = sum(
        row["delivery"]["available"] for row in layer_rows
    )
    metadata_available_count = sum(
        row["delivery"]["metadata_available"] for row in layer_rows
    )
    style_complete_layer_count = sum(
        row["delivery"]["styles_complete"] for row in layer_rows
    )
    proxy_candidate_count = sum(
        "proxy_candidate" in row["blockers"] for row in layer_rows
    )
    layers_ready_count = sum(not row["blockers"] for row in layer_rows)
    requirements = [
        _requirement(
            "catalog_integrity",
            catalog_valid,
            expected="valid current catalog with no unresolved entries",
            actual={
                "snapshot_layer_count": snapshot.layer_count,
                "database_leaf_count": len(layers),
                "unresolved_count": snapshot.unresolved_count,
            },
        ),
        _requirement(
            "source_reconciliation",
            source_drift_count == 0,
            expected=0,
            actual=source_drift_count,
        ),
        _requirement(
            "strategy_reconciliation",
            strategy_ready,
            expected={
                "complete": True,
                "matches_plan": True,
                "row_count": len(layers),
            },
            actual={
                "complete": strategy_status.complete,
                "matches_plan": strategy_status.matches_plan,
                "row_count": strategy_status.row_count,
            },
        ),
        _requirement(
            "no_blocked_strategies",
            strategy_counts["blocked"] == 0,
            expected=0,
            actual=strategy_counts["blocked"],
        ),
        _requirement(
            "no_unmaterialized_compositions",
            strategy_counts["composition"] == 0,
            expected=0,
            actual=strategy_counts["composition"],
        ),
        _requirement(
            "current_source_authorizations",
            unauthorized_enabled_source_count == 0
            and invalid_source_count == 0,
            expected=0,
            actual=(
                unauthorized_enabled_source_count + invalid_source_count
            ),
        ),
        _requirement(
            "daily_update_schedule",
            schedule_invalid_count == 0
            and overdue_without_open_run_count == 0,
            expected=0,
            actual=(
                schedule_invalid_count + overdue_without_open_run_count
            ),
        ),
        _requirement(
            "local_deliveries",
            local_delivery_available_count == len(layers),
            expected=len(layers),
            actual=local_delivery_available_count,
        ),
        _requirement(
            "complete_styles",
            style_complete_layer_count == len(layers),
            expected=len(layers),
            actual=style_complete_layer_count,
        ),
        _requirement(
            "local_metadata",
            metadata_available_count == len(layers),
            expected=len(layers),
            actual=metadata_available_count,
        ),
        _requirement(
            "remote_proxy_disabled",
            not remote_proxy_enabled,
            expected=False,
            actual=remote_proxy_enabled,
        ),
    ]
    structural_ready = bool(
        catalog_valid
        and all(requirement["passed"] for requirement in requirements)
        and layers_ready_count == len(layers)
    )
    physical_ready = bool(
        physical and physical_verified_count == len(layers)
    )
    if physical:
        requirements.append(
            _requirement(
                "physical_delivery_integrity",
                physical_verified_count == len(layers),
                expected=len(layers),
                actual=physical_verified_count,
            )
        )
    ready = bool(
        structural_ready and (not physical or physical_ready)
    )
    generated_at = db.scalar(select(func.now()))
    report: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "ok": True,
        "ready": ready,
        "mode": "read-only",
        "profile": "physical" if physical else "structural",
        "provider_key": provider_key,
        "generated_at": generated_at,
        "state_fence": {
            "catalog_snapshot_id": snapshot.id,
            "catalog_definition_sha256": snapshot.definition_sha256,
            "source_plan_sha256": source_plan.plan_sha256,
            "strategy_plan_sha256": strategy_plan.plan_sha256,
            "strategy_generation": strategy_status.generation,
            "report_sha256": None,
        },
        "summary": {
            "leaf_layer_count": len(layers),
            "strategy_counts": {
                key: strategy_counts[key]
                for key in (
                    "vector",
                    "raster",
                    "tiles",
                    "composition",
                    "blocked",
                )
            },
            "source_drift_count": source_drift_count,
            "strategy_drift_count": int(not strategy_ready),
            "blocked_strategy_count": strategy_counts["blocked"],
            "enabled_source_count": enabled_source_count,
            "unauthorized_enabled_source_count": (
                unauthorized_enabled_source_count
            ),
            "invalid_source_count": invalid_source_count,
            "overdue_without_open_run_count": (
                overdue_without_open_run_count
            ),
            "local_delivery_available_count": (
                local_delivery_available_count
            ),
            "metadata_available_count": metadata_available_count,
            "style_complete_layer_count": style_complete_layer_count,
            "physical_verified_count": (
                physical_verified_count if physical else None
            ),
            "proxy_candidate_count": proxy_candidate_count,
            "layers_ready_count": layers_ready_count,
            "layers_incomplete_count": len(layers) - layers_ready_count,
            "blockers_by_code": dict(sorted(blocker_counts.items())),
        },
        "requirements": requirements,
        "layers": layer_rows,
        "acceptance_scope": {
            "database_and_local_storage_checked": True,
            "structural_mirror_readiness_proven": structural_ready,
            "physical_delivery_proven": physical_ready,
            "offline_egress_cut_proven": False,
            "browser_map_legend_identify_proven": False,
            "corrupt_update_continuity_proven": False,
            "live_promotion_and_rollback_proven": False,
        },
        "safety": {
            "database_writes": False,
            "upstream_network": False,
            "watchers_run": False,
            "runs_enqueued": False,
            "downloads_started": False,
            "delivery_mutated": False,
        },
    }
    report["state_fence"]["report_sha256"] = _report_sha256(report)
    return report


def readiness_exit_code(report: dict[str, Any]) -> int:
    """Return zero only for a fully ready report in the selected profile."""

    return 0 if report.get("ready") is True else 1


def _source_schedule_blocker(
    source: ReferenceLayerSource,
    *,
    has_open_run: bool,
) -> str | None:
    if (
        source.check_interval_seconds > MAX_DAILY_CHECK_INTERVAL_SECONDS
        or source.full_refresh_interval_seconds
        < source.check_interval_seconds
        or source.next_check_at is None
    ):
        return "schedule_interval_invalid"
    now = datetime.now(timezone.utc)
    next_check_at = source.next_check_at
    if next_check_at.tzinfo is None:
        next_check_at = next_check_at.replace(tzinfo=timezone.utc)
    else:
        next_check_at = next_check_at.astimezone(timezone.utc)
    if next_check_at <= now and not has_open_run:
        return "overdue_without_open_run"
    return None


def _active_version_is_valid(
    version: ReferenceDeliveryVersion | None,
    *,
    snapshot_id: int,
) -> bool:
    if (
        version is None
        or version.catalog_snapshot_id != snapshot_id
        or not isinstance(version.validation_json, dict)
        or version.validation_json.get("passed") is not True
    ):
        return False
    try:
        return (
            canonical_json_sha256(version.validation_json)
            == version.validation_sha256
        )
    except DeliveryBuildError:
        return False


def _requirement(
    code: str,
    passed: bool,
    *,
    expected: Any,
    actual: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
    }


def _report_sha256(report: dict[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in report.items()
        if key != "generated_at"
    }
    semantic["state_fence"] = dict(semantic["state_fence"])
    semantic["state_fence"]["report_sha256"] = None
    encoded = json.dumps(
        semantic,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_provider_key(provider_key: str) -> None:
    if (
        not isinstance(provider_key, str)
        or _PROVIDER_RE.fullmatch(provider_key) is None
    ):
        raise MirrorReadinessError(
            "provider key is invalid",
            code="invalid_provider_key",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the complete local SIUR mirror without changing state"
        ),
    )
    parser.add_argument("--provider-key", default="siur")
    parser.add_argument(
        "--physical",
        action="store_true",
        help=(
            "also hash local artifacts and smoke the local renderer; "
            "never contacts upstream"
        ),
    )
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
    try:
        register_all_models()
        with SessionLocal() as db:
            db.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, "
                    "READ ONLY"
                )
            )
            with ReferenceBlobStore(
                settings.reference_storage_root,
                max_blob_bytes=settings.reference_blob_max_bytes,
                quota_bytes=settings.reference_storage_quota_bytes,
                min_free_bytes=settings.reference_storage_min_free_bytes,
                read_only=True,
            ) as store:
                report = build_mirror_readiness_report(
                    db,
                    store,
                    provider_key=arguments.provider_key,
                    remote_proxy_enabled=(
                        settings.reference_remote_proxy_enabled
                    ),
                    physical=arguments.physical,
                )
        _print_json(report)
        return readiness_exit_code(report)
    except (
        MirrorReadinessError,
        MirrorReconciliationError,
        MirrorStrategyError,
        MirrorLifecycleError,
    ) as error:
        _print_json(
            {
                "ok": False,
                "ready": False,
                "error_code": getattr(
                    error,
                    "code",
                    "readiness_evidence_invalid",
                ),
                "error_summary": str(error),
            }
        )
        return 2
    except (ReferenceBlobStoreError, SQLAlchemyError) as error:
        _print_json(
            {
                "ok": False,
                "ready": False,
                "error_code": "readiness_dependency_unavailable",
                "error_summary": type(error).__name__,
            }
        )
        return 3
    except Exception as error:  # pragma: no cover - operator boundary
        _print_json(
            {
                "ok": False,
                "ready": False,
                "error_code": "readiness_dependency_unavailable",
                "error_summary": type(error).__name__,
            }
        )
        return 3


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    raise SystemExit(main())
