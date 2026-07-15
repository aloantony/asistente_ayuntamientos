from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.assets.models import MunicipalAsset
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.geo.access import (
    derive_municipality_id,
    get_visible_entity,
    has_any_map_view_permission,
    has_map_edit_permission,
    has_map_view_permission,
    require_visible_entity,
)
from app.geo.geometry import build_point_geojson
from app.geo.models import EntityLocation, GeoLocation
from app.geo.schemas import EntityLocationCreate, GeoEntityType, GeoMapItem
from app.users.models import User

router = APIRouter(prefix="/geo", tags=["geo"])

ASSET_LOCATION_SCOPE_CONFLICT = (
    "Location organization or municipality cannot change while assets reference it"
)
LOCATION_UPDATE_CONFLICT = "Location update conflicts with existing data"


@router.get("/map-items", response_model=list[GeoMapItem])
def list_map_items(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    entity_type: Annotated[GeoEntityType | None, Query()] = None,
    entity_id: Annotated[int | None, Query(ge=1)] = None,
    organization_id: int | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> list[GeoMapItem]:
    if not has_any_map_view_permission(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: map.view",
        )

    query = (
        select(EntityLocation)
        .join(EntityLocation.location)
        .options(selectinload(EntityLocation.location))
        .where(GeoLocation.review_status != "rejected")
        .order_by(EntityLocation.id.desc())
        .limit(limit)
    )
    if entity_type is not None:
        query = query.where(EntityLocation.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(EntityLocation.entity_id == entity_id)
    if organization_id is not None:
        query = query.where(GeoLocation.organization_id == organization_id)

    items: list[GeoMapItem] = []
    for attachment in db.scalars(query):
        visible = get_visible_entity(
            db,
            current_user,
            attachment.entity_type,
            attachment.entity_id,
        )
        if visible is None:
            continue
        if organization_id is not None and visible.organization_id != organization_id:
            continue
        if not has_map_view_permission(db, current_user, visible.organization_id):
            continue
        if status_filter is not None and visible.status != status_filter:
            continue
        if not include_archived and visible.status == "archived":
            continue
        items.append(
            GeoMapItem(
                entity_type=visible.entity_type,  # type: ignore[arg-type]
                entity_id=attachment.entity_id,
                title=visible.title,
                subtitle=visible.subtitle,
                status=visible.status,
                priority=visible.priority,
                organization_id=visible.organization_id,
                organization_name=visible.organization_name,
                detail_path=visible.detail_path,
                location=attachment.location,
            )
        )
    return items


@router.post(
    "/entity-locations",
    response_model=GeoMapItem,
    status_code=status.HTTP_201_CREATED,
)
def create_entity_location(
    payload: EntityLocationCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> GeoMapItem:
    visible = require_visible_entity(
        db,
        current_user,
        payload.entity_type,
        payload.entity_id,
    )

    if not has_map_edit_permission(db, current_user, visible.organization_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: map.edit",
        )

    requested_org_id = payload.location.organization_id
    if requested_org_id is not None and requested_org_id != visible.organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Location organization does not match entity organization",
        )

    organization_id = visible.organization_id
    municipality_id = payload.location.municipality_id
    if municipality_id is None:
        municipality_id = derive_municipality_id(db, organization_id)

    geometry_json = build_point_geojson(
        payload.location.latitude,
        payload.location.longitude,
    )

    attachment = db.scalar(
        select(EntityLocation)
        .options(selectinload(EntityLocation.location))
        .where(
            EntityLocation.entity_type == payload.entity_type,
            EntityLocation.entity_id == payload.entity_id,
            EntityLocation.role == payload.role,
        )
    )

    location_scope_changed = False
    if attachment is None:
        location = GeoLocation(
            organization_id=organization_id,
            municipality_id=municipality_id,
            label=payload.location.label,
            geometry_type="point",
            geometry_json=geometry_json,
            latitude=payload.location.latitude,
            longitude=payload.location.longitude,
            address_text=payload.location.address_text,
            place_name=payload.location.place_name,
            cadastral_reference=payload.location.cadastral_reference,
            source=payload.location.source,
            confidence=payload.location.confidence,
            review_status=payload.location.review_status,
            created_by_id=current_user.id,
        )
        attachment = EntityLocation(
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            role=payload.role,
            created_by_id=current_user.id,
            location=location,
        )
        db.add(attachment)
    else:
        location = attachment.location
        location_scope_changed = (
            location.organization_id != organization_id
            or location.municipality_id != municipality_id
        )
        if location_scope_changed:
            ensure_location_has_no_assets(db, location.id)
        location.organization_id = organization_id
        location.municipality_id = municipality_id
        location.label = payload.location.label
        location.geometry_type = "point"
        location.geometry_json = geometry_json
        location.latitude = payload.location.latitude
        location.longitude = payload.location.longitude
        location.address_text = payload.location.address_text
        location.place_name = payload.location.place_name
        location.cadastral_reference = payload.location.cadastral_reference
        location.source = payload.location.source
        location.confidence = payload.location.confidence
        location.review_status = payload.location.review_status

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                ASSET_LOCATION_SCOPE_CONFLICT
                if location_scope_changed
                else LOCATION_UPDATE_CONFLICT
            ),
        ) from None
    db.refresh(attachment.location)

    return GeoMapItem(
        entity_type=visible.entity_type,  # type: ignore[arg-type]
        entity_id=payload.entity_id,
        title=visible.title,
        subtitle=visible.subtitle,
        status=visible.status,
        priority=visible.priority,
        organization_id=visible.organization_id,
        organization_name=visible.organization_name,
        detail_path=visible.detail_path,
        location=attachment.location,
    )


def ensure_location_has_no_assets(db: Session, location_id: int) -> None:
    asset_id = db.scalar(
        select(MunicipalAsset.id)
        .where(MunicipalAsset.location_id == location_id)
        .limit(1)
    )
    if asset_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ASSET_LOCATION_SCOPE_CONFLICT,
        )
