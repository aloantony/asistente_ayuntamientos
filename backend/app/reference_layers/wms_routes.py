from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from math import isfinite
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.db.session import get_db
from app.reference_layers.access import require_catalog_view
from app.reference_layers.local_delivery import (
    LocalDeliveryError,
    LocalDeliverySelection,
    resolve_local_delivery,
)
from app.reference_layers.local_geoserver import (
    LocalGeoServerError,
    LocalGeoServerRenderer,
    LocalGeoServerResponse,
)
from app.reference_layers.local_tile_archive import (
    LocalTileArchiveError,
    LocalTileArchiveRenderer,
    LocalTileArchiveResponse,
    LocalTileNotFoundError,
)
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerStyle,
    ReferenceService,
)
from app.reference_layers.wms_cache import (
    CachedWMSResponse,
    build_wms_cache_key,
    get_cached_wms_response,
    store_cached_wms_response,
)
from app.reference_layers.wms_delivery import (
    AttestedWMSDelivery,
    WMSDeliveryCapabilityError,
    WMSDeliveryEvidenceUnavailableError,
    WMSDeliveryIdentifyError,
    WMSDeliveryLicenseDeniedError,
    WMSDeliveryWebMercatorError,
    resolve_attested_wms_delivery,
)
from app.reference_layers.wms_proxy import (
    UnsafeWMSEndpointError,
    WMSRequest,
    WMSResponse,
    WMSUpstreamUnavailableError,
    build_identify_request,
    build_legend_request,
    build_tile_request,
    fetch_wms_response,
    tile_lonlat_bounds,
)
from app.reference_layers.wms_schemas import (
    InvalidFeatureInfoError,
    parse_feature_collection,
)
from app.users.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference-layers"])

SIUR_PROVIDER_KEY = "siur"
TILE_FRESH_SECONDS = 15 * 60
TILE_STALE_SECONDS = 24 * 60 * 60
LEGEND_FRESH_SECONDS = 24 * 60 * 60
LEGEND_STALE_SECONDS = 7 * 24 * 60 * 60

_tile_rate_limiter = SlidingWindowRateLimiter(240, 60)
_legend_rate_limiter = SlidingWindowRateLimiter(60, 60)
_identify_rate_limiter = SlidingWindowRateLimiter(60, 60)


@dataclass(frozen=True)
class ReferenceWMSContext:
    layer: ReferenceLayer
    service: ReferenceService
    style: ReferenceLayerStyle | None
    delivery: AttestedWMSDelivery


@dataclass(frozen=True)
class ReferenceLocalContext:
    layer: ReferenceLayer
    style: ReferenceLayerStyle | None
    delivery: LocalDeliverySelection


def _require_delivery_fence(
    context: ReferenceWMSContext | ReferenceLocalContext,
    *,
    version_id: int | None,
    generation: int | None,
) -> None:
    if (version_id is None) != (generation is None):
        raise HTTPException(
            status_code=422,
            detail="Version and generation must be provided together",
        )
    if version_id is None or generation is None:
        return
    if (
        not isinstance(context, ReferenceLocalContext)
        or context.delivery.version_id != version_id
        or context.delivery.generation != generation
    ):
        raise HTTPException(
            status_code=409,
            detail="Reference delivery version changed",
        )


