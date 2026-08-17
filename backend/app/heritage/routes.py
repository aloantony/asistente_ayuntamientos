from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.documents.models import Document
from app.heritage.access import (
    get_heritage_organization_for_read,
    get_heritage_organization_for_write,
    require_heritage_permission,
)
from app.heritage.models import ArchiveItem, HeritageAsset
from app.heritage.schemas import (
    ArchiveItemCreate,
    ArchiveItemRead,
    ArchiveKind,
    DigitisationState,
    HeritageAssetCreate,
    HeritageAssetRead,
    HeritageKind,
    ProtectionLevel,
)
from app.users.models import User

router = APIRouter(prefix="/heritage", tags=["heritage"])


@router.get("/assets", response_model=list[HeritageAssetRead])
def list_heritage_assets(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    kind: HeritageKind | None = None,
    protection_level: ProtectionLevel | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[HeritageAsset]:
    get_heritage_organization_for_read(db, organization_id)
    require_heritage_permission(db, current_user, organization_id, "heritage.view")

    query = (
        select(HeritageAsset)
        .where(HeritageAsset.organization_id == organization_id)
        .order_by(HeritageAsset.name, HeritageAsset.id)
    )
    if kind is not None:
        query = query.where(HeritageAsset.kind == kind)
    if protection_level is not None:
        query = query.where(HeritageAsset.protection_level == protection_level)
    if q and q.strip():
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                HeritageAsset.name.ilike(search_text),
                HeritageAsset.description.ilike(search_text),
                HeritageAsset.period.ilike(search_text),
            )
        )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/assets",
    response_model=HeritageAssetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_heritage_asset(
    payload: HeritageAssetCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> HeritageAsset:
    get_heritage_organization_for_write(db, payload.organization_id)
    require_heritage_permission(
        db,
        current_user,
        payload.organization_id,
        "heritage.edit",
    )

    asset = HeritageAsset(**payload.model_dump())
    db.add(asset)
    commit_or_conflict(db, "A heritage asset with that slug already exists")
    db.refresh(asset)
    return asset


@router.get("/archive", response_model=list[ArchiveItemRead])
def list_archive_items(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    kind: ArchiveKind | None = None,
    digitisation_state: DigitisationState | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[ArchiveItem]:
    get_heritage_organization_for_read(db, organization_id)
    require_heritage_permission(db, current_user, organization_id, "heritage.view")

    query = (
        select(ArchiveItem)
        .where(ArchiveItem.organization_id == organization_id)
        # Lo más antiguo primero: un archivo se recorre cronológicamente, y lo
        # que no tiene año va al final en lugar de encabezar la lista.
        .order_by(
            ArchiveItem.start_year.is_(None),
            ArchiveItem.start_year,
            ArchiveItem.reference,
        )
    )
    if kind is not None:
        query = query.where(ArchiveItem.kind == kind)
    if digitisation_state is not None:
        query = query.where(ArchiveItem.digitisation_state == digitisation_state)
    if q and q.strip():
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                ArchiveItem.title.ilike(search_text),
                ArchiveItem.description.ilike(search_text),
                ArchiveItem.reference.ilike(search_text),
                ArchiveItem.physical_location.ilike(search_text),
            )
        )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/archive",
    response_model=ArchiveItemRead,
    status_code=status.HTTP_201_CREATED,
)
def create_archive_item(
    payload: ArchiveItemCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ArchiveItem:
    get_heritage_organization_for_write(db, payload.organization_id)
    require_heritage_permission(
        db,
        current_user,
        payload.organization_id,
        "heritage.edit",
    )
    if payload.document_id is not None:
        ensure_document_belongs_to_organization(
            db,
            document_id=payload.document_id,
            organization_id=payload.organization_id,
        )
    if payload.heritage_asset_id is not None:
        ensure_asset_belongs_to_organization(
            db,
            asset_id=payload.heritage_asset_id,
            organization_id=payload.organization_id,
        )

    item = ArchiveItem(**payload.model_dump())
    db.add(item)
    commit_or_conflict(db, "An archive item with that reference already exists")
    db.refresh(item)
    return item


def ensure_document_belongs_to_organization(
    db: Session,
    *,
    document_id: int,
    organization_id: int,
) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )
    if document.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document does not belong to the organization",
        )
    return document


def ensure_asset_belongs_to_organization(
    db: Session,
    *,
    asset_id: int,
    organization_id: int,
) -> HeritageAsset:
    asset = db.get(HeritageAsset, asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Heritage asset not found",
        )
    if asset.organization_id != organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Heritage asset does not belong to the organization",
        )
    return asset


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
