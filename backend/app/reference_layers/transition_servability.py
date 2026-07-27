"""Fail-closed physical verification for delivery recovery transitions.

Promotion validates freshly materialized resources.  Rollback and explicit
reactivation can happen much later, so their immutable database evidence is
not enough: the CAS blobs, PostGIS relation and direct local renderer must
still be present and usable immediately before the state transition.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers import geo_ingest
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.geoserver_admin import (
    GeoServerAdminClient,
    LayerSmokeResult,
)
from app.reference_layers.local_tile_archive import LocalTileArchiveRenderer
from app.reference_layers.mirror_lifecycle import (
    DeliveryPhysicalTransitionVerification,
)
from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_BLOB_KEY_RE = re.compile(
    r"^blobs/sha256/(?P<prefix>[0-9a-f]{2})/(?P<sha>[0-9a-f]{64})$",
    re.ASCII,
)
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
_FILE_CHUNK_BYTES = 1024 * 1024


class DeliveryTransitionServabilityError(RuntimeError):
    """A historical delivery is no longer physically safe to activate."""


def verify_delivery_transition_servability(
    store: ReferenceBlobStore,
    db: Session,
    version: ReferenceDeliveryVersion,
    *,
    geoserver: GeoServerAdminClient | None = None,
) -> DeliveryPhysicalTransitionVerification:
    """Verify every physical dependency while lifecycle locks remain held."""

    assets = tuple(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    primary_assets = tuple(item for item in assets if item.is_primary)
    if len(primary_assets) != 1:
        raise DeliveryTransitionServabilityError(
            "delivery has no unique primary physical asset"
        )
    primary = primary_assets[0]
    if (
        primary.sha256 != version.content_sha256
        or _SHA256_RE.fullmatch(primary.sha256) is None
    ):
        raise DeliveryTransitionServabilityError(
            "delivery primary physical identity is invalid"
        )
    filesystem_assets = tuple(
        item for item in assets if item.storage_backend == "filesystem"
    )
    if any(
        item.storage_backend not in {"filesystem", "postgres"}
        for item in assets
    ):
        raise DeliveryTransitionServabilityError(
            "delivery physical backend is unsupported"
        )

    verified_blobs: set[tuple[str, str, int]] = set()
    for asset in filesystem_assets:
        identity = _filesystem_identity(asset)
        if identity in verified_blobs:
            continue
        if asset.asset_kind == "tile_archive":
            _require_blob_size(store, asset)
        else:
            _verify_blob_bytes(store, asset)
        verified_blobs.add(identity)

    if version.delivery_kind == "vector":
        if (
            primary.asset_kind != "vector_table"
            or primary.storage_backend != "postgres"
            or primary.size_bytes is not None
        ):
            raise DeliveryTransitionServabilityError(
                "vector primary physical identity is invalid"
            )
        try:
            geo_ingest.verify_vector_delivery_table(
                db,
                storage_key=primary.storage_key,
                content_sha256=primary.sha256,
                validation_json=version.validation_json,
                expected_feature_count=version.feature_count,
            )
        except Exception as error:
            raise DeliveryTransitionServabilityError(
                "PostGIS delivery table failed physical verification"
            ) from error
        renderer = "geoserver"
        render_transport = "direct_geoserver_wms"
        resource_name, style_names = _smoke_geoserver(
            primary,
            geoserver=geoserver,
        )
        tile_asset_ids: tuple[int, ...] = ()
    elif version.delivery_kind == "raster":
        if (
            primary.asset_kind != "raster_cog"
            or primary.storage_backend != "filesystem"
        ):
            raise DeliveryTransitionServabilityError(
                "raster primary physical identity is invalid"
            )
        renderer = "geoserver"
        render_transport = "direct_geoserver_wms"
        resource_name, style_names = _smoke_geoserver(
            primary,
            geoserver=geoserver,
        )
        tile_asset_ids = ()
    elif version.delivery_kind == "tiles":
        if (
            primary.asset_kind != "tile_archive"
            or primary.storage_backend != "filesystem"
        ):
            raise DeliveryTransitionServabilityError(
                "tile primary physical identity is invalid"
            )
        tile_assets = tuple(
            item for item in assets if item.asset_kind == "tile_archive"
        )
        if not tile_assets or any(
            item.storage_backend != "filesystem" for item in tile_assets
        ):
            raise DeliveryTransitionServabilityError(
                "tile delivery physical coverage is invalid"
            )
        renderer = "tile_archive"
        render_transport = "local_tile_archive"
        resource_name = None
        style_names = ()
        renderer_instance = LocalTileArchiveRenderer(store.root)
        for asset in tile_assets:
            _inspect_and_smoke_tile_archive(
                store,
                renderer_instance,
                asset,
            )
        tile_asset_ids = tuple(item.id for item in tile_assets)
    else:
        raise DeliveryTransitionServabilityError(
            "delivery kind has no physical transition verifier"
        )

    return DeliveryPhysicalTransitionVerification(
        version_id=version.id,
        primary_asset_id=primary.id,
        primary_sha256=primary.sha256,
        renderer=renderer,
        render_transport=render_transport,
        resource_name=resource_name,
        verified_filesystem_asset_ids=tuple(
            item.id for item in filesystem_assets
        ),
        rendered_style_names=style_names,
        rendered_tile_asset_ids=tile_asset_ids,
    )


def _filesystem_identity(
    asset: ReferenceDeliveryAsset,
) -> tuple[str, str, int]:
    match = (
        _BLOB_KEY_RE.fullmatch(asset.storage_key)
        if isinstance(asset.storage_key, str)
        else None
    )
    if (
        match is None
        or not isinstance(asset.sha256, str)
        or _SHA256_RE.fullmatch(asset.sha256) is None
        or match.group("sha") != asset.sha256
        or match.group("prefix") != asset.sha256[:2]
        or isinstance(asset.size_bytes, bool)
        or not isinstance(asset.size_bytes, int)
        or asset.size_bytes < 1
    ):
        raise DeliveryTransitionServabilityError(
            "delivery filesystem asset identity is invalid"
        )
    return asset.storage_key, asset.sha256, asset.size_bytes


def _require_blob_size(
    store: ReferenceBlobStore,
    asset: ReferenceDeliveryAsset,
) -> None:
    _filesystem_identity(asset)
    try:
        path = store.resolve_blob(asset.storage_key)
        metadata = path.stat()
    except Exception as error:
        raise DeliveryTransitionServabilityError(
            "delivery filesystem blob is absent"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size != asset.size_bytes
    ):
        raise DeliveryTransitionServabilityError(
            "delivery filesystem blob size is invalid"
        )


def _verify_blob_bytes(
    store: ReferenceBlobStore,
    asset: ReferenceDeliveryAsset,
) -> None:
    _filesystem_identity(asset)
    digest = hashlib.sha256()
    try:
        with store.open_blob(asset.storage_key) as source:
            descriptor = source.fileno()
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != asset.size_bytes
            ):
                raise DeliveryTransitionServabilityError(
                    "delivery filesystem blob size is invalid"
                )
            while True:
                chunk = source.read(_FILE_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
    except DeliveryTransitionServabilityError:
        raise
    except Exception as error:
        raise DeliveryTransitionServabilityError(
            "delivery filesystem blob could not be verified"
        ) from error
    if (
        _file_fingerprint(before) != _file_fingerprint(after)
        or digest.hexdigest() != asset.sha256
    ):
        raise DeliveryTransitionServabilityError(
            "delivery filesystem blob digest is invalid"
        )


def _file_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _smoke_geoserver(
    primary: ReferenceDeliveryAsset,
    *,
    geoserver: GeoServerAdminClient | None,
) -> tuple[str, tuple[str | None, ...]]:
    metadata = _asset_metadata(primary)
    layer_name = metadata.get("layer_name")
    styles = metadata.get("styles")
    default_style = metadata.get("default_style_name")
    identify_available = metadata.get("identify_available")
    legend_available = metadata.get("legend_available")
    if (
        metadata.get("renderer") != "geoserver"
        or not isinstance(layer_name, str)
        or _RESOURCE_RE.fullmatch(layer_name) is None
        or not isinstance(styles, dict)
        or len(styles) > 256
        or not isinstance(identify_available, bool)
        or not isinstance(legend_available, bool)
    ):
        raise DeliveryTransitionServabilityError(
            "GeoServer delivery metadata is invalid"
        )
    style_values: set[str] = set()
    for style_id, style_name in styles.items():
        if (
            not isinstance(style_id, str)
            or not style_id.isascii()
            or not style_id.isdecimal()
            or int(style_id) < 1
            or not isinstance(style_name, str)
            or _RESOURCE_RE.fullmatch(style_name) is None
        ):
            raise DeliveryTransitionServabilityError(
                "GeoServer delivery style identity is invalid"
            )
        style_values.add(style_name)
    if style_values:
        if default_style not in style_values:
            raise DeliveryTransitionServabilityError(
                "GeoServer default style identity is invalid"
            )
        style_names: tuple[str | None, ...] = tuple(sorted(style_values))
    else:
        if default_style is not None:
            raise DeliveryTransitionServabilityError(
                "GeoServer default style identity is invalid"
            )
        style_names = (None,)
    client = geoserver or GeoServerAdminClient()
    try:
        client.health()
        results = tuple(
            client.smoke_layer(
                layer_name=layer_name,
                style_name=style_name,
                legend_available=legend_available,
                identify_available=identify_available,
                z=0,
                x=0,
                y=0,
                pixel_x=128,
                pixel_y=128,
            )
            for style_name in style_names
        )
    except Exception as error:
        raise DeliveryTransitionServabilityError(
            "GeoServer resource or direct render smoke is unavailable"
        ) from error
    if any(
        not _valid_geoserver_smoke(
            result,
            layer_name=layer_name,
            style_name=style_name,
            legend_available=legend_available,
            identify_available=identify_available,
        )
        for result, style_name in zip(results, style_names, strict=True)
    ):
        raise DeliveryTransitionServabilityError(
            "GeoServer direct render smoke evidence is invalid"
        )
    return layer_name, style_names


def _valid_geoserver_smoke(
    result: Any,
    *,
    layer_name: str,
    style_name: str | None,
    legend_available: bool,
    identify_available: bool,
) -> bool:
    return bool(
        isinstance(result, LayerSmokeResult)
        and result.layer_name == layer_name
        and result.style_name == style_name
        and not isinstance(result.image_bytes, bool)
        and isinstance(result.image_bytes, int)
        and result.image_bytes > 0
        and isinstance(result.image_sha256, str)
        and _SHA256_RE.fullmatch(result.image_sha256)
        and isinstance(result.image_content_type, str)
        and result.image_content_type.startswith("image/")
        and (
            not legend_available
            or (
                isinstance(result.legend_bytes, int)
                and not isinstance(result.legend_bytes, bool)
                and result.legend_bytes > 0
                and isinstance(result.legend_sha256, str)
                and _SHA256_RE.fullmatch(result.legend_sha256)
                and isinstance(result.legend_content_type, str)
                and result.legend_content_type.startswith("image/")
            )
        )
        and (
            not identify_available
            or (
                isinstance(result.identify_bytes, int)
                and not isinstance(result.identify_bytes, bool)
                and result.identify_bytes > 0
                and isinstance(result.identify_sha256, str)
                and _SHA256_RE.fullmatch(result.identify_sha256)
                and result.identify_content_type
                in {"application/json", "application/geo+json"}
                and isinstance(result.identify_feature_count, int)
                and not isinstance(result.identify_feature_count, bool)
                and result.identify_feature_count >= 0
            )
        )
    )


def _inspect_and_smoke_tile_archive(
    store: ReferenceBlobStore,
    renderer: LocalTileArchiveRenderer,
    asset: ReferenceDeliveryAsset,
) -> None:
    metadata = _asset_metadata(asset)
    expected_tile_count = metadata.get("expected_tile_count")
    expected_coordinate_sha256 = metadata.get(
        "expected_coordinate_sha256"
    )
    smoke_coordinate = metadata.get("smoke_coordinate")
    if (
        metadata.get("renderer") != "tile_archive"
        or isinstance(expected_tile_count, bool)
        or not isinstance(expected_tile_count, int)
        or expected_tile_count < 1
        or not isinstance(expected_coordinate_sha256, str)
        or _SHA256_RE.fullmatch(expected_coordinate_sha256) is None
        or not isinstance(smoke_coordinate, list)
        or len(smoke_coordinate) != 3
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in smoke_coordinate
        )
    ):
        raise DeliveryTransitionServabilityError(
            "tile archive delivery metadata is invalid"
        )
    try:
        inspection = geo_ingest.inspect_tile_archive(
            store,
            storage_key=asset.storage_key,
            archive_sha256=asset.sha256,
            expected_tile_count=expected_tile_count,
            expected_coordinate_sha256=expected_coordinate_sha256,
        )
        if (
            inspection.min_zoom != metadata.get("min_zoom")
            or inspection.max_zoom != metadata.get("max_zoom")
            or inspection.image_format != metadata.get("image_format")
        ):
            raise DeliveryTransitionServabilityError(
                "tile archive inspection differs from delivery metadata"
            )
        response = renderer.render_tile(
            storage_key=asset.storage_key,
            archive_sha256=asset.sha256,
            z=smoke_coordinate[0],
            x=smoke_coordinate[1],
            y=smoke_coordinate[2],
        )
    except DeliveryTransitionServabilityError:
        raise
    except Exception as error:
        raise DeliveryTransitionServabilityError(
            "tile archive failed full inspection or local render smoke"
        ) from error
    if (
        not response.body
        or not response.content_type.startswith("image/")
    ):
        raise DeliveryTransitionServabilityError(
            "tile archive local render smoke evidence is invalid"
        )


def _asset_metadata(asset: ReferenceDeliveryAsset) -> dict[str, Any]:
    if not isinstance(asset.metadata_json, dict) or len(asset.metadata_json) > 64:
        raise DeliveryTransitionServabilityError(
            "delivery physical metadata is invalid"
        )
    return asset.metadata_json