@router.get(
    "/organizations/{organization_id}/reference-layers/{layer_id}"
    "/tiles/{z}/{x}/{y}.png",
    response_class=Response,
)
def get_reference_layer_tile(
    request: Request,
    organization_id: Annotated[int, Path(ge=1)],
    layer_id: Annotated[int, Path(ge=1)],
    z: Annotated[int, Path(ge=0, le=24)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    style_id: Annotated[int | None, Query(ge=1)] = None,
    version_id: Annotated[int | None, Query(ge=1)] = None,
    generation: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    context = _resolve_delivery_context(
        db,
        current_user=current_user,
        organization_id=organization_id,
        layer_id=layer_id,
        style_id=style_id,
        operation="tile",
    )
    _require_delivery_fence(
        context,
        version_id=version_id,
        generation=generation,
    )
    _require_rate_limit(_tile_rate_limiter, "tile", current_user.id)
    _require_tile_scope(context.layer, z=z, x=x, y=y)
    if isinstance(context, ReferenceLocalContext):
        return _render_local_tile(
            request=request,
            context=context,
            z=z,
            x=x,
            y=y,
        )
    try:
        wms_request = build_tile_request(
            endpoint_url=context.delivery.endpoint_url,
            version=context.delivery.version,
            remote_name=context.delivery.remote_name,
            style_name=context.delivery.style_name,
            z=z,
            x=x,
            y=y,
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tile request") from None
    return _cached_binary_response(
        request=request,
        context=context,
        wms_request=wms_request,
        cache_coordinates={"x": x, "y": y, "z": z},
        fresh_seconds=TILE_FRESH_SECONDS,
        stale_seconds=TILE_STALE_SECONDS,
    )


@router.get(
    "/organizations/{organization_id}/reference-layers/{layer_id}/legend.png",
    response_class=Response,
)
def get_reference_layer_legend(
    request: Request,
    organization_id: Annotated[int, Path(ge=1)],
    layer_id: Annotated[int, Path(ge=1)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    style_id: Annotated[int | None, Query(ge=1)] = None,
    version_id: Annotated[int | None, Query(ge=1)] = None,
    generation: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    context = _resolve_delivery_context(
        db,
        current_user=current_user,
        organization_id=organization_id,
        layer_id=layer_id,
        style_id=style_id,
        operation="legend",
    )
    _require_delivery_fence(
        context,
        version_id=version_id,
        generation=generation,
    )
    _require_rate_limit(_legend_rate_limiter, "legend", current_user.id)
    if isinstance(context, ReferenceLocalContext):
        return _render_local_legend(request=request, context=context)
    try:
        wms_request = build_legend_request(
            endpoint_url=context.delivery.endpoint_url,
            version=context.delivery.version,
            remote_name=context.delivery.remote_name,
            style_name=context.delivery.style_name,
        )
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="Layer cannot be rendered",
        ) from None
    return _cached_binary_response(
        request=request,
        context=context,
        wms_request=wms_request,
        cache_coordinates={},
        fresh_seconds=LEGEND_FRESH_SECONDS,
        stale_seconds=LEGEND_STALE_SECONDS,
    )


@router.get(
    "/organizations/{organization_id}/reference-layers/{layer_id}/identify",
    response_class=JSONResponse,
)
def identify_reference_layer(
    organization_id: Annotated[int, Path(ge=1)],
    layer_id: Annotated[int, Path(ge=1)],
    z: Annotated[int, Query(ge=0, le=24)],
    x: Annotated[int, Query(ge=0)],
    y: Annotated[int, Query(ge=0)],
    pixel_x: Annotated[int, Query(ge=0, le=255)],
    pixel_y: Annotated[int, Query(ge=0, le=255)],
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    style_id: Annotated[int | None, Query(ge=1)] = None,
    version_id: Annotated[int | None, Query(ge=1)] = None,
    generation: Annotated[int | None, Query(ge=1)] = None,
    feature_count: Annotated[int, Query(ge=1, le=10)] = 5,
) -> JSONResponse:
    context = _resolve_delivery_context(
        db,
        current_user=current_user,
        organization_id=organization_id,
        layer_id=layer_id,
        style_id=style_id,
        require_queryable=True,
        operation="identify",
    )
    _require_delivery_fence(
        context,
        version_id=version_id,
        generation=generation,
    )
    _require_rate_limit(_identify_rate_limiter, "identify", current_user.id)
    _require_tile_scope(context.layer, z=z, x=x, y=y)
    if isinstance(context, ReferenceLocalContext):
        return _render_local_identify(
            context=context,
            z=z,
            x=x,
            y=y,
            pixel_x=pixel_x,
            pixel_y=pixel_y,
            feature_count=feature_count,
        )
    try:
        wms_request = build_identify_request(
            endpoint_url=context.delivery.endpoint_url,
            version=context.delivery.version,
            remote_name=context.delivery.remote_name,
            style_name=context.delivery.style_name,
            z=z,
            x=x,
            y=y,
            pixel_x=pixel_x,
            pixel_y=pixel_y,
            feature_count=feature_count,
        )
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid identify request",
        ) from None
    try:
        upstream = fetch_wms_response(wms_request)
        payload = parse_feature_collection(
            upstream.body,
            max_features=feature_count,
        )
    except (
        InvalidFeatureInfoError,
        UnsafeWMSEndpointError,
        WMSUpstreamUnavailableError,
    ):
        logger.warning("Reference WMS identify failed", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Reference map service is unavailable",
        ) from None
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization, Cookie",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _resolve_delivery_context(
    db: Session,
    *,
    current_user: User,
    organization_id: int,
    layer_id: int,
    style_id: int | None,
    operation: Literal["tile", "legend", "identify"],
    require_queryable: bool = False,
) -> ReferenceWMSContext | ReferenceLocalContext:
    require_catalog_view(db, current_user, organization_id)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.id == layer_id,
            ReferenceLayer.provider_key == SIUR_PROVIDER_KEY,
        )
    )
    if layer is None or layer.status not in {"active", "degraded"}:
        raise HTTPException(status_code=404, detail="Reference layer not found")
    if (
        layer.node_type != "layer"
        or layer.renderer != "raster_tile"
        or layer.delivery_mode not in {"mirror", "proxy"}
    ):
        raise HTTPException(status_code=409, detail="Layer cannot be rendered")
    if require_queryable and not layer.queryable:
        raise HTTPException(status_code=409, detail="Layer is not queryable")
    style = _resolve_style(db, layer=layer, style_id=style_id)
    try:
        local_delivery = resolve_local_delivery(
            db,
            layer=layer,
            style=style,
            operation=operation,
        )
    except LocalDeliveryError as exc:
        if exc.blocker in {
            "style_unsupported",
            "local_identify_unavailable",
            "local_legend_unavailable",
            "local_disabled",
        }:
            raise HTTPException(
                status_code=409,
                detail="Layer cannot perform this operation",
            ) from None
        raise HTTPException(
            status_code=503,
            detail="Local reference layer is unavailable",
        ) from None
    if local_delivery is not None:
        return ReferenceLocalContext(
            layer=layer,
            style=style,
            delivery=local_delivery,
        )

    if not settings.reference_remote_proxy_enabled:
        raise HTTPException(
            status_code=503,
            detail="Local reference layer is unavailable",
        )
    if not layer.remote_name:
        raise HTTPException(status_code=409, detail="Layer cannot be rendered")
    service = layer.service
    if (
        service is None
        or service.provider_key != SIUR_PROVIDER_KEY
        or service.upstream_protocol != "wms"
        or service.status not in {"active", "degraded"}
    ):
        raise HTTPException(status_code=409, detail="Layer cannot be rendered")
    try:
        delivery = resolve_attested_wms_delivery(
            db,
            layer=layer,
            service=service,
            style=style,
            operation=operation,
        )
    except WMSDeliveryLicenseDeniedError:
        raise HTTPException(
            status_code=451,
            detail="Reference layer license is not approved",
        ) from None
    except WMSDeliveryWebMercatorError:
        raise HTTPException(
            status_code=409,
            detail="Layer does not support web map tiles",
        ) from None
    except WMSDeliveryIdentifyError:
        raise HTTPException(
            status_code=409,
            detail="Layer is not queryable",
        ) from None
    except WMSDeliveryCapabilityError:
        raise HTTPException(
            status_code=409,
            detail="Layer cannot be rendered",
        ) from None
    except WMSDeliveryEvidenceUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Reference layer delivery is not attested",
        ) from None
    return ReferenceWMSContext(
        layer=layer,
        service=service,
        style=style,
        delivery=delivery,
    )


