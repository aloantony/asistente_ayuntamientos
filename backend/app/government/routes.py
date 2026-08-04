from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.government.access import (
    get_government_organization_for_read,
    get_government_organization_for_write,
    require_government_permission,
)
from app.government.models import GovernmentMember
from app.government.schemas import (
    GovernmentLevel,
    GovernmentMemberCreate,
    GovernmentMemberRead,
    GovernmentMemberStatus,
    GovernmentMemberUpdate,
)
from app.users.models import User

router = APIRouter(prefix="/government", tags=["government"])


@router.get("/members", response_model=list[GovernmentMemberRead])
def list_government_members(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    level: GovernmentLevel | None = None,
    status_filter: Annotated[
        GovernmentMemberStatus | None,
        Query(alias="status"),
    ] = None,
    include_archived: bool = False,
) -> list[GovernmentMember]:
    get_government_organization_for_read(db, organization_id)
    require_government_permission(
        db,
        current_user,
        organization_id,
        "government.view",
    )

    query = (
        select(GovernmentMember)
        .where(GovernmentMember.organization_id == organization_id)
        .order_by(
            GovernmentMember.sort_order,
            GovernmentMember.full_name,
            GovernmentMember.id,
        )
    )
    if level is not None:
        query = query.where(GovernmentMember.level == level)
    if status_filter is not None:
        query = query.where(GovernmentMember.status == status_filter)
    elif not include_archived:
        query = query.where(GovernmentMember.status != "archived")

    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/members",
    response_model=GovernmentMemberRead,
    status_code=status.HTTP_201_CREATED,
)
def create_government_member(
    payload: GovernmentMemberCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> GovernmentMember:
    get_government_organization_for_write(db, payload.organization_id)
    require_government_permission(
        db,
        current_user,
        payload.organization_id,
        "government.manage",
    )

    member = GovernmentMember(
        **payload.model_dump(),
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


@router.get("/members/{member_id}", response_model=GovernmentMemberRead)
def get_government_member(
    member_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> GovernmentMember:
    member = get_existing_government_member(db, member_id)
    get_government_organization_for_read(db, member.organization_id)
    require_government_permission(
        db,
        current_user,
        member.organization_id,
        "government.view",
    )
    return member


@router.patch("/members/{member_id}", response_model=GovernmentMemberRead)
def update_government_member(
    member_id: int,
    payload: GovernmentMemberUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> GovernmentMember:
    member = get_existing_government_member(db, member_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        get_government_organization_for_read(db, member.organization_id)
        require_government_permission(
            db,
            current_user,
            member.organization_id,
            "government.view",
        )
        return member

    get_government_organization_for_write(db, member.organization_id)
    require_government_permission(
        db,
        current_user,
        member.organization_id,
        "government.manage",
    )
    # Un parche puede traer una sola fecha del mandato; el rango se comprueba
    # contra el valor ya guardado para que no quede invertido en la base.
    ensure_term_range(
        updates.get("term_start_date", member.term_start_date),
        updates.get("term_end_date", member.term_end_date),
    )

    for field, value in updates.items():
        setattr(member, field, value)
    member.updated_by_id = current_user.id
    db.commit()
    db.refresh(member)
    return member


def ensure_term_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="term_end_date cannot be earlier than term_start_date",
        )


def get_existing_government_member(
    db: Session,
    member_id: int,
) -> GovernmentMember:
    member = db.get(GovernmentMember, member_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Government member not found",
        )
    return member
