from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, literal, or_, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.assets.access import get_asset_organization_for_write
from app.assets.models import MunicipalAsset, MunicipalAssetType
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.geo.access import (
    build_asset_subtitle,
    derive_municipality_id,
    has_asset_location_edit_permission,
    has_asset_view_permission,
    has_any_map_view_permission,
    has_map_edit_permission,
    has_map_view_permission,
    require_visible_entity,
)
from app.geo.geometry import build_point_geojson
from app.geo.models import EntityLocation, GeoLocation
from app.geo.schemas import (
    EntityLocationCreate,
    GeoEntityType,
    GeoLocationCreate,
    GeoMapItem,
)
from app.organizations.models import Organization
from app.organizations.access import get_user_organization_ids
from app.projects.models import Project, project_groups, project_users
from app.rbac.models import Group, user_groups
from app.rbac.permissions import has_permission
from app.requirements.models import Requirement
from app.requirements.routes import build_requirement_visibility_filter
from app.users.models import User

router = APIRouter(prefix="/geo", tags=["geo"])

LOCATION_UPDATE_CONFLICT = "Location update conflicts with existing data"
ASSET_LOCATION_UPDATE_CONFLICT = (
    "Asset location update conflicts with existing data"
)
REQUIREMENT_LAYER_COLOR = "#c0603a"
PROJECT_LAYER_COLOR = "#2f74d0"
ASSET_LAYER_FALLBACK_COLOR = "#3caf8c"


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
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[GeoMapItem]:
    if entity_id is not None and entity_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="entity_type is required when entity_id is provided",
        )
    if not has_any_map_view_permission(db, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: map.view",
        )

    map_organization_ids = get_visible_map_organization_ids(
        db,
        current_user,
        organization_id=organization_id,
    )
    asset_organization_ids = get_visible_asset_map_organization_ids(
        db,
        current_user,
        map_organization_ids=map_organization_ids,
        organization_id=organization_id,
    )

    return list_visible_map_items(
        db,
        current_user,
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=organization_id,
        status_filter=status_filter,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        map_organization_ids=map_organization_ids,
        asset_organization_ids=asset_organization_ids,
    )


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
    if payload.entity_type == "asset":
        return upsert_asset_location(db, current_user, payload)

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
        .with_for_update()
    )

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
        db.add(location)
        attachment.location = location

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=LOCATION_UPDATE_CONFLICT,
        ) from None
    db.refresh(location)

    layer_key = (
        "requirements" if payload.entity_type == "requirement" else "projects"
    )
    layer_label = (
        "Necesidades" if payload.entity_type == "requirement" else "Proyectos"
    )
    layer_color = (
        REQUIREMENT_LAYER_COLOR
        if payload.entity_type == "requirement"
        else PROJECT_LAYER_COLOR
    )

    return GeoMapItem(
        entity_type=visible.entity_type,  # type: ignore[arg-type]
        entity_id=payload.entity_id,
        role=payload.role,
        layer_key=layer_key,
        layer_label=layer_label,
        layer_color=layer_color,
        item_type=None,
        condition_status=None,
        title=visible.title,
        subtitle=visible.subtitle,
        status=visible.status,
        priority=visible.priority,
        organization_id=visible.organization_id,
        organization_name=visible.organization_name,
        detail_path=visible.detail_path,
        location=location,
    )


def get_visible_map_organization_ids(
    db: Session,
    current_user: User,
    *,
    organization_id: int | None,
) -> list[int] | None:
    if current_user.is_superuser:
        return None
    candidate_ids = (
        [organization_id]
        if organization_id is not None
        else get_user_organization_ids(db, current_user)
    )
    return [
        candidate_id
        for candidate_id in candidate_ids
        if has_map_view_permission(db, current_user, candidate_id)
    ]


def get_visible_asset_map_organization_ids(
    db: Session,
    current_user: User,
    *,
    map_organization_ids: list[int] | None,
    organization_id: int | None,
) -> list[int] | None:
    if current_user.is_superuser:
        return None
    candidate_ids = map_organization_ids
    if candidate_ids is None:
        candidate_ids = (
            [organization_id]
            if organization_id is not None
            else get_user_organization_ids(db, current_user)
        )
    return [
        candidate_id
        for candidate_id in candidate_ids
        if has_asset_view_permission(db, current_user, candidate_id)
    ]