def _resolve_style(
    db: Session,
    *,
    layer: ReferenceLayer,
    style_id: int | None,
) -> ReferenceLayerStyle | None:
    filters = [
        ReferenceLayerStyle.provider_key == layer.provider_key,
        ReferenceLayerStyle.layer_id == layer.id,
        ReferenceLayerStyle.status.in_(("active", "degraded")),
    ]
    if style_id is not None:
        style = db.scalar(
            select(ReferenceLayerStyle).where(
                *filters,
                ReferenceLayerStyle.id == style_id,
            )
        )
        if style is None:
            raise HTTPException(status_code=404, detail="Reference style not found")
        return style
    style = db.scalar(
        select(ReferenceLayerStyle).where(
            *filters,
            ReferenceLayerStyle.is_default.is_(True),
        )
    )
    if style is not None:
        return style
    any_style = db.scalar(select(ReferenceLayerStyle.id).where(*filters))
    if any_style is not None:
        raise HTTPException(status_code=409, detail="Layer has no default style")
    return None


def _render_local_tile(
    *,
    request: Request,
    context: ReferenceLocalContext,
    z: int,
    x: int,
    y: int,
) -> Response:
    try:
        if context.delivery.backend == "geoserver":
            if context.delivery.layer_name is None:
                raise LocalGeoServerError("missing local renderer layer")
            rendered: LocalGeoServerResponse | LocalTileArchiveResponse = (
                LocalGeoServerRenderer().render_tile(
                    layer_name=context.delivery.layer_name,
                    style_name=context.delivery.style_name,
                    z=z,
                    x=x,
                    y=y,
                )
            )
            cache_status = "GWC"
        else:
            rendered = LocalTileArchiveRenderer(
                settings.reference_storage_root
            ).render_tile(
                storage_key=context.delivery.storage_key,
                archive_sha256=context.delivery.asset_sha256,
                z=z,
                x=x,
                y=y,
            )
            cache_status = "ARCHIVE"
    except LocalTileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Layer is not available for tile",
        ) from None
    except (LocalGeoServerError, LocalTileArchiveError, ValueError):
        logger.error("Local reference tile rendering failed", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Local reference map service is unavailable",
        ) from None
    return _local_binary_response(
        request=request,
        rendered=rendered,
        cache_status=cache_status,
        version_id=context.delivery.version_id,
        generation=context.delivery.generation,
    )


