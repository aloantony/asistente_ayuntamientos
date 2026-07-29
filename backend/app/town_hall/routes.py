from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.documents.storage import (
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidStorageKeyError,
    LocalStorageService,
    UnsupportedDocumentContentTypeError,
)
from app.town_hall.access import (
    require_town_hall_edit,
    require_town_hall_view,
    resolve_organization,
)
from app.town_hall.models import MunicipalBlock, MunicipalProfile
from app.town_hall.schemas import (
    MunicipalBlockCreate,
    MunicipalBlockRead,
    MunicipalBlockReorder,
    MunicipalBlockUpdate,
    MunicipalNavItemRead,
    MunicipalNavSectionRead,
    MunicipalProfileRead,
    MunicipalProfileUpdate,
    TownHallRead,
)
from app.users.models import User

router = APIRouter(prefix="/town-hall", tags=["town-hall"])
storage_service = LocalStorageService()


def get_profile(db: Session, organization_id: int) -> MunicipalProfile | None:
    return db.scalar(
        select(MunicipalProfile).where(
            MunicipalProfile.organization_id == organization_id
        )
    )


def read_profile(profile: MunicipalProfile | None) -> MunicipalProfileRead:
    if profile is None:
        return MunicipalProfileRead()
    return MunicipalProfileRead(
        display_name=profile.display_name,
        weather_enabled=profile.weather_enabled,
        weather_location=profile.weather_location,
        has_shield=profile.shield_storage_key is not None,
    )


def build_nav(db: Session, organization_id: int) -> list[MunicipalNavSectionRead]:
    """Árbol de navegación activo, ya ordenado por posición."""
    blocks = list(
        db.scalars(
            select(MunicipalBlock)
            .where(
                MunicipalBlock.organization_id == organization_id,
                MunicipalBlock.status == "active",
                MunicipalBlock.block_type.in_(("nav_section", "nav_item")),
            )
            .order_by(MunicipalBlock.position, MunicipalBlock.id)
        )
    )

    sections: dict[int, MunicipalNavSectionRead] = {}
    for block in blocks:
        if block.block_type == "nav_section":
            sections[block.id] = MunicipalNavSectionRead(
                id=block.id,
                title=block.title,
                position=block.position,
                items=[],
            )

    for block in blocks:
        if block.block_type != "nav_item" or block.parent_id is None:
            continue
        section = sections.get(block.parent_id)
        if section is None:
            # Elemento cuyo apartado está archivado: no se muestra.
            continue
        section.items.append(
            MunicipalNavItemRead(
                id=block.id,
                title=block.title,
                position=block.position,
            )
        )

    return list(sections.values())


def get_owned_block(db: Session, block_id: int) -> MunicipalBlock:
    block = db.get(MunicipalBlock, block_id)
    if block is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )
    return block


def next_position(db: Session, organization_id: int, parent_id: int | None) -> int:
    query = select(func.max(MunicipalBlock.position)).where(
        MunicipalBlock.organization_id == organization_id,
        MunicipalBlock.status == "active",
    )
    if parent_id is None:
        query = query.where(MunicipalBlock.parent_id.is_(None))
    else:
        query = query.where(MunicipalBlock.parent_id == parent_id)

    highest = db.scalar(query)
    return 0 if highest is None else highest + 1


def require_parent_section(
    db: Session,
    organization_id: int,
    parent_id: int,
) -> MunicipalBlock:
    parent = db.get(MunicipalBlock, parent_id)
    if (
        parent is None
        or parent.organization_id != organization_id
        or parent.block_type != "nav_section"
        or parent.status != "active"
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Parent must be an active navigation section of the same organization",
        )
    return parent


