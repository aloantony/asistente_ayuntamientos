from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.config import settings as app_settings
from app.db.session import get_db
from app.reference_layers.access import (
    require_catalog_manage,
    require_catalog_view,
)
from app.reference_layers.models import (
    OrganizationReferenceLayerSetting,
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerStyle,
    ReferenceService,
)
from app.reference_layers.local_delivery import (
    catalog_local_delivery_availability,
)
from app.reference_layers.mirror_status import catalog_mirror_statuses
from app.reference_layers.schemas import (
    ReferenceCatalogRead,
    ReferenceCatalogSnapshotRead,
    ReferenceLayerRead,
    ReferenceLayerSettingRead,
    ReferenceLayerSettingUpdate,
    ReferenceLayerStyleRead,
    ReferenceServiceRead,
)
from app.reference_layers.wms_delivery import catalog_delivery_availability
from app.reference_layers.wms_routes import router as wms_router
from app.users.models import User

router = APIRouter(tags=["reference-layers"])
router.include_router(wms_router)


@router.get(
    "/reference-layers/catalog",
    response_model=ReferenceCatalogRead,
)
def get_reference_catalog(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    provider_key: Annotated[
        str,
        Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.:/-]*$"),
    ],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> ReferenceCatalogRead:
    require_catalog_view(db, current_user, organization_id)
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot)
        .where(
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
            ReferenceCatalogSnapshot.provider_key == provider_key,
        )
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reference layer catalog is not available",
        )

    services = list(
        db.scalars(
            select(ReferenceService)
            .where(
                ReferenceService.provider_key == snapshot.provider_key,
                ReferenceService.last_seen_snapshot_id == snapshot.id,
            )
            .order_by(ReferenceService.title, ReferenceService.id)
        )
    )
    layers = list(
        db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == snapshot.provider_key,
                ReferenceLayer.last_seen_snapshot_id == snapshot.id,
            )
            .order_by(
                ReferenceLayer.parent_id.asc().nulls_first(),
                ReferenceLayer.sort_order,
                ReferenceLayer.id,
            )
        )
    )
    styles = list(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(
                ReferenceLayerStyle.provider_key == snapshot.provider_key,
                ReferenceLayerStyle.last_seen_snapshot_id == snapshot.id,
            )
            .order_by(
                ReferenceLayerStyle.layer_id,
                ReferenceLayerStyle.sort_order,
                ReferenceLayerStyle.id,
            )
        )
    )
    settings: dict[int, OrganizationReferenceLayerSetting] = {}
    if organization_id is not None:
        settings = {
            item.layer_id: item
            for item in db.scalars(
                select(OrganizationReferenceLayerSetting).where(
                    OrganizationReferenceLayerSetting.organization_id
                    == organization_id
                )
            )
        }

    proxy_delivery_availability = catalog_delivery_availability(
        db,
        snapshot=snapshot,
        services=services,
        layers=layers,
        styles=styles,
        enabled=app_settings.reference_remote_proxy_enabled,
    )
    local_delivery_availability = catalog_local_delivery_availability(
        db,
        provider_key=snapshot.provider_key,
        layers=layers,
        styles=styles,
    )
    delivery_availability = {
        layer.id: (
            local_delivery_availability[layer.id]
            if local_delivery_availability[layer.id] is not None
            else proxy_delivery_availability[layer.id]
        )
        for layer in layers
    }
    mirror_statuses = catalog_mirror_statuses(
        db,
        provider_key=snapshot.provider_key,
        layers=layers,
        styles=styles,
        local_availability=local_delivery_availability,
    )

    layer_reads: list[ReferenceLayerRead] = []
    for layer in layers:
        setting = settings.get(layer.id)
        effective_visible = layer.default_visible
        effective_opacity = Decimal(layer.default_opacity)
        if setting is not None:
            if setting.visible is not None:
                effective_visible = setting.visible
            if setting.opacity is not None:
                effective_opacity = Decimal(setting.opacity)
        availability = delivery_availability[layer.id]
        mirror_status = mirror_statuses[layer.id]
        layer_reads.append(
            ReferenceLayerRead.model_validate(layer).model_copy(
                update={
                    "effective_visible": effective_visible,
                    "effective_opacity": float(effective_opacity),
                    "delivery_available": (
                        availability.delivery_available
                    ),
                    "identify_available": (
                        availability.identify_available
                    ),
                    "delivery_blocker": availability.delivery_blocker,
                    "available_style_ids": list(
                        availability.available_style_ids
                    ),
                    "legend_available": (
                        availability.legend_available
                    ),
                    "metadata_available": layer.metadata_url is not None,
                    "mirror_status": mirror_status.status,
                    "active_version_id": mirror_status.active_version_id,
                    "active_generation": mirror_status.active_generation,
                    "active_source_version": (
                        mirror_status.active_source_version
                    ),
                    "active_reference_at": mirror_status.active_reference_at,
                    "active_created_at": mirror_status.active_created_at,
                    "last_run_status": mirror_status.last_run_status,
                    "last_checked_at": mirror_status.last_checked_at,
                    "last_sync_error_code": mirror_status.last_error_code,
                    "last_sync_error_summary": mirror_status.last_error_summary,
                    "next_check_at": mirror_status.next_check_at,
                }
            )
        )

    return ReferenceCatalogRead(
        snapshot=ReferenceCatalogSnapshotRead.model_validate(snapshot),
        organization_id=organization_id,
        services=[ReferenceServiceRead.model_validate(item) for item in services],
        layers=layer_reads,
        styles=[
            ReferenceLayerStyleRead.model_validate(item).model_copy(
                update={
                    "legend_available": (
                        item.id
                        in delivery_availability[
                            item.layer_id
                        ].available_legend_style_ids
                    )
                }
            )
            for item in styles
        ],
    )


@router.put(
    "/organizations/{organization_id}/reference-layers/{layer_id}/settings",
    response_model=ReferenceLayerSettingRead,
)
def update_reference_layer_setting(
    organization_id: int,
    layer_id: int,
    payload: ReferenceLayerSettingUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OrganizationReferenceLayerSetting:
    require_catalog_manage(db, current_user, organization_id)
    layer = db.get(ReferenceLayer, layer_id)
    if layer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reference layer not found",
        )
    if layer.node_type != "layer":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Reference layer settings require a layer node",
        )

    setting = db.scalar(
        select(OrganizationReferenceLayerSetting)
        .where(
            OrganizationReferenceLayerSetting.organization_id == organization_id,
            OrganizationReferenceLayerSetting.layer_id == layer_id,
        )
        .with_for_update()
    )
    if setting is None:
        setting = OrganizationReferenceLayerSetting(
            organization_id=organization_id,
            layer_id=layer_id,
        )
        db.add(setting)
    setting.visible = payload.visible
    setting.opacity = payload.opacity
    setting.updated_by_id = current_user.id

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reference layer setting conflicts with existing data",
        ) from None
    db.refresh(setting)
    return setting


@router.delete(
    "/organizations/{organization_id}/reference-layers/{layer_id}/settings",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_reference_layer_setting(
    organization_id: int,
    layer_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    require_catalog_manage(db, current_user, organization_id)
    setting = db.scalar(
        select(OrganizationReferenceLayerSetting).where(
            OrganizationReferenceLayerSetting.organization_id == organization_id,
            OrganizationReferenceLayerSetting.layer_id == layer_id,
        )
    )
    if setting is not None:
        db.delete(setting)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