def _render_local_legend(
    *,
    request: Request,
    context: ReferenceLocalContext,
) -> Response:
    if (
        context.delivery.backend != "geoserver"
        or context.delivery.layer_name is None
    ):
        raise HTTPException(status_code=409, detail="Layer has no local legend")
    try:
        rendered = LocalGeoServerRenderer().render_legend(
            layer_name=context.delivery.layer_name,
            style_name=context.delivery.style_name,
        )
    except (LocalGeoServerError, ValueError):
        logger.error("Local reference legend rendering failed", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Local reference map service is unavailable",
        ) from None
    return _local_binary_response(
        request=request,
        rendered=rendered,
        cache_status="GWC",
        version_id=context.delivery.version_id,
        generation=context.delivery.generation,
    )


def _render_local_identify(
    *,
    context: ReferenceLocalContext,
    z: int,
    x: int,
    y: int,
    pixel_x: int,
    pixel_y: int,
    feature_count: int,
) -> JSONResponse:
    if (
        context.delivery.backend != "geoserver"
        or context.delivery.layer_name is None
    ):
        raise HTTPException(status_code=409, detail="Layer is not queryable")
    try:
        rendered = LocalGeoServerRenderer().get_feature_info(
            layer_name=context.delivery.layer_name,
            style_name=context.delivery.style_name,
            z=z,
            x=x,
            y=y,
            pixel_x=pixel_x,
            pixel_y=pixel_y,
            feature_count=feature_count,
        )
        payload = parse_feature_collection(
            rendered.body,
            max_features=feature_count,
        )
    except (LocalGeoServerError, InvalidFeatureInfoError, ValueError):
        logger.error("Local reference identify failed", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Local reference map service is unavailable",
        ) from None
    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "private, no-store",
            "Vary": "Authorization, Cookie",
            "X-Content-Type-Options": "nosniff",
            "X-Reference-Version": str(context.delivery.version_id),
            "X-Reference-Generation": str(context.delivery.generation),
        },
    )


def _local_binary_response(
    *,
    request: Request,
    rendered: LocalGeoServerResponse | LocalTileArchiveResponse,
    cache_status: str,
    version_id: int,
    generation: int,
) -> Response:
    headers = {
        "Cache-Control": "private, max-age=300",
        "ETag": rendered.etag,
        "Vary": "Authorization, Cookie",
        "X-Content-Type-Options": "nosniff",
        "X-Reference-Cache": cache_status,
        "X-Reference-Version": str(version_id),
        "X-Reference-Generation": str(generation),
    }
    if _etag_matches(request.headers.get("if-none-match"), rendered.etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=rendered.body,
        media_type=rendered.content_type,
        headers=headers,
    )