@router.get("", response_model=TownHallRead)
def read_town_hall(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> TownHallRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_view(db, current_user, organization.id)

    return TownHallRead(
        organization_id=organization.id,
        organization_name=organization.name,
        profile=read_profile(get_profile(db, organization.id)),
        nav=build_nav(db, organization.id),
    )


@router.patch("/profile", response_model=MunicipalProfileRead)
def update_profile(
    payload: MunicipalProfileUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalProfileRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    profile = get_profile(db, organization.id)
    if profile is None:
        profile = MunicipalProfile(organization_id=organization.id)
        db.add(profile)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    profile.updated_by_id = current_user.id

    db.commit()
    db.refresh(profile)
    return read_profile(profile)


@router.post("/shield", response_model=MunicipalProfileRead)
def upload_shield(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalProfileRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    try:
        stored_upload = storage_service.save_branding_file(
            file,
            organization_id=organization.id,
            max_bytes=settings.municipal_shield_max_upload_bytes,
        )
    except UnsupportedDocumentContentTypeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported shield content type",
        ) from None
    except DocumentTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Shield exceeds maximum upload size",
        ) from None
    except EmptyDocumentError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty shield upload",
        ) from None
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid shield storage key",
        ) from None

    profile = get_profile(db, organization.id)
    if profile is None:
        profile = MunicipalProfile(organization_id=organization.id)
        db.add(profile)

    previous_key = profile.shield_storage_key
    profile.shield_storage_key = stored_upload.storage_key
    profile.shield_content_type = stored_upload.content_type
    profile.shield_size_bytes = stored_upload.size_bytes
    profile.updated_by_id = current_user.id

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        storage_service.delete_file(stored_upload.storage_key)
        raise

    # El escudo anterior deja de ser alcanzable: se borra tras confirmar el
    # cambio para no dejar huérfanos en el volumen.
    if previous_key is not None and previous_key != stored_upload.storage_key:
        storage_service.delete_file(previous_key)

    db.refresh(profile)
    return read_profile(profile)


@router.get("/shield")
def download_shield(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> FileResponse:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_view(db, current_user, organization.id)

    profile = get_profile(db, organization.id)
    if profile is None or profile.shield_storage_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shield not found",
        )

    try:
        file_path = storage_service.resolve_storage_key(profile.shield_storage_key)
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shield not found",
        ) from None

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shield not found",
        )

    return FileResponse(
        file_path,
        media_type=profile.shield_content_type or "application/octet-stream",
    )


@router.post(
    "/blocks",
    response_model=MunicipalBlockRead,
    status_code=status.HTTP_201_CREATED,
)
def create_block(
    payload: MunicipalBlockCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> MunicipalBlockRead:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    if payload.block_type == "nav_section":
        if payload.parent_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A navigation section cannot have a parent",
            )
        parent_id = None
    else:
        if payload.parent_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A navigation item requires a parent section",
            )
        parent_id = require_parent_section(db, organization.id, payload.parent_id).id

    block = MunicipalBlock(
        organization_id=organization.id,
        parent_id=parent_id,
        block_type=payload.block_type,
        title=payload.title,
        position=next_position(db, organization.id, parent_id),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    return MunicipalBlockRead.model_validate(block)


@router.post("/blocks/reorder", response_model=list[MunicipalBlockRead])
def reorder_blocks(
    payload: MunicipalBlockReorder,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    organization_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[MunicipalBlockRead]:
    organization = resolve_organization(db, current_user, organization_id)
    require_town_hall_edit(db, current_user, organization.id)

    placements = {placement.id: placement for placement in payload.placements}
    if len(placements) != len(payload.placements):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Duplicate block in reorder payload",
        )

    blocks = list(
        db.scalars(
            select(MunicipalBlock).where(MunicipalBlock.id.in_(placements.keys()))
        )
    )
    if len(blocks) != len(placements):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Block not found",
        )

    for block in blocks:
        # La comprobación por bloque cierra la fuga entre organizaciones: no
        # basta con tener permiso en la organización resuelta.
        if block.organization_id != organization.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Block not found",
            )

        placement = placements[block.id]
        if block.block_type == "nav_section":
            if placement.parent_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A navigation section cannot have a parent",
                )
            block.parent_id = None
        else:
            if placement.parent_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="A navigation item requires a parent section",
                )
            block.parent_id = require_parent_section(
                db,
                organization.id,
                placement.parent_id,
            ).id

        block.position = placement.position
        block.updated_by_id = current_user.id

    db.commit()
    return [MunicipalBlockRead.model_validate(block) for block in blocks]


@router.patch("/blocks/{block_id}", response_model=MunicipalBlockRead)
def update_block(
    block_id: int,
    payload: MunicipalBlockUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalBlockRead:
    block = get_owned_block(db, block_id)
    require_town_hall_edit(db, current_user, block.organization_id)

    fields = payload.model_dump(exclude_unset=True)
    if "title" in fields:
        block.title = fields["title"]
    if "status" in fields:
        block.status = fields["status"]
        # Archivar un apartado archiva sus elementos: preferimos el borrado
        # lógico al físico, pero sin dejar elementos huérfanos reordenables.
        if block.status == "archived" and block.block_type == "nav_section":
            for child in db.scalars(
                select(MunicipalBlock).where(
                    MunicipalBlock.parent_id == block.id,
                    MunicipalBlock.status == "active",
                )
            ):
                child.status = "archived"
                child.updated_by_id = current_user.id

    block.updated_by_id = current_user.id
    db.commit()
    db.refresh(block)
    return MunicipalBlockRead.model_validate(block)
