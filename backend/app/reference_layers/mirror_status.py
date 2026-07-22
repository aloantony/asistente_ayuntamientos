"""Bounded operational status projection for the reference-layer catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.reference_layers.models import (
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceSyncRun,
)

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
    last_run_status: str | None = None
    last_checked_at: datetime | None = None
    last_error_code: str | None = None
    last_error_summary: str | None = None
    next_check_at: datetime | None = None


def catalog_mirror_statuses(
    db: Session,
    *,
    provider_key: str,
    layers: list[ReferenceLayer],
) -> dict[int, LayerMirrorStatus]:
    """Return one operational mirror state per catalog node in four queries."""

    leaf_ids = [item.id for item in layers if item.node_type == "layer"]
    result = {
        item.id: LayerMirrorStatus(
            status="not_applicable" if item.node_type != "layer" else "legacy"
        )
        for item in layers
    }
    if not leaf_ids:
        return result

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
            .where(
                ReferenceLayerSource.provider_key == provider_key,
                ReferenceLayerSource.layer_id.in_(leaf_ids),
            )
            .distinct(ReferenceLayerSource.layer_id)
            .order_by(
                ReferenceLayerSource.layer_id,
                ReferenceSyncRun.queued_at.desc(),
                ReferenceSyncRun.id.desc(),
            )
        )
    }
    active_rows: dict[
        int,
        tuple[ReferenceLayerDeliveryState, ReferenceDeliveryVersion | None],
    ] = {
        state.layer_id: (state, version)
        for state, version in db.execute(
            select(ReferenceLayerDeliveryState, ReferenceDeliveryVersion)
            .outerjoin(
                ReferenceDeliveryVersion,
                ReferenceDeliveryVersion.id
                == ReferenceLayerDeliveryState.active_version_id,
            )
            .where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.layer_id.in_(leaf_ids),
            )
        )
    }

    for layer_id in leaf_ids:
        source_row = source_rows.get(layer_id)
        if source_row is None:
            continue
        any_enabled, next_check_at = source_row
        run = latest_runs.get(layer_id)
        state_row = active_rows.get(layer_id)
        state = state_row[0] if state_row is not None else None
        version = state_row[1] if state_row is not None else None
        status = _derive_status(
            any_enabled=any_enabled,
            state=state,
            version=version,
            run=run,
        )
        result[layer_id] = LayerMirrorStatus(
            status=status,
            active_version_id=version.id if version is not None else None,
            active_generation=state.generation if state is not None else None,
            active_source_version=(
                version.source_version if version is not None else None
            ),
            active_reference_at=(
                version.reference_at if version is not None else None
            ),
            active_created_at=(version.created_at if version is not None else None),
            last_run_status=run.status if run is not None else None,
            last_checked_at=run.finished_at if run is not None else None,
            last_error_code=run.error_code if run is not None else None,
            last_error_summary=run.error_summary if run is not None else None,
            next_check_at=next_check_at,
        )
    return result


def _derive_status(
    *,
    any_enabled: bool,
    state: ReferenceLayerDeliveryState | None,
    version: ReferenceDeliveryVersion | None,
    run: ReferenceSyncRun | None,
) -> MirrorStatus:
    if state is not None and state.status == "disabled":
        return "disabled"
    active = (
        state is not None
        and state.status == "active"
        and state.active_version_id is not None
        and version is not None
    )
    if run is not None and run.status in {"queued", "running"}:
        return "syncing"
    if active:
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