def _cached_binary_response(
    *,
    request: Request,
    context: ReferenceWMSContext,
    wms_request: WMSRequest,
    cache_coordinates: dict[str, int],
    fresh_seconds: int,
    stale_seconds: int,
) -> Response:
    cache_enabled = (
        context.delivery.allow_cache
        and context.service.cache_policy in {"mirror", "on_demand"}
    )
    cache_key = build_wms_cache_key(
        {
            "coordinates": cache_coordinates,
            "attestation_sha256": context.delivery.attestation_sha256,
            "layer_id": context.layer.id,
            "operation": wms_request.operation,
            "provider_key": context.layer.provider_key,
            "style_id": context.style.id if context.style is not None else None,
        }
    )
    cached = get_cached_wms_response(cache_key) if cache_enabled else None
    if cached is not None and cached.is_fresh():
        return _binary_response(
            request=request,
            cached=cached,
            cache_status="HIT",
        )
    try:
        upstream = fetch_wms_response(wms_request)
    except (UnsafeWMSEndpointError, WMSUpstreamUnavailableError):
        logger.warning("Reference WMS binary request failed", exc_info=True)
        if cached is not None:
            return _binary_response(
                request=request,
                cached=cached,
                cache_status="STALE",
            )
        raise HTTPException(
            status_code=502,
            detail="Reference map service is unavailable",
        ) from None
    stored = _to_cached_response(upstream, fresh_seconds=fresh_seconds)
    if cache_enabled:
        store_cached_wms_response(
            cache_key,
            stored,
            stale_ttl_seconds=stale_seconds,
        )
    return _binary_response(
        request=request,
        cached=stored,
        cache_status="MISS" if cache_enabled else "BYPASS",
    )


def _to_cached_response(
    response: WMSResponse,
    *,
    fresh_seconds: int,
) -> CachedWMSResponse:
    return CachedWMSResponse(
        body=response.body,
        content_type=response.content_type,
        etag=response.etag,
        stored_at=int(time.time()),
        fresh_for_seconds=fresh_seconds,
    )


def _binary_response(
    *,
    request: Request,
    cached: CachedWMSResponse,
    cache_status: str,
) -> Response:
    headers = {
        "Cache-Control": f"private, max-age={cached.fresh_for_seconds}",
        "ETag": cached.etag,
        "Vary": "Authorization, Cookie",
        "X-Content-Type-Options": "nosniff",
        "X-Reference-Cache": cache_status,
    }
    if _etag_matches(request.headers.get("if-none-match"), cached.etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=cached.body,
        media_type=cached.content_type,
        headers=headers,
    )


def _etag_matches(value: str | None, etag: str) -> bool:
    if not value:
        return False
    return any(candidate.strip() in {"*", etag} for candidate in value.split(","))


def _require_rate_limit(
    limiter: SlidingWindowRateLimiter,
    operation: str,
    user_id: int,
) -> None:
    if limiter.try_acquire(f"{operation}:{user_id}"):
        return
    raise HTTPException(
        status_code=429,
        detail="Reference map request limit exceeded",
        headers={"Retry-After": "60"},
    )


def _require_tile_scope(
    layer: ReferenceLayer,
    *,
    z: int,
    x: int,
    y: int,
) -> None:
    if layer.min_zoom is not None and z < layer.min_zoom:
        raise HTTPException(status_code=404, detail="Layer is not available at zoom")
    if layer.max_zoom is not None and z > layer.max_zoom:
        raise HTTPException(status_code=404, detail="Layer is not available at zoom")
    try:
        tile_west, tile_south, tile_east, tile_north = tile_lonlat_bounds(
            z,
            x,
            y,
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid tile request") from None
    try:
        bounds = _geographic_bounds(layer.bounds_json)
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="Layer cannot be rendered",
        ) from None
    if bounds is None:
        return
    west, south, east, north = bounds
    if (
        tile_east <= west
        or tile_west >= east
        or tile_north <= south
        or tile_south >= north
    ):
        raise HTTPException(status_code=404, detail="Layer is not available for tile")


def _geographic_bounds(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid geographic bounds")
    raw = tuple(value.get(key) for key in ("west", "south", "east", "north"))
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in raw
    ):
        raise ValueError("invalid geographic bounds")
    west, south, east, north = (float(item) for item in raw)
    if not (
        all(isfinite(item) for item in (west, south, east, north))
        and -180 <= west < east <= 180
        and -90 <= south < north <= 90
    ):
        raise ValueError("invalid geographic bounds")
    return west, south, east, north
