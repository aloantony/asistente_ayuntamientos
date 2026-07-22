"""Resolve an active, fully local delivery without consulting an upstream."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSyncRun,
)
from app.reference_layers.wms_delivery import LayerDeliveryAvailability

_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
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
    state: ReferenceLayerDeliveryState
    version: ReferenceDeliveryVersion
    source: ReferenceLayerSource
    run: ReferenceSyncRun


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

    state = db.scalar(
        select(ReferenceLayerDeliveryState).where(
            ReferenceLayerDeliveryState.provider_key == layer.provider_key,
            ReferenceLayerDeliveryState.layer_id == layer.id,
        )
    )
    if state is None:
        return None
    if state.status != "active" or state.active_version_id is None:
        raise LocalDeliveryError("local_disabled")
    row = db.execute(
        select(
            ReferenceDeliveryVersion,
            ReferenceLayerSource,
            ReferenceSyncRun,
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
            ReferenceSyncRun,
            and_(
                ReferenceSyncRun.id == ReferenceDeliveryVersion.sync_run_id,
                ReferenceSyncRun.source_id == ReferenceDeliveryVersion.source_id,
            ),
        )
        .where(
            ReferenceDeliveryVersion.id == state.active_version_id,
            ReferenceDeliveryVersion.provider_key == layer.provider_key,
            ReferenceDeliveryVersion.layer_id == layer.id,
        )
    ).one_or_none()
    if row is None:
        raise LocalDeliveryError("local_version_invalid")
    version, source, run = row
    assets = list(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    return _build_selection(
        _ActiveRecord(state, version, source, run),
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
    states = {
        state.layer_id: state
        for state in db.scalars(
            select(ReferenceLayerDeliveryState).where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.layer_id.in_(leaf_ids),
            )
        )
    }
    active_ids = [
        state.active_version_id
        for state in states.values()
        if state.status == "active" and state.active_version_id is not None
    ]
    active_records: dict[int, _ActiveRecord] = {}
    if active_ids:
        rows = db.execute(
            select(
                ReferenceDeliveryVersion,
                ReferenceLayerSource,
                ReferenceSyncRun,
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
            .where(ReferenceDeliveryVersion.id.in_(active_ids))
        ).all()
        for version, source, run in rows:
            state = states.get(version.layer_id)
            if state is not None and state.active_version_id == version.id:
                active_records[version.layer_id] = _ActiveRecord(
                    state,
                    version,
                    source,
                    run,
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
        if state is None:
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


def _build_selection(
    record: _ActiveRecord,
    assets: list[ReferenceDeliveryAsset],
    *,
    style: ReferenceLayerStyle | None,
    operation: Operation,
) -> LocalDeliverySelection:
    if (
        record.run.status != "succeeded"
        or record.run.source_definition_sha256
        != record.source.definition_sha256
    ):
        raise LocalDeliveryError("local_source_changed")
    primary = [asset for asset in assets if asset.is_primary]
    if len(primary) != 1:
        raise LocalDeliveryError("local_version_invalid")
    primary_asset = primary[0]
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
        or selected.storage_backend != "filesystem"
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