def list_visible_map_items(
    db: Session,
    current_user: User,
    *,
    entity_type: GeoEntityType | None,
    entity_id: int | None,
    organization_id: int | None,
    status_filter: str | None,
    include_archived: bool,
    limit: int,
    offset: int,
    map_organization_ids: list[int] | None,
    asset_organization_ids: list[int] | None,
) -> list[GeoMapItem]:
    candidate_queries = []
    if entity_type in {None, "requirement"} and map_organization_ids != []:
        candidate_queries.append(
            build_requirement_map_candidates(
                db,
                current_user,
                entity_id=entity_id,
                organization_id=organization_id,
                status_filter=status_filter,
                include_archived=include_archived,
                visible_organization_ids=map_organization_ids,
            )
        )
    if entity_type in {None, "project"} and map_organization_ids != []:
        candidate_queries.append(
            build_project_map_candidates(
                db,
                current_user,
                entity_id=entity_id,
                organization_id=organization_id,
                status_filter=status_filter,
                include_archived=include_archived,
                visible_organization_ids=map_organization_ids,
            )
        )
    if entity_type in {None, "asset"} and asset_organization_ids != []:
        candidate_queries.append(
            build_asset_map_candidates(
                entity_id=entity_id,
                organization_id=organization_id,
                status_filter=status_filter,
                include_archived=include_archived,
                visible_organization_ids=asset_organization_ids,
            )
        )
    if not candidate_queries:
        return []

    combined_query = (
        union_all(*candidate_queries)
        if len(candidate_queries) > 1
        else candidate_queries[0]
    )
    candidates = combined_query.subquery("visible_map_candidates")
    rows = db.execute(
        select(
            candidates.c.entity_type,
            candidates.c.entity_id,
            candidates.c.role,
            candidates.c.location_id,
        )
        .order_by(
            candidates.c.location_updated_at.desc(),
            candidates.c.location_id.desc(),
            candidates.c.entity_type.asc(),
            candidates.c.entity_id.desc(),
            candidates.c.role.asc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return hydrate_map_candidates(db, rows)


def build_requirement_map_candidates(
    db: Session,
    current_user: User,
    *,
    entity_id: int | None,
    organization_id: int | None,
    status_filter: str | None,
    include_archived: bool,
    visible_organization_ids: list[int] | None,
):
    query = (
        select(
            literal("requirement").label("entity_type"),
            Requirement.id.label("entity_id"),
            EntityLocation.role.label("role"),
            GeoLocation.id.label("location_id"),
            GeoLocation.updated_at.label("location_updated_at"),
        )
        .select_from(Requirement)
        .join(
            EntityLocation,
            and_(
                EntityLocation.entity_type == "requirement",
                EntityLocation.entity_id == Requirement.id,
            ),
        )
        .join(GeoLocation, EntityLocation.location_id == GeoLocation.id)
        .where(
            GeoLocation.review_status != "rejected",
            GeoLocation.organization_id == Requirement.organization_id,
        )
    )
    if entity_id is not None:
        query = query.where(Requirement.id == entity_id)
    if organization_id is not None:
        query = query.where(Requirement.organization_id == organization_id)
    if visible_organization_ids is not None:
        query = query.where(
            Requirement.organization_id.in_(visible_organization_ids)
        )
    if status_filter is not None:
        query = query.where(Requirement.status == status_filter)
    elif not include_archived:
        query = query.where(Requirement.status != "archived")
    if not current_user.is_superuser:
        query = query.where(build_requirement_visibility_filter(db, current_user))
    return query


def build_project_map_candidates(
    db: Session,
    current_user: User,
    *,
    entity_id: int | None,
    organization_id: int | None,
    status_filter: str | None,
    include_archived: bool,
    visible_organization_ids: list[int] | None,
):
    query = (
        select(
            literal("project").label("entity_type"),
            Project.id.label("entity_id"),
            EntityLocation.role.label("role"),
            GeoLocation.id.label("location_id"),
            GeoLocation.updated_at.label("location_updated_at"),
        )
        .select_from(Project)
        .join(
            EntityLocation,
            and_(
                EntityLocation.entity_type == "project",
                EntityLocation.entity_id == Project.id,
            ),
        )
        .join(GeoLocation, EntityLocation.location_id == GeoLocation.id)
        .where(
            GeoLocation.review_status != "rejected",
            GeoLocation.organization_id == Project.organization_id,
        )
    )
    if entity_id is not None:
        query = query.where(Project.id == entity_id)
    if organization_id is not None:
        query = query.where(Project.organization_id == organization_id)
    if visible_organization_ids is not None:
        query = query.where(Project.organization_id.in_(visible_organization_ids))
    if status_filter is not None:
        query = query.where(Project.status == status_filter)
    elif not include_archived:
        query = query.where(Project.status != "archived")
    if not current_user.is_superuser:
        organization_ids = visible_organization_ids or []
        view_all_ids = [
            candidate_id
            for candidate_id in organization_ids
            if has_permission(
                current_user,
                "projects.view_all",
                db,
                organization_id=candidate_id,
            )
        ]
        direct_assignment = exists().where(
            project_users.c.project_id == Project.id,
            project_users.c.user_id == current_user.id,
        )
        group_assignment = exists().where(
            project_groups.c.project_id == Project.id,
            project_groups.c.group_id == user_groups.c.group_id,
            project_groups.c.group_id == Group.id,
            Group.organization_id == Project.organization_id,
            user_groups.c.user_id == current_user.id,
        )
        visibility_filters = [direct_assignment, group_assignment]
        if view_all_ids:
            visibility_filters.append(Project.organization_id.in_(view_all_ids))
        query = query.where(or_(*visibility_filters))
    return query


def build_asset_map_candidates(
    *,
    entity_id: int | None,
    organization_id: int | None,
    status_filter: str | None,
    include_archived: bool,
    visible_organization_ids: list[int] | None,
):
    query = (
        select(
            literal("asset").label("entity_type"),
            MunicipalAsset.id.label("entity_id"),
            literal("primary").label("role"),
            GeoLocation.id.label("location_id"),
            GeoLocation.updated_at.label("location_updated_at"),
        )
        .select_from(MunicipalAsset)
        .join(GeoLocation, MunicipalAsset.location_id == GeoLocation.id)
        .join(Organization, MunicipalAsset.organization_id == Organization.id)
        .where(
            GeoLocation.review_status != "rejected",
            Organization.status.in_(("active", "paused")),
        )
    )
    if entity_id is not None:
        query = query.where(MunicipalAsset.id == entity_id)
    if organization_id is not None:
        query = query.where(MunicipalAsset.organization_id == organization_id)
    if visible_organization_ids is not None:
        query = query.where(
            MunicipalAsset.organization_id.in_(visible_organization_ids)
        )
    if status_filter is not None:
        query = query.where(MunicipalAsset.status == status_filter)
    elif not include_archived:
        query = query.where(MunicipalAsset.status != "archived")
    return query


def hydrate_map_candidates(db: Session, rows) -> list[GeoMapItem]:
    location_ids = {row.location_id for row in rows}
    locations = {
        location.id: location
        for location in db.scalars(
            select(GeoLocation).where(GeoLocation.id.in_(location_ids))
        )
    }
    requirement_ids = {
        row.entity_id for row in rows if row.entity_type == "requirement"
    }
    requirements = {}
    if requirement_ids:
        requirements = {
            requirement.id: requirement
            for requirement in db.scalars(
                select(Requirement)
                .options(selectinload(Requirement.organization))
                .where(Requirement.id.in_(requirement_ids))
            )
        }
    project_ids = {row.entity_id for row in rows if row.entity_type == "project"}
    projects = {}
    if project_ids:
        projects = {
            project.id: project
            for project in db.scalars(
                select(Project)
                .options(selectinload(Project.organization))
                .where(Project.id.in_(project_ids))
            )
        }
    asset_ids = {row.entity_id for row in rows if row.entity_type == "asset"}
    assets = {}
    if asset_ids:
        asset_rows = db.execute(
            select(MunicipalAsset, Organization.name)
            .join(Organization, MunicipalAsset.organization_id == Organization.id)
            .options(
                selectinload(MunicipalAsset.asset_type).selectinload(
                    MunicipalAssetType.category
                )
            )
            .where(MunicipalAsset.id.in_(asset_ids))
        )
        assets = {asset.id: (asset, name) for asset, name in asset_rows}

    items: list[GeoMapItem] = []
    for row in rows:
        location = locations.get(row.location_id)
        if location is None:
            continue
        if row.entity_type == "requirement":
            requirement = requirements.get(row.entity_id)
            if requirement is None:
                continue
            items.append(
                GeoMapItem(
                    entity_type="requirement",
                    entity_id=requirement.id,
                    role=row.role,
                    layer_key="requirements",
                    layer_label="Necesidades",
                    layer_color=REQUIREMENT_LAYER_COLOR,
                    item_type=None,
                    condition_status=None,
                    title=requirement.title,
                    subtitle=requirement.summary,
                    status=requirement.status,
                    priority=requirement.priority,
                    organization_id=requirement.organization_id,
                    organization_name=requirement.organization.name,
                    detail_path=f"/requisitos?id={requirement.id}",
                    location=location,
                )
            )
        elif row.entity_type == "project":
            project = projects.get(row.entity_id)
            if project is None:
                continue
            items.append(
                GeoMapItem(
                    entity_type="project",
                    entity_id=project.id,
                    role=row.role,
                    layer_key="projects",
                    layer_label="Proyectos",
                    layer_color=PROJECT_LAYER_COLOR,
                    item_type=None,
                    condition_status=None,
                    title=project.name,
                    subtitle=project.description,
                    status=project.status,
                    priority=None,
                    organization_id=project.organization_id,
                    organization_name=project.organization.name,
                    detail_path="/proyectos",
                    location=location,
                )
            )
        else:
            asset_row = assets.get(row.entity_id)
            if asset_row is None:
                continue
            asset, organization_name = asset_row
            items.append(
                GeoMapItem(
                    entity_type="asset",
                    entity_id=asset.id,
                    role="primary",
                    layer_key=f"asset-category-{asset.asset_type.category.id}",
                    layer_label=asset.asset_type.category.name,
                    layer_color=(
                        asset.asset_type.category.color
                        or ASSET_LAYER_FALLBACK_COLOR
                    ),
                    item_type=asset.asset_type.name,
                    condition_status=asset.condition_status,
                    title=asset.name,
                    subtitle=build_asset_subtitle(asset),
                    status=asset.status,
                    priority=None,
                    organization_id=asset.organization_id,
                    organization_name=organization_name,
                    detail_path="/ayuntamiento",
                    location=location,
                )
            )
    return items


def upsert_asset_location(
    db: Session,
    current_user: User,
    payload: EntityLocationCreate,
) -> GeoMapItem:
    if payload.role != "primary":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Asset locations only support the primary role",
        )

    asset = db.scalar(
        select(MunicipalAsset)
        .options(
            selectinload(MunicipalAsset.asset_type).selectinload(
                MunicipalAssetType.category
            )
        )
        .where(MunicipalAsset.id == payload.entity_id)
    )
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entity access denied",
        )
    if not has_asset_location_edit_permission(
        db,
        current_user,
        asset.organization_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permissions required: assets.view and assets.edit",
        )
    if not has_map_edit_permission(db, current_user, asset.organization_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: map.edit",
        )

    asset = db.scalar(
        select(MunicipalAsset)
        .options(
            selectinload(MunicipalAsset.asset_type).selectinload(
                MunicipalAssetType.category
            )
        )
        .where(MunicipalAsset.id == payload.entity_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset changed while assigning its location",
        )
    organization = get_asset_organization_for_write(db, asset.organization_id)
    if organization.municipality_id != asset.municipality_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset municipality does not match organization municipality",
        )
    requested_org_id = payload.location.organization_id
    if requested_org_id is not None and requested_org_id != asset.organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Location organization does not match asset organization",
        )
    requested_municipality_id = payload.location.municipality_id
    if (
        requested_municipality_id is not None
        and requested_municipality_id != asset.municipality_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Location municipality does not match asset municipality",
        )

    try:
        location = build_asset_location(
            payload.location,
            organization_id=asset.organization_id,
            municipality_id=asset.municipality_id,
            created_by_id=current_user.id,
        )
        db.add(location)
        db.flush()
        asset.location_id = location.id
        asset.updated_by_id = current_user.id
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ASSET_LOCATION_UPDATE_CONFLICT,
        ) from None

    db.refresh(location)
    return GeoMapItem(
        entity_type="asset",
        entity_id=asset.id,
        role="primary",
        layer_key=f"asset-category-{asset.asset_type.category.id}",
        layer_label=asset.asset_type.category.name,
        layer_color=(
            asset.asset_type.category.color or ASSET_LAYER_FALLBACK_COLOR
        ),
        item_type=asset.asset_type.name,
        condition_status=asset.condition_status,
        title=asset.name,
        subtitle=build_asset_subtitle(asset),
        status=asset.status,
        priority=None,
        organization_id=asset.organization_id,
        organization_name=organization.name,
        detail_path="/ayuntamiento",
        location=location,
    )


def build_asset_location(
    payload: GeoLocationCreate,
    *,
    organization_id: int,
    municipality_id: int,
    created_by_id: int,
) -> GeoLocation:
    return GeoLocation(
        organization_id=organization_id,
        municipality_id=municipality_id,
        label=payload.label,
        geometry_type="point",
        geometry_json=build_point_geojson(payload.latitude, payload.longitude),
        latitude=payload.latitude,
        longitude=payload.longitude,
        address_text=payload.address_text,
        place_name=payload.place_name,
        cadastral_reference=payload.cadastral_reference,
        source="user_provided",
        confidence=payload.confidence,
        review_status="proposed",
        created_by_id=created_by_id,
    )
