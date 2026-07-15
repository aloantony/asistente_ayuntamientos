from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.assets.access import (
    get_asset_organization_for_read,
    get_asset_organization_for_write,
    require_asset_permission,
)
from app.assets.models import (
    MunicipalAsset,
    MunicipalAssetCategory,
    MunicipalAssetType,
)
from app.assets.schemas import (
    AssetCategoryCreate,
    AssetCategoryRead,
    AssetCategoryUpdate,
    AssetConditionStatus,
    AssetStatus,
    AssetTaxonomyStatus,
    AssetTypeCreate,
    AssetTypeRead,
    AssetTypeUpdate,
    MunicipalAssetCreate,
    MunicipalAssetRead,
    MunicipalAssetUpdate,
)
from app.maintenance.models import MaintenanceOrder
from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.users.models import User

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/categories", response_model=list[AssetCategoryRead])
def list_asset_categories(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[
        AssetTaxonomyStatus | None,
        Query(alias="status"),
    ] = None,
    include_archived: bool = False,
) -> list[MunicipalAssetCategory]:
    get_asset_organization_for_read(db, organization_id)
    require_asset_permission(db, current_user, organization_id, "assets.view")

    query = (
        select(MunicipalAssetCategory)
        .where(MunicipalAssetCategory.organization_id == organization_id)
        .order_by(
            MunicipalAssetCategory.sort_order,
            MunicipalAssetCategory.name,
            MunicipalAssetCategory.id,
        )
    )
    if status_filter is not None:
        query = query.where(MunicipalAssetCategory.status == status_filter)
    elif not include_archived:
        query = query.where(MunicipalAssetCategory.status != "archived")

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/categories",
    response_model=AssetCategoryRead,
    status_code=status.HTTP_201_CREATED,
)
def create_asset_category(
    payload: AssetCategoryCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAssetCategory:
    get_asset_organization_for_write(db, payload.organization_id)
    require_asset_permission(
        db,
        current_user,
        payload.organization_id,
        "assets.create",
    )
    if payload.status == "archived":
        require_asset_permission(
            db,
            current_user,
            payload.organization_id,
            "assets.archive",
        )

    category = MunicipalAssetCategory(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(category)
    commit_or_conflict(db, "Asset category code already exists")
    db.refresh(category)
    return category


@router.patch("/categories/{category_id}", response_model=AssetCategoryRead)
def update_asset_category(
    category_id: int,
    payload: AssetCategoryUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAssetCategory:
    category = get_existing_asset_category(db, category_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_asset_organization_for_read(db, category.organization_id)
        require_asset_permission(
            db,
            current_user,
            category.organization_id,
            "assets.view",
        )
        return category

    get_asset_organization_for_write(db, category.organization_id)
    require_asset_update_permissions(
        db,
        current_user,
        organization_id=category.organization_id,
        updates=updates,
    )

    for field, value in updates.items():
        setattr(category, field, value)
    category.updated_by_id = current_user.id
    commit_or_conflict(db, "Asset category code already exists")
    db.refresh(category)
    return category


@router.get("/types", response_model=list[AssetTypeRead])
def list_asset_types(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    category_id: int | None = None,
    status_filter: Annotated[
        AssetTaxonomyStatus | None,
        Query(alias="status"),
    ] = None,
    include_archived: bool = False,
) -> list[MunicipalAssetType]:
    get_asset_organization_for_read(db, organization_id)
    require_asset_permission(db, current_user, organization_id, "assets.view")

    query = (
        select_asset_types()
        .where(MunicipalAssetType.organization_id == organization_id)
        .order_by(
            MunicipalAssetType.sort_order,
            MunicipalAssetType.name,
            MunicipalAssetType.id,
        )
    )
    if category_id is not None:
        ensure_category_matches_organization(
            db,
            category_id=category_id,
            organization_id=organization_id,
        )
        query = query.where(MunicipalAssetType.category_id == category_id)
    if status_filter is not None:
        query = query.where(MunicipalAssetType.status == status_filter)
    elif not include_archived:
        query = query.where(MunicipalAssetType.status != "archived")

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/types",
    response_model=AssetTypeRead,
    status_code=status.HTTP_201_CREATED,
)
def create_asset_type(
    payload: AssetTypeCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAssetType:
    get_asset_organization_for_write(db, payload.organization_id)
    require_asset_permission(
        db,
        current_user,
        payload.organization_id,
        "assets.create",
    )
    ensure_category_matches_organization(
        db,
        category_id=payload.category_id,
        organization_id=payload.organization_id,
        require_active=True,
    )
    if payload.status == "archived":
        require_asset_permission(
            db,
            current_user,
            payload.organization_id,
            "assets.archive",
        )

    asset_type = MunicipalAssetType(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(asset_type)
    commit_or_conflict(db, "Asset type code already exists in category")
    return get_existing_asset_type(db, asset_type.id)


@router.patch("/types/{asset_type_id}", response_model=AssetTypeRead)
def update_asset_type(
    asset_type_id: int,
    payload: AssetTypeUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAssetType:
    asset_type = get_existing_asset_type(db, asset_type_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_asset_organization_for_read(db, asset_type.organization_id)
        require_asset_permission(
            db,
            current_user,
            asset_type.organization_id,
            "assets.view",
        )
        return asset_type

    get_asset_organization_for_write(db, asset_type.organization_id)
    require_asset_update_permissions(
        db,
        current_user,
        organization_id=asset_type.organization_id,
        updates=updates,
    )
    if "category_id" in updates:
        ensure_category_matches_organization(
            db,
            category_id=updates["category_id"],
            organization_id=asset_type.organization_id,
            require_active=True,
        )

    for field, value in updates.items():
        setattr(asset_type, field, value)
    asset_type.updated_by_id = current_user.id
    commit_or_conflict(db, "Asset type code already exists in category")
    return get_existing_asset_type(db, asset_type.id)


@router.get("", response_model=list[MunicipalAssetRead])
def list_assets(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    category_id: int | None = None,
    asset_type_id: int | None = None,
    status_filter: Annotated[
        AssetStatus | None,
        Query(alias="status"),
    ] = None,
    condition_status: AssetConditionStatus | None = None,
    include_archived: bool = False,
) -> list[MunicipalAsset]:
    get_asset_organization_for_read(db, organization_id)
    require_asset_permission(db, current_user, organization_id, "assets.view")

    query = (
        select_assets_with_summaries()
        .where(MunicipalAsset.organization_id == organization_id)
        .order_by(MunicipalAsset.name, MunicipalAsset.id)
    )
    if q and q.strip():
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                MunicipalAsset.code.ilike(search_text),
                MunicipalAsset.name.ilike(search_text),
                MunicipalAsset.description.ilike(search_text),
                MunicipalAsset.material.ilike(search_text),
            )
        )
    if category_id is not None:
        ensure_category_matches_organization(
            db,
            category_id=category_id,
            organization_id=organization_id,
        )
        query = query.join(MunicipalAsset.asset_type).where(
            MunicipalAssetType.category_id == category_id
        )
    if asset_type_id is not None:
        ensure_type_matches_organization(
            db,
            asset_type_id=asset_type_id,
            organization_id=organization_id,
        )
        query = query.where(MunicipalAsset.asset_type_id == asset_type_id)
    if status_filter is not None:
        query = query.where(MunicipalAsset.status == status_filter)
    elif not include_archived:
        query = query.where(MunicipalAsset.status != "archived")
    if condition_status is not None:
        query = query.where(MunicipalAsset.condition_status == condition_status)

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "",
    response_model=MunicipalAssetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_asset(
    payload: MunicipalAssetCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAsset:
    organization = get_asset_organization_for_write(db, payload.organization_id)
    require_asset_permission(
        db,
        current_user,
        payload.organization_id,
        "assets.create",
    )
    ensure_type_matches_organization(
        db,
        asset_type_id=payload.asset_type_id,
        organization_id=payload.organization_id,
        require_active=True,
    )
    if payload.status == "archived":
        require_asset_permission(
            db,
            current_user,
            payload.organization_id,
            "assets.archive",
        )

    values = payload.model_dump()
    values["municipality_id"] = organization.municipality_id
    asset = MunicipalAsset(
        **values,
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(asset)
    commit_or_conflict(db, "Asset code already exists")
    return get_existing_asset(db, asset.id)


@router.get("/{asset_id}", response_model=MunicipalAssetRead)
def get_asset(
    asset_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAsset:
    asset = get_existing_asset(db, asset_id)
    get_asset_organization_for_read(db, asset.organization_id)
    require_asset_permission(db, current_user, asset.organization_id, "assets.view")
    return asset


@router.patch("/{asset_id}", response_model=MunicipalAssetRead)
def update_asset(
    asset_id: int,
    payload: MunicipalAssetUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalAsset:
    asset = get_existing_asset(db, asset_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_asset_organization_for_read(db, asset.organization_id)
        require_asset_permission(
            db,
            current_user,
            asset.organization_id,
            "assets.view",
        )
        return asset

    get_asset_organization_for_write(db, asset.organization_id)
    require_asset_update_permissions(
        db,
        current_user,
        organization_id=asset.organization_id,
        updates=updates,
    )
    if "asset_type_id" in updates:
        ensure_type_matches_organization(
            db,
            asset_type_id=updates["asset_type_id"],
            organization_id=asset.organization_id,
            require_active=True,
        )
    if updates.get("status") in {"retired", "archived"}:
        asset = get_locked_asset(db, asset_id)
        has_open_maintenance = db.scalar(
            select(MaintenanceOrder.id)
            .where(
                MaintenanceOrder.asset_id == asset.id,
                MaintenanceOrder.status.in_(
                    ("planned", "scheduled", "in_progress")
                ),
            )
            .limit(1)
        )
        if has_open_maintenance is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Asset has open maintenance orders",
            )
    for field, value in updates.items():
        setattr(asset, field, value)
    asset.updated_by_id = current_user.id
    commit_or_conflict(db, "Asset code already exists")
    return get_existing_asset(db, asset.id)


def select_asset_types():
    return (
        select(MunicipalAssetType)
        .options(selectinload(MunicipalAssetType.category))
        .execution_options(populate_existing=True)
    )


def select_assets_with_summaries():
    return (
        select(MunicipalAsset)
        .options(
            selectinload(MunicipalAsset.asset_type).selectinload(
                MunicipalAssetType.category
            ),
            selectinload(MunicipalAsset.location),
        )
        .execution_options(populate_existing=True)
    )


def get_existing_asset_category(
    db: Session,
    category_id: int,
) -> MunicipalAssetCategory:
    category = db.get(MunicipalAssetCategory, category_id)
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset category not found",
        )
    return category


def get_existing_asset_type(db: Session, asset_type_id: int) -> MunicipalAssetType:
    asset_type = db.scalar(
        select_asset_types().where(MunicipalAssetType.id == asset_type_id)
    )
    if asset_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset type not found",
        )
    return asset_type


def get_existing_asset(db: Session, asset_id: int) -> MunicipalAsset:
    asset = db.scalar(
        select_assets_with_summaries().where(MunicipalAsset.id == asset_id)
    )
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset not found",
        )
    return asset


def get_locked_asset(db: Session, asset_id: int) -> MunicipalAsset:
    asset = db.scalar(
        select(MunicipalAsset)
        .where(MunicipalAsset.id == asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset not found",
        )
    return asset


def ensure_category_matches_organization(
    db: Session,
    *,
    category_id: int,
    organization_id: int,
    require_active: bool = False,
) -> MunicipalAssetCategory:
    category = get_existing_asset_category(db, category_id)
    if category.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset category does not belong to the organization",
        )
    if require_active and category.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset category must be active",
        )
    return category


def ensure_type_matches_organization(
    db: Session,
    *,
    asset_type_id: int,
    organization_id: int,
    require_active: bool = False,
) -> MunicipalAssetType:
    asset_type = get_existing_asset_type(db, asset_type_id)
    if asset_type.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset type does not belong to the organization",
        )
    if asset_type.category.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset type category does not belong to the organization",
        )
    if require_active and asset_type.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset type must be active",
        )
    if require_active and asset_type.category.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Asset type category must be active",
        )
    return asset_type


def require_asset_update_permissions(
    db: Session,
    current_user: User,
    *,
    organization_id: int,
    updates: dict[str, object],
) -> None:
    if set(updates) - {"status"}:
        require_asset_permission(
            db,
            current_user,
            organization_id,
            "assets.edit",
        )
    if updates.get("status") == "archived":
        require_asset_permission(
            db,
            current_user,
            organization_id,
            "assets.archive",
        )
    elif "status" in updates:
        require_asset_permission(
            db,
            current_user,
            organization_id,
            "assets.edit",
        )


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
