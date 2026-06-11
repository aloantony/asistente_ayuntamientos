from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.municipalities.models import Municipality
from app.municipalities.schemas import (
    MunicipalityCreate,
    MunicipalityRead,
    MunicipalityStatus,
    MunicipalityUpdate,
)
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/municipalities", tags=["municipalities"])


@router.get("", response_model=list[MunicipalityRead])
def list_municipalities(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    q: str | None = None,
    province: str | None = None,
    autonomous_community: str | None = None,
    status_filter: Annotated[
        MunicipalityStatus | None,
        Query(alias="status"),
    ] = None,
    include_archived: bool = False,
) -> list[Municipality]:
    require_municipality_permission(db, current_user, "municipalities.view")

    query = select(Municipality).order_by(Municipality.name, Municipality.id)
    if q:
        search_text = f"%{q.strip()}%"
        query = query.where(
            or_(
                Municipality.name.ilike(search_text),
                Municipality.province.ilike(search_text),
                Municipality.autonomous_community.ilike(search_text),
                Municipality.ine_code.ilike(search_text),
            )
        )
    if province:
        query = query.where(Municipality.province.ilike(province.strip()))
    if autonomous_community:
        query = query.where(
            Municipality.autonomous_community.ilike(autonomous_community.strip())
        )
    if status_filter is not None:
        query = query.where(Municipality.status == status_filter)
    elif not include_archived:
        query = query.where(Municipality.status != "archived")

    return list(db.scalars(query))


@router.post(
    "",
    response_model=MunicipalityRead,
    status_code=status.HTTP_201_CREATED,
)
def create_municipality(
    payload: MunicipalityCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Municipality:
    require_municipality_permission(db, current_user, "municipalities.create")

    values = payload.model_dump()
    apply_density(values)
    municipality = Municipality(**values)
    db.add(municipality)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Municipality already exists",
        ) from None

    db.refresh(municipality)
    return municipality


@router.get("/{municipality_id}", response_model=MunicipalityRead)
def get_municipality(
    municipality_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Municipality:
    require_municipality_permission(db, current_user, "municipalities.view")
    return get_existing_municipality(db, municipality_id)


@router.patch("/{municipality_id}", response_model=MunicipalityRead)
def update_municipality(
    municipality_id: int,
    payload: MunicipalityUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Municipality:
    municipality = get_existing_municipality(db, municipality_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        require_municipality_permission(db, current_user, "municipalities.view")
        return municipality

    non_status_updates = set(updates) - {"status"}
    if non_status_updates:
        require_municipality_permission(db, current_user, "municipalities.edit")
    if updates.get("status") == "archived":
        require_municipality_permission(db, current_user, "municipalities.archive")
    elif "status" in updates:
        require_municipality_permission(db, current_user, "municipalities.edit")

    apply_density(updates, municipality)
    for field, value in updates.items():
        setattr(municipality, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Municipality already exists",
        ) from None

    db.refresh(municipality)
    return municipality


def apply_density(
    values: dict[str, object],
    municipality: Municipality | None = None,
) -> None:
    population = values.get(
        "population",
        None if municipality is None else municipality.population,
    )
    surface_km2 = values.get(
        "surface_km2",
        None if municipality is None else municipality.surface_km2,
    )
    source_changed = municipality is None or (
        "population" in values or "surface_km2" in values
    )

    if (
        source_changed
        and isinstance(population, int)
        and isinstance(surface_km2, int | float)
        and surface_km2 > 0
    ):
        values["density"] = population / surface_km2
    elif source_changed and "density" not in values:
        values["density"] = None


def get_existing_municipality(db: Session, municipality_id: int) -> Municipality:
    municipality = db.get(Municipality, municipality_id)
    if municipality is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Municipality not found",
        )

    return municipality


def require_municipality_permission(
    db: Session,
    current_user: User,
    permission_code: str,
) -> None:
    if current_user.is_superuser:
        return

    if has_permission(current_user, "municipalities.manage", db):
        return

    if has_permission(current_user, permission_code, db):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )
