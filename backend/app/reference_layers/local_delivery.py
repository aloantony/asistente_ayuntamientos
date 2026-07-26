"""Resolve an active, fully local delivery without consulting an upstream."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.reference_layers.mirror_lifecycle import (
    catalog_snapshot_contains_active_layer,
    delivery_state_matches_promotion_head,
    stored_catalog_snapshot_is_valid,
    sync_run_source_definition_is_valid,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSyncRun,
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
                select(ReferenceLayerSource.enabled).where(
                    ReferenceLayerSource.provider_key
                    == current_layer.provider_key,
                    ReferenceLayerSource.layer_id == current_layer.id,
                )
            )
        )
        if configured:
            raise LocalDeliveryError(
                "local_not_ready" if any(configured) else "local_disabled"
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
    assets = list(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    return _build_selection(
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
    source_enabled_by_layer: dict[int, list[bool]] = {}
    for layer_id, enabled in db.execute(
        select(ReferenceLayerSource.layer_id, ReferenceLayerSource.enabled).where(
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.layer_id.in_(leaf_ids),
        )
    ):
        source_enabled_by_layer.setdefault(layer_id, []).append(enabled)
    active_ids = [
        state.active_version_id
        for state in states.values()
        if state.status == "active" and state.active_version_id is not None
        and delivery_state_matches_promotion_head(
            state,
            heads.get(state.layer_id),
        )
    ]
    active_records: dict[int, _ActiveRecord] = {}
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
            if (
                state is not None
                and head is not None
                and layer is not None
                and current_snapshot is not None
                and layer_blockers.get(version.layer_id) is None
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
        state = states.get(layer.id)
        layer_blocker = layer_blockers.get(layer.id)
        if layer_blocker is not None:
            if state is not None or source_enabled_by_layer.get(layer.id):
                result[layer.id] = _unavailable(layer_blocker)
            continue
        if state is None:
            configured = source_enabled_by_layer.get(layer.id)
            if configured:
                result[layer.id] = _unavailable(
                    "local_not_ready" if any(configured) else "local_disabled"
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
            result[layer.id] = _unavailable("local_version_invalid")
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
    record: _ActiveRecord,
    assets: list[ReferenceDeliveryAsset],
    *,
    style: ReferenceLayerStyle | None,
    operation: Operation,
) -> LocalDeliverySelection:
    primary_asset = _validate_active_record(record, assets)
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
    record: _ActiveRecord,
    assets: list[ReferenceDeliveryAsset],
) -> ReferenceDeliveryAsset:
    if not delivery_state_matches_promotion_head(record.state, record.head):
        raise LocalDeliveryError("local_version_invalid")
    if (
        not sync_run_source_definition_is_valid(record.run)
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
