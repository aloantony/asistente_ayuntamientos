"""Resolve an active, fully local delivery without consulting an upstream."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.reference_layers.mirror_lifecycle import (
    catalog_snapshot_contains_active_layer,
    delivery_state_matches_promotion_head,
    stored_catalog_snapshot_is_valid,
    stored_source_definition_is_valid,
    sync_run_source_definition_is_valid,
)
from app.reference_layers.mirror_authorization import (
    source_authorization_blocker,
    version_authorization_blocker,
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
    ReferenceLayerMirrorStrategy,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceService,
    ReferenceStyleParityPlan,
    ReferenceStyleParityPlanItem,
    ReferenceSyncRun,
)
from app.reference_layers.reviewed_ortho_evidence import (
    ReviewedOrthoEvidenceError,
    require_reviewed_ign_ortho_delivery_allowed,
)
from app.reference_layers.wms_delivery import LayerDeliveryAvailability

_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_POSTGIS_KEY = re.compile(r"^reference_data\.[a-z][a-z0-9_]{0,62}$", re.ASCII)
_BLOB_KEY = re.compile(
    r"^blobs/sha256/(?P<prefix>[0-9a-f]{2})/(?P<sha>[0-9a-f]{64})$",
    re.ASCII,
)
Operation = Literal["tile", "legend", "identify"]


class LocalDeliveryError(Exception):
    """An explicitly configured local delivery is not safe to serve."""

    def __init__(self, blocker: str) -> None:
        super().__init__(blocker)
        self.blocker = blocker


@dataclass(frozen=True)
class LocalDeliverySelection:
    backend: Literal["geoserver", "tile_archive"]
    version_id: int
    generation: int
    delivery_kind: str
    asset_id: int
    asset_sha256: str
    storage_key: str
    layer_name: str | None
    style_name: str | None
    content_type: str
    identify_available: bool
    legend_available: bool


@dataclass(frozen=True)
class _ActiveRecord:
    layer: ReferenceLayer
    current_snapshot: ReferenceCatalogSnapshot
    state: ReferenceLayerDeliveryState
    version: ReferenceDeliveryVersion
    source: ReferenceLayerSource
    run: ReferenceSyncRun
    snapshot: ReferenceCatalogSnapshot
    head: ReferenceDeliveryPromotion


def resolve_local_delivery(
    db: Session,
    *,
    layer: ReferenceLayer,
    style: ReferenceLayerStyle | None,
    operation: Operation,
) -> LocalDeliverySelection | None:
    """Resolve local delivery, returning ``None`` only when no state exists.

    Once a layer has a local state, corruption or an explicit deactivation must
    not silently fall back to the remote WMS path.
    """

    current_layer, current_snapshot = _load_current_layer_context(
        db,
        layer=layer,
    )
    state = db.scalar(
        select(ReferenceLayerDeliveryState).where(
            ReferenceLayerDeliveryState.provider_key
            == current_layer.provider_key,
            ReferenceLayerDeliveryState.layer_id == current_layer.id,
        )
    )
    if state is None:
        configured = list(
            db.scalars(
                select(ReferenceLayerSource)
                .where(
                    ReferenceLayerSource.provider_key
                    == current_layer.provider_key,
                    ReferenceLayerSource.layer_id == current_layer.id,
                )
                .order_by(
                    ReferenceLayerSource.is_primary.desc(),
                    ReferenceLayerSource.priority,
                    ReferenceLayerSource.id,
                )
            )
        )
        if configured:
            enabled = [source for source in configured if source.enabled]
            blockers = [
                source_authorization_blocker(db, source=source)
                for source in enabled
            ]
            authorization_blocker = next(
                (blocker for blocker in blockers if blocker is not None),
                None,
            )
            if enabled and all(blockers):
                raise LocalDeliveryError(
                    authorization_blocker
                    or "mirror_authorization_missing"
                )
            raise LocalDeliveryError(
                "local_not_ready" if enabled else "local_disabled"
            )
        return None
    head = db.scalar(
        select(ReferenceDeliveryPromotion)
        .where(
            ReferenceDeliveryPromotion.provider_key
            == current_layer.provider_key,
            ReferenceDeliveryPromotion.layer_id == current_layer.id,
        )
        .order_by(ReferenceDeliveryPromotion.sequence_number.desc())
        .limit(1)
    )
    if not delivery_state_matches_promotion_head(state, head):
        raise LocalDeliveryError("local_version_invalid")
    assert head is not None
    if state.status != "active" or state.active_version_id is None:
        raise LocalDeliveryError("local_disabled")
    row = db.execute(
        select(
            ReferenceDeliveryVersion,
            ReferenceLayerSource,
            ReferenceSyncRun,
            ReferenceCatalogSnapshot,
        )
        .join(
            ReferenceLayerSource,
            and_(
                ReferenceLayerSource.id == ReferenceDeliveryVersion.source_id,
                ReferenceLayerSource.provider_key
                == ReferenceDeliveryVersion.provider_key,
                ReferenceLayerSource.layer_id == ReferenceDeliveryVersion.layer_id,
            ),
        )
        .join(
            ReferenceCatalogSnapshot,
            and_(
                ReferenceCatalogSnapshot.id
                == ReferenceDeliveryVersion.catalog_snapshot_id,
                ReferenceCatalogSnapshot.provider_key
                == ReferenceDeliveryVersion.provider_key,
            ),
        )
        .join(
            ReferenceSyncRun,
            and_(
                ReferenceSyncRun.id == ReferenceDeliveryVersion.sync_run_id,
                ReferenceSyncRun.source_id == ReferenceDeliveryVersion.source_id,
            ),
        )
        .where(
            ReferenceDeliveryVersion.id == state.active_version_id,
            ReferenceDeliveryVersion.provider_key
            == current_layer.provider_key,
            ReferenceDeliveryVersion.layer_id == current_layer.id,
        )
    ).one_or_none()
    if row is None:
        raise LocalDeliveryError("local_version_invalid")
    version, source, run, snapshot = row
    authorization_blocker = version_authorization_blocker(
        db,
        version=version,
        source=source,
        run=run,
    )
    if authorization_blocker is not None:
        raise LocalDeliveryError(authorization_blocker)
    configured_style_versions = _configured_style_parity_versions(
        db,
        [version.id],
    )
    if (
        version.id in configured_style_versions
        and version.id
        not in _complete_style_parity_versions(db, [version.id])
    ):
        raise LocalDeliveryError("style_parity_incomplete")
    assets = list(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    return _build_selection(
        db,
        _ActiveRecord(
            current_layer,
            current_snapshot,
            state,
            version,
            source,
            run,
            snapshot,
            head,
        ),
        assets,
        style=style,
        operation=operation,
    )


def catalog_local_delivery_availability(
    db: Session,
    *,
    provider_key: str,
    layers: list[ReferenceLayer],
    styles: list[ReferenceLayerStyle],
) -> dict[int, LayerDeliveryAvailability | None]:
    """Resolve catalog availability in a bounded number of database queries.

    ``None`` means that local delivery has never been configured and the caller
    may evaluate the legacy attested proxy.  Any returned unavailable value is
    authoritative and blocks an accidental remote fallback.
    """

    leaf_ids = [item.id for item in layers if item.node_type == "layer"]
    result: dict[int, LayerDeliveryAvailability | None] = {
        item.id: None for item in layers
    }
    if not leaf_ids:
        return result
    current_snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    strategy_rows = {}
    if current_snapshot is not None:
        strategy_rows = {
            row.layer_id: row
            for row in db.scalars(
                select(ReferenceLayerMirrorStrategy).where(
                    ReferenceLayerMirrorStrategy.provider_key == provider_key,
                    ReferenceLayerMirrorStrategy.catalog_snapshot_id
                    == current_snapshot.id,
                )
            )
        }
    strategy_matrix_configured = bool(strategy_rows)
    layer_blockers = {
        layer.id: _current_layer_blocker(
            layer,
            current_snapshot,
        )
        for layer in layers
        if layer.node_type == "layer"
    }
    layers_by_id = {layer.id: layer for layer in layers}
    states = {
        state.layer_id: state
        for state in db.scalars(
            select(ReferenceLayerDeliveryState).where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.layer_id.in_(leaf_ids),
            )
        )
    }
    heads = {
        promotion.layer_id: promotion
        for promotion in db.scalars(
            select(ReferenceDeliveryPromotion)
            .where(
                ReferenceDeliveryPromotion.provider_key == provider_key,
                ReferenceDeliveryPromotion.layer_id.in_(leaf_ids),
            )
            .distinct(ReferenceDeliveryPromotion.layer_id)
            .order_by(
                ReferenceDeliveryPromotion.layer_id,
                ReferenceDeliveryPromotion.sequence_number.desc(),
            )
        )
    }
    sources_by_layer: dict[int, list[ReferenceLayerSource]] = {}
    for source in db.scalars(
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.layer_id.in_(leaf_ids),
        )
        .order_by(
            ReferenceLayerSource.layer_id,
            ReferenceLayerSource.is_primary.desc(),
            ReferenceLayerSource.priority,
            ReferenceLayerSource.id,
        )
    ):
        sources_by_layer.setdefault(source.layer_id, []).append(source)
    active_ids = [
        state.active_version_id
        for state in states.values()
        if state.status == "active" and state.active_version_id is not None
        and delivery_state_matches_promotion_head(
            state,
            heads.get(state.layer_id),
        )
    ]
    complete_style_versions = _complete_style_parity_versions(
        db,
        active_ids,
    )
    configured_style_versions = _configured_style_parity_versions(
        db,
        active_ids,
    )
    active_records: dict[int, _ActiveRecord] = {}
    active_authorization_blockers: dict[int, str] = {}
    if active_ids:
        rows = db.execute(
            select(
                ReferenceDeliveryVersion,
                ReferenceLayerSource,
                ReferenceSyncRun,
                ReferenceCatalogSnapshot,
            )
            .join(
                ReferenceLayerSource,
                and_(
                    ReferenceLayerSource.id == ReferenceDeliveryVersion.source_id,
                    ReferenceLayerSource.provider_key
                    == ReferenceDeliveryVersion.provider_key,
                    ReferenceLayerSource.layer_id
                    == ReferenceDeliveryVersion.layer_id,
                ),
            )
            .join(
                ReferenceSyncRun,
                and_(
                    ReferenceSyncRun.id == ReferenceDeliveryVersion.sync_run_id,
                    ReferenceSyncRun.source_id
                    == ReferenceDeliveryVersion.source_id,
                ),
            )
            .join(
                ReferenceCatalogSnapshot,
                and_(
                    ReferenceCatalogSnapshot.id
                    == ReferenceDeliveryVersion.catalog_snapshot_id,
                    ReferenceCatalogSnapshot.provider_key
                    == ReferenceDeliveryVersion.provider_key,
                ),
            )
            .where(ReferenceDeliveryVersion.id.in_(active_ids))
        ).all()
        for version, source, run, snapshot in rows:
            state = states.get(version.layer_id)
            head = heads.get(version.layer_id)
            layer = layers_by_id.get(version.layer_id)
            authorization_blocker = version_authorization_blocker(
                db,
                version=version,
                source=source,
                run=run,
            )
            if authorization_blocker is not None:
                active_authorization_blockers[
                    version.layer_id
                ] = authorization_blocker
            if (
                state is not None
                and head is not None
                and layer is not None
                and current_snapshot is not None
                and layer_blockers.get(version.layer_id) is None
                and (
                    version.id not in configured_style_versions
                    or version.id in complete_style_versions
                )
                and authorization_blocker is None
                and state.active_version_id == version.id
                and delivery_state_matches_promotion_head(state, head)
            ):
                active_records[version.layer_id] = _ActiveRecord(
                    layer,
                    current_snapshot,
                    state,
                    version,
                    source,
                    run,
                    snapshot,
                    head,
                )
    version_ids = [record.version.id for record in active_records.values()]
    assets_by_version: dict[int, list[ReferenceDeliveryAsset]] = {}
    if version_ids:
        for asset in db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id.in_(version_ids))
            .order_by(ReferenceDeliveryAsset.version_id, ReferenceDeliveryAsset.id)
        ):
            assets_by_version.setdefault(asset.version_id, []).append(asset)
    styles_by_layer: dict[int, list[ReferenceLayerStyle]] = {}
    for style in styles:
        if style.status in {"active", "degraded"}:
            styles_by_layer.setdefault(style.layer_id, []).append(style)

    for layer in layers:
        if layer.node_type != "layer":
            continue
        strategy = strategy_rows.get(layer.id)
        if strategy_matrix_configured and strategy is None:
            result[layer.id] = _unavailable("strategy_missing")
            continue
        if strategy is not None and strategy.strategy == "blocked":
            result[layer.id] = _unavailable(strategy.strategy_reason_code)
            continue
        if strategy is not None and strategy.strategy == "composition":
            result[layer.id] = _unavailable("composition_not_materialized")
            continue
        state = states.get(layer.id)
        layer_blocker = layer_blockers.get(layer.id)
        if layer_blocker is not None:
            if state is not None or sources_by_layer.get(layer.id):
                result[layer.id] = _unavailable(layer_blocker)
            continue
        if state is None:
            configured = sources_by_layer.get(layer.id)
            if configured:
                enabled_sources = [
                    source for source in configured if source.enabled
                ]
                authorization_blockers = [
                    source_authorization_blocker(
                        db,
                        source=source,
                    )
                    for source in enabled_sources
                ]
                if enabled_sources and all(authorization_blockers):
                    result[layer.id] = _unavailable(
                        next(
                            blocker
                            for blocker in authorization_blockers
                            if blocker is not None
                        )
                    )
                    continue
                result[layer.id] = _unavailable(
                    "local_not_ready"
                    if enabled_sources
                    else "local_disabled"
                )
            continue
        if not delivery_state_matches_promotion_head(
            state,
            heads.get(layer.id),
        ):
            result[layer.id] = _unavailable("local_version_invalid")
            continue
        if state.status != "active" or state.active_version_id is None:
            result[layer.id] = _unavailable("local_disabled")
            continue
        record = active_records.get(layer.id)
        if record is None:
            blocker = (
                active_authorization_blockers.get(layer.id)
                or (
                    "style_parity_incomplete"
                if state.active_version_id in configured_style_versions
                and state.active_version_id not in complete_style_versions
                else "local_version_invalid"
                )
            )
            result[layer.id] = _unavailable(blocker)
            continue
        assets = assets_by_version.get(record.version.id, [])
        layer_styles = styles_by_layer.get(layer.id, [])
        available_style_ids: list[int] = []
        legend_style_ids: list[int] = []
        identify_available = False
        blocker: str | None = None
        default_selection: LocalDeliverySelection | None = None
        if layer_styles:
            for style in layer_styles:
                try:
                    selection = _build_selection(
                        db,
                        record,
                        assets,
                        style=style,
                        operation="tile",
                    )
                except LocalDeliveryError as exc:
                    blocker = blocker or exc.blocker
                    continue
                available_style_ids.append(style.id)
                if selection.legend_available:
                    legend_style_ids.append(style.id)
                identify_available = (
                    identify_available or selection.identify_available
                )
                if style.is_default:
                    default_selection = selection
        else:
            try:
                default_selection = _build_selection(
                    db,
                    record,
                    assets,
                    style=None,
                    operation="tile",
                )
            except LocalDeliveryError as exc:
                blocker = exc.blocker
            if default_selection is not None:
                identify_available = default_selection.identify_available

        delivery_available = default_selection is not None
        if layer_styles and not delivery_available and available_style_ids:
            blocker = "style_unsupported"
        elif not delivery_available:
            blocker = blocker or "local_version_invalid"
        result[layer.id] = LayerDeliveryAvailability(
            delivery_available=delivery_available,
            legend_available=(
                default_selection.legend_available
                if default_selection is not None
                else bool(legend_style_ids)
            ),
            identify_available=identify_available and layer.queryable,
            delivery_blocker=blocker,
            available_style_ids=tuple(available_style_ids),
            available_legend_style_ids=tuple(legend_style_ids),
        )
    return result


def _complete_style_parity_versions(
    db: Session,
    version_ids: list[int],
) -> set[int]:
    if not version_ids:
        return set()
    version_plans = {
        version.id: (version, plan)
        for version, plan in db.execute(
            select(
                ReferenceDeliveryVersion,
                ReferenceStyleParityPlan,
            )
            .join(
                ReferenceStyleParityPlan,
                and_(
                    ReferenceStyleParityPlan.source_id
                    == ReferenceDeliveryVersion.source_id,
                    ReferenceStyleParityPlan.sync_run_id
                    == ReferenceDeliveryVersion.sync_run_id,
                ),
            )
            .where(ReferenceDeliveryVersion.id.in_(version_ids))
        )
    }
    if not version_plans:
        return set()
    plan_ids = [plan.id for _version, plan in version_plans.values()]
    items_by_plan: dict[int, list[ReferenceStyleParityPlanItem]] = {}
    for item in db.scalars(
        select(ReferenceStyleParityPlanItem).where(
            ReferenceStyleParityPlanItem.plan_id.in_(plan_ids)
        )
    ):
        items_by_plan.setdefault(item.plan_id, []).append(item)
    parities_by_version: dict[int, list[ReferenceDeliveryStyleParity]] = {}
    parity_ids: list[int] = []
    for parity in db.scalars(
        select(ReferenceDeliveryStyleParity).where(
            ReferenceDeliveryStyleParity.version_id.in_(version_ids)
        )
    ):
        parities_by_version.setdefault(parity.version_id, []).append(
            parity
        )
        parity_ids.append(parity.id)
    resource_counts = {
        parity_id: int(count)
        for parity_id, count in db.execute(
            select(
                ReferenceDeliveryStyleResource.delivery_parity_id,
                func.count(),
            )
            .where(
                ReferenceDeliveryStyleResource.delivery_parity_id.in_(
                    parity_ids
                )
            )
            .group_by(
                ReferenceDeliveryStyleResource.delivery_parity_id
            )
        )
    } if parity_ids else {}
    complete: set[int] = set()
    for version_id, (version, plan) in version_plans.items():
        items = items_by_plan.get(plan.id, [])
        parities = parities_by_version.get(version_id, [])
        gate = (
            version.validation_json.get("style_parity_gate")
            if isinstance(version.validation_json, dict)
            else None
        )
        if (
            not plan.complete
            or plan.missing_style_count != 0
            or plan.provider_key != version.provider_key
            or plan.layer_id != version.layer_id
            or plan.catalog_snapshot_id != version.catalog_snapshot_id
            or plan.catalog_definition_sha256
            != version.catalog_definition_sha256
            or plan.delivery_kind != version.delivery_kind
            or _json_sha256(plan.evidence_json) != plan.evidence_sha256
            or len(items) != plan.required_style_count
            or len(parities) != len(items)
            or {item.id for item in items}
            != {parity.plan_item_id for parity in parities}
            or any(
                item.parity_kind == "missing"
                or not item.verified
                or _json_sha256(item.evidence_json)
                != item.evidence_sha256
                for item in items
            )
            or any(
                not parity.verified
                or _json_sha256(parity.evidence_json)
                != parity.evidence_sha256
                or resource_counts.get(parity.id, 0)
                != parity.resource_count
                for parity in parities
            )
            or not isinstance(gate, dict)
            or gate.get("passed") is not True
            or gate.get("plan_id") != plan.id
            or gate.get("plan_evidence_sha256")
            != plan.evidence_sha256
            or gate.get("required_style_count") != len(items)
            or gate.get("verified_style_count") != len(items)
            or gate.get("missing_style_count") != 0
        ):
            continue
        complete.add(version_id)
    return complete


def _configured_style_parity_versions(
    db: Session,
    version_ids: list[int],
) -> set[int]:
    if not version_ids:
        return set()
    return set(
        db.scalars(
            select(ReferenceDeliveryVersion.id)
            .join(
                ReferenceStyleParityPlan,
                and_(
                    ReferenceStyleParityPlan.source_id
                    == ReferenceDeliveryVersion.source_id,
                    ReferenceStyleParityPlan.sync_run_id
                    == ReferenceDeliveryVersion.sync_run_id,
                ),
            )
            .where(ReferenceDeliveryVersion.id.in_(version_ids))
        )
    )


def _json_sha256(value: Any) -> str | None:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _load_current_layer_context(
    db: Session,
    *,
    layer: ReferenceLayer,
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
            ReferenceLayer.id == layer.id,
            ReferenceLayer.provider_key == layer.provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    ).one_or_none()
    if row is None:
        raise LocalDeliveryError("local_disabled")
    current_layer, current_snapshot = row
    blocker = _current_layer_blocker(current_layer, current_snapshot)
    if blocker is not None:
        raise LocalDeliveryError(blocker)
    return current_layer, current_snapshot


def _current_layer_blocker(
    layer: ReferenceLayer,
    snapshot: ReferenceCatalogSnapshot | None,
) -> str | None:
    if (
        snapshot is None
        or layer.provider_key != snapshot.provider_key
        or layer.last_seen_snapshot_id != snapshot.id
        or layer.node_type != "layer"
        or layer.status not in {"active", "degraded"}
    ):
        return "local_disabled"
    if (
        not snapshot.is_current
        or not stored_catalog_snapshot_is_valid(snapshot)
        or not catalog_snapshot_contains_active_layer(snapshot, layer)
    ):
        return "local_version_invalid"
    return None


def _build_selection(
    db: Session,
    record: _ActiveRecord,
    assets: list[ReferenceDeliveryAsset],
    *,
    style: ReferenceLayerStyle | None,
    operation: Operation,
) -> LocalDeliverySelection:
    primary_asset = _validate_active_record(db, record, assets)
    if record.version.delivery_kind in {"vector", "raster"}:
        return _geoserver_selection(
            record,
            primary_asset,
            style=style,
            operation=operation,
        )
    if record.version.delivery_kind == "tiles":
        return _tile_archive_selection(
            record,
            assets,
            primary_asset=primary_asset,
            style=style,
            operation=operation,
        )
    raise LocalDeliveryError("local_version_invalid")


def _validate_active_record(
    db: Session,
    record: _ActiveRecord,
    assets: list[ReferenceDeliveryAsset],
) -> ReferenceDeliveryAsset:
    if not delivery_state_matches_promotion_head(record.state, record.head):
        raise LocalDeliveryError("local_version_invalid")
    if (
        not sync_run_source_definition_is_valid(record.run)
        or not stored_source_definition_is_valid(record.source)
        or record.source.definition_sha256
        != record.run.source_definition_sha256
        or record.run.status != "succeeded"
    ):
        raise LocalDeliveryError("local_source_changed")
    if (
        record.layer.id != record.version.layer_id
        or record.layer.provider_key != record.version.provider_key
        or record.source.provider_key != record.version.provider_key
        or record.source.layer_id != record.version.layer_id
        or record.source.id != record.version.source_id
        or record.run.id != record.version.sync_run_id
        or record.run.source_id != record.version.source_id
        or record.run.provider_key != record.version.provider_key
        or record.run.layer_id != record.version.layer_id
        or not isinstance(record.run.source_definition_json, dict)
        or record.run.source_definition_json.get("target_kind")
        != record.version.delivery_kind
    ):
        raise LocalDeliveryError("local_version_invalid")
    if record.layer.service_id is not None:
        service = db.scalar(
            select(ReferenceService).where(
                ReferenceService.provider_key
                == record.layer.provider_key,
                ReferenceService.id == record.layer.service_id,
            )
        )
        if service is None:
            raise LocalDeliveryError("local_version_invalid")
        catalog_layer = (
            record.layer.remote_name or record.layer.source_key
        )
        try:
            require_reviewed_ign_ortho_delivery_allowed(
                catalog_endpoint_url=service.base_url,
                catalog_layer=catalog_layer,
                source_definition=record.run.source_definition_json,
                validation_json=record.version.validation_json,
                content_sha256=record.version.content_sha256,
            )
        except ReviewedOrthoEvidenceError:
            blocker = (
                "reviewed_ortho_2021_blocked"
                if catalog_layer == "Ortofoto_2021"
                else "reviewed_ortho_legacy_fenced"
            )
            raise LocalDeliveryError(blocker) from None
    try:
        catalog_hash_is_valid = (
            record.snapshot.id == record.version.catalog_snapshot_id
            and record.snapshot.provider_key == record.version.provider_key
            and record.snapshot.definition_sha256
            == record.version.catalog_definition_sha256
            and stored_catalog_snapshot_is_valid(record.snapshot)
            and catalog_snapshot_contains_active_layer(
                record.snapshot,
                record.layer,
            )
            and record.current_snapshot.id
            == record.layer.last_seen_snapshot_id
            and record.current_snapshot.is_current
            and stored_catalog_snapshot_is_valid(record.current_snapshot)
            and catalog_snapshot_contains_active_layer(
                record.current_snapshot,
                record.layer,
            )
        )
        validation_hash = hashlib.sha256(
            json.dumps(
                record.version.validation_json,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError, RecursionError):
        raise LocalDeliveryError("local_version_invalid") from None
    if (
        not catalog_hash_is_valid
        or not isinstance(record.version.validation_json, dict)
        or record.version.validation_json.get("passed") is not True
        or validation_hash != record.version.validation_sha256
        or _SHA256.fullmatch(record.version.content_sha256) is None
        or _SHA256.fullmatch(record.version.manifest_sha256) is None
    ):
        raise LocalDeliveryError("local_version_invalid")
    primary = [asset for asset in assets if asset.is_primary]
    if len(primary) != 1:
        raise LocalDeliveryError("local_version_invalid")
    asset = primary[0]
    expected_kind = {
        "vector": "vector_table",
        "raster": "raster_cog",
        "tiles": "tile_archive",
    }.get(record.version.delivery_kind)
    if (
        asset.asset_kind != expected_kind
        or _SHA256.fullmatch(asset.sha256) is None
        or asset.sha256 != record.version.content_sha256
        or not isinstance(asset.media_type, str)
        or not 1 <= len(asset.media_type) <= 255
    ):
        raise LocalDeliveryError("local_version_invalid")
    if record.version.delivery_kind == "vector":
        if (
            asset.storage_backend != "postgres"
            or _POSTGIS_KEY.fullmatch(asset.storage_key) is None
        ):
            raise LocalDeliveryError("local_version_invalid")
    else:
        if not _filesystem_asset_is_valid(asset):
            raise LocalDeliveryError("local_version_invalid")
    return asset


def _geoserver_selection(
    record: _ActiveRecord,
    asset: ReferenceDeliveryAsset,
    *,
    style: ReferenceLayerStyle | None,
    operation: Operation,
) -> LocalDeliverySelection:
    expected_kind = (
        "vector_table" if record.version.delivery_kind == "vector" else "raster_cog"
    )
    if asset.asset_kind != expected_kind:
        raise LocalDeliveryError("local_version_invalid")
    metadata = _metadata(asset.metadata_json)
    if metadata.get("renderer") != "geoserver":
        raise LocalDeliveryError("local_version_invalid")
    layer_name = _resource(metadata.get("layer_name"))
    styles = _style_map(metadata.get("styles"))
    if style is None:
        raw_default = metadata.get("default_style_name")
        style_name = None if raw_default is None else _resource(raw_default)
    else:
        style_name = styles.get(style.id)
        if style_name is None:
            raise LocalDeliveryError("style_unsupported")
    identify = metadata.get("identify_available") is True
    legend = metadata.get("legend_available") is True
    if operation == "identify" and not identify:
        raise LocalDeliveryError("local_identify_unavailable")
    if operation == "legend" and not legend:
        raise LocalDeliveryError("local_legend_unavailable")
    return LocalDeliverySelection(
        backend="geoserver",
        version_id=record.version.id,
        generation=record.state.generation,
        delivery_kind=record.version.delivery_kind,
        asset_id=asset.id,
        asset_sha256=asset.sha256,
        storage_key=asset.storage_key,
        layer_name=layer_name,
        style_name=style_name,
        content_type=asset.media_type,
        identify_available=identify,
        legend_available=legend,
    )


def _tile_archive_selection(
    record: _ActiveRecord,
    assets: list[ReferenceDeliveryAsset],
    *,
    primary_asset: ReferenceDeliveryAsset,
    style: ReferenceLayerStyle | None,
    operation: Operation,
) -> LocalDeliverySelection:
    if operation != "tile":
        raise LocalDeliveryError(
            "local_legend_unavailable"
            if operation == "legend"
            else "local_identify_unavailable"
        )
    archives = [asset for asset in assets if asset.asset_kind == "tile_archive"]
    selected: ReferenceDeliveryAsset | None = None
    for asset in archives:
        metadata = _metadata(asset.metadata_json)
        style_id = metadata.get("catalog_style_id")
        if style_id is not None and (
            isinstance(style_id, bool) or not isinstance(style_id, int)
        ):
            raise LocalDeliveryError("local_version_invalid")
        if style is not None and style_id == style.id:
            selected = asset
            break
        if style is None and asset.id == primary_asset.id and style_id is None:
            selected = asset
    if selected is None:
        raise LocalDeliveryError(
            "style_unsupported" if style is not None else "local_version_invalid"
        )
    metadata = _metadata(selected.metadata_json)
    if (
        metadata.get("renderer") != "tile_archive"
        or not _filesystem_asset_is_valid(selected)
    ):
        raise LocalDeliveryError("local_version_invalid")
    return LocalDeliverySelection(
        backend="tile_archive",
        version_id=record.version.id,
        generation=record.state.generation,
        delivery_kind="tiles",
        asset_id=selected.id,
        asset_sha256=selected.sha256,
        storage_key=selected.storage_key,
        layer_name=None,
        style_name=None,
        content_type=selected.media_type,
        identify_available=False,
        legend_available=False,
    )


def _filesystem_asset_is_valid(asset: ReferenceDeliveryAsset) -> bool:
    match = _BLOB_KEY.fullmatch(asset.storage_key)
    return bool(
        asset.storage_backend == "filesystem"
        and _SHA256.fullmatch(asset.sha256) is not None
        and match is not None
        and match.group("prefix") == asset.sha256[:2]
        and match.group("sha") == asset.sha256
        and not isinstance(asset.size_bytes, bool)
        and isinstance(asset.size_bytes, int)
        and asset.size_bytes > 0
    )


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 64:
        raise LocalDeliveryError("local_version_invalid")
    return value


def _style_map(value: Any) -> dict[int, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 256:
        raise LocalDeliveryError("local_version_invalid")
    result: dict[int, str] = {}
    for raw_id, raw_name in value.items():
        if (
            not isinstance(raw_id, str)
            or not raw_id.isascii()
            or not raw_id.isdecimal()
        ):
            raise LocalDeliveryError("local_version_invalid")
        style_id = int(raw_id)
        if style_id < 1 or style_id in result:
            raise LocalDeliveryError("local_version_invalid")
        result[style_id] = _resource(raw_name)
    return result


def _resource(value: Any) -> str:
    if not isinstance(value, str) or _RESOURCE_NAME.fullmatch(value) is None:
        raise LocalDeliveryError("local_version_invalid")
    return value


def _unavailable(blocker: str) -> LayerDeliveryAvailability:
    return LayerDeliveryAvailability(
        delivery_available=False,
        legend_available=False,
        identify_available=False,
        delivery_blocker=blocker,
        available_style_ids=(),
        available_legend_style_ids=(),
    )
