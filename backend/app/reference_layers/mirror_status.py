"""Bounded operational status projection for the reference-layer catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.reference_layers.local_delivery import (
    catalog_local_delivery_availability,
)
from app.reference_layers.models import (
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSyncRun,
)
from app.reference_layers.wms_delivery import LayerDeliveryAvailability

MirrorStatus = Literal[
    "not_applicable",
    "legacy",
    "pending",
    "syncing",
    "active",
    "serving_previous",
    "error",
    "disabled",
]


@dataclass(frozen=True)
class LayerMirrorStatus:
    status: MirrorStatus
    active_version_id: int | None = None
    active_generation: int | None = None
    active_source_version: str | None = None
    active_reference_at: datetime | None = None
    active_created_at: datetime | None = None
    last_run_id: int | None = None
    last_run_status: str | None = None
    last_run_started_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_run_duration_seconds: float | None = None
    last_error_code: str | None = None
    last_error_summary: str | None = None
    next_check_at: datetime | None = None


def catalog_mirror_statuses(
    db: Session,
    *,
    provider_key: str,
    layers: list[ReferenceLayer],
    styles: list[ReferenceLayerStyle] | None = None,
    local_availability: dict[
        int,
        LayerDeliveryAvailability | None,
    ]
    | None = None,
) -> dict[int, LayerMirrorStatus]:
    """Return one fail-closed operational mirror state per catalog node."""

    leaf_ids = [item.id for item in layers if item.node_type == "layer"]
    result = {
        item.id: LayerMirrorStatus(
            status="not_applicable" if item.node_type != "layer" else "legacy"
        )
        for item in layers
    }
    if not leaf_ids:
        return result

    if local_availability is None:
        if styles is None:
            styles = list(
                db.scalars(
                    select(ReferenceLayerStyle).where(
                        ReferenceLayerStyle.layer_id.in_(leaf_ids),
                        ReferenceLayerStyle.status.in_(("active", "degraded")),
                    )
                )
            )
        availability = catalog_local_delivery_availability(
            db,
            provider_key=provider_key,
            layers=layers,
            styles=styles,
        )
    else:
        availability = local_availability

    source_rows = {
        layer_id: (bool(any_enabled), next_check_at)
        for layer_id, any_enabled, next_check_at in db.execute(
            select(
                ReferenceLayerSource.layer_id,
                func.bool_or(ReferenceLayerSource.enabled),
                func.min(ReferenceLayerSource.next_check_at).filter(
                    ReferenceLayerSource.enabled.is_(True)
                ),
            )
            .where(
                ReferenceLayerSource.provider_key == provider_key,
                ReferenceLayerSource.layer_id.in_(leaf_ids),
            )
            .group_by(ReferenceLayerSource.layer_id)
        )
    }
    latest_runs = {
        layer_id: run
        for layer_id, run in db.execute(
            select(ReferenceLayerSource.layer_id, ReferenceSyncRun)
            .join(
                ReferenceSyncRun,
                ReferenceSyncRun.source_id == ReferenceLayerSource.id,
            )
            .outerjoin(
                ReferenceLayerDeliveryState,
                and_(
                    ReferenceLayerDeliveryState.provider_key
                    == ReferenceLayerSource.provider_key,
                    ReferenceLayerDeliveryState.layer_id
                    == ReferenceLayerSource.layer_id,
                ),
            )
            .where(
                ReferenceLayerSource.provider_key == provider_key,
                ReferenceLayerSource.layer_id.in_(leaf_ids),
                ReferenceLayerSource.enabled.is_(True),
                or_(
                    and_(
                        ReferenceLayerDeliveryState.status == "active",
                        ReferenceSyncRun.expected_active_generation
                        == ReferenceLayerDeliveryState.generation,
                    ),
                    and_(
                        ReferenceLayerDeliveryState.layer_id.is_(None),
                        ReferenceSyncRun.expected_active_generation == 0,
                    ),
                ),
            )
            .distinct(ReferenceLayerSource.layer_id)
            .order_by(
                ReferenceLayerSource.layer_id,
                ReferenceSyncRun.queued_at.desc(),
                ReferenceSyncRun.id.desc(),
            )
        )
    }
    active_run = aliased(ReferenceSyncRun)
    active_rows: dict[
        int,
        tuple[
            ReferenceLayerDeliveryState,
            ReferenceDeliveryVersion | None,
            ReferenceSyncRun | None,
        ],
    ] = {
        state.layer_id: (state, version, version_run)
        for state, version, version_run in db.execute(
            select(
                ReferenceLayerDeliveryState,
                ReferenceDeliveryVersion,
                active_run,
            )
            .outerjoin(
                ReferenceDeliveryVersion,
                ReferenceDeliveryVersion.id
                == ReferenceLayerDeliveryState.active_version_id,
            )
            .outerjoin(
                active_run,
                and_(
                    active_run.id == ReferenceDeliveryVersion.sync_run_id,
                    active_run.source_id == ReferenceDeliveryVersion.source_id,
                ),
            )
            .where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.layer_id.in_(leaf_ids),
            )
        )
    }

    layers_by_id = {layer.id: layer for layer in layers}
    for layer_id in leaf_ids:
        source_row = source_rows.get(layer_id)
        if source_row is None:
            continue
        any_enabled, next_check_at = source_row
        run = latest_runs.get(layer_id)
        state_row = active_rows.get(layer_id)
        state = state_row[0] if state_row is not None else None
        version = state_row[1] if state_row is not None else None
        if run is None and state_row is not None:
            run = state_row[2]
        layer_availability = availability.get(layer_id)
        servable = bool(
            layer_availability is not None
            and layer_availability.delivery_available
        )
        status = _derive_status(
            any_enabled=any_enabled,
            state=state,
            version=version,
            run=run,
            servable=servable,
            serving_historical_catalog=bool(
                version is not None
                and version.catalog_snapshot_id
                != layers_by_id[layer_id].last_seen_snapshot_id
            ),
        )
        expose_active = status in {
            "active",
            "syncing",
            "serving_previous",
        } and servable
        result[layer_id] = LayerMirrorStatus(
            status=status,
            active_version_id=(
                version.id if expose_active and version is not None else None
            ),
            active_generation=(
                state.generation if expose_active and state is not None else None
            ),
            active_source_version=(
                _safe_source_version(version.source_version)
                if expose_active and version is not None
                else None
            ),
            active_reference_at=(
                version.reference_at
                if expose_active and version is not None
                else None
            ),
            active_created_at=(
                version.created_at
                if expose_active and version is not None
                else None
            ),
            last_run_id=run.id if run is not None else None,
            last_run_status=run.status if run is not None else None,
            last_run_started_at=run.started_at if run is not None else None,
            last_checked_at=run.finished_at if run is not None else None,
            last_run_duration_seconds=_run_duration_seconds(run),
            last_error_code=run.error_code if run is not None else None,
            last_error_summary=_safe_error_summary(run),
            next_check_at=next_check_at,
        )
    return result


def _derive_status(
    *,
    any_enabled: bool,
    state: ReferenceLayerDeliveryState | None,
    version: ReferenceDeliveryVersion | None,
    run: ReferenceSyncRun | None,
    servable: bool,
    serving_historical_catalog: bool,
) -> MirrorStatus:
    if state is not None and state.status == "disabled":
        return "disabled"
    active = (
        state is not None
        and state.status == "active"
        and state.active_version_id is not None
        and version is not None
    )
    if state is not None and state.status == "active" and not active:
        return "error"
    if active and not servable:
        return "error"
    if run is not None and run.status in {"queued", "running"}:
        return "syncing"
    if active:
        if serving_historical_catalog:
            return "serving_previous"
        if (
            run is not None
            and run.status in {"failed", "rejected"}
            and run.finished_at is not None
            and run.finished_at >= version.created_at
        ):
            return "serving_previous"
        return "active"
    if not any_enabled:
        return "disabled"
    if run is not None and run.status in {"failed", "rejected"}:
        return "error"
    return "pending"


def _safe_error_summary(run: ReferenceSyncRun | None) -> str | None:
    if run is None or run.error_summary is None:
        return None
    if not isinstance(run.error_summary, str) or len(run.error_summary) > 4_096:
        return None
    return run.error_summary


def _run_duration_seconds(run: ReferenceSyncRun | None) -> float | None:
    if (
        run is None
        or run.started_at is None
        or run.finished_at is None
        or run.finished_at < run.started_at
    ):
        return None
    return (run.finished_at - run.started_at).total_seconds()


def _safe_source_version(value: str | None) -> str | None:
    if value is None or not isinstance(value, str) or len(value) > 2_048:
        return None
    return value
