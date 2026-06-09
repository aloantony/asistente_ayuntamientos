from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.admin.schemas import (
    AdminGroupCreate,
    AdminGroupDeleteResponse,
    AdminGroupRead,
    AdminGroupUpdate,
    GroupMembershipResponse,
)
from app.db.session import get_db
from app.rbac.models import Group, group_roles, user_groups
from app.rbac.permissions import require_permission
from app.users.models import User

router = APIRouter(
    prefix="/admin/groups",
    tags=["admin-groups"],
    dependencies=[Depends(require_permission("groups.manage"))],
)


@router.get("", response_model=list[AdminGroupRead])
def list_groups(db: Annotated[Session, Depends(get_db)]) -> list[Group]:
    query = (
        select(Group)
        .options(selectinload(Group.users), selectinload(Group.roles))
        .order_by(Group.id)
    )
    return list(db.scalars(query))


@router.post("", response_model=AdminGroupRead, status_code=status.HTTP_201_CREATED)
def create_group(
    payload: AdminGroupCreate,
    db: Annotated[Session, Depends(get_db)],
) -> Group:
    group = Group(name=payload.name, description=payload.description)
    db.add(group)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Group already exists",
        ) from None

    db.refresh(group)
    return group


@router.get("/{group_id}", response_model=AdminGroupRead)
def get_group(
    group_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> Group:
    group = db.scalar(
        select(Group)
        .options(selectinload(Group.users), selectinload(Group.roles))
        .where(Group.id == group_id)
    )
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
    return group


@router.patch("/{group_id}", response_model=AdminGroupRead)
def update_group(
    group_id: int,
    payload: AdminGroupUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> Group:
    group = db.scalar(
        select(Group)
        .options(selectinload(Group.users), selectinload(Group.roles))
        .where(Group.id == group_id)
    )
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(group, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Group already exists",
        ) from None

    db.refresh(group)
    return group


@router.delete("/{group_id}", response_model=AdminGroupDeleteResponse)
def delete_group(
    group_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> AdminGroupDeleteResponse:
    group = db.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    db.execute(delete(user_groups).where(user_groups.c.group_id == group.id))
    db.execute(delete(group_roles).where(group_roles.c.group_id == group.id))
    db.delete(group)
    db.commit()

    return AdminGroupDeleteResponse(group_id=group_id, detail="Group deleted")


@router.post(
    "/{group_id}/users/{user_id}",
    response_model=GroupMembershipResponse,
    status_code=status.HTTP_200_OK,
)
def add_user_to_group(
    group_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> GroupMembershipResponse:
    ensure_group_and_user_exist(db, group_id=group_id, user_id=user_id)

    exists = db.execute(
        select(user_groups).where(
            user_groups.c.group_id == group_id,
            user_groups.c.user_id == user_id,
        )
    ).first()
    if exists is None:
        db.execute(insert(user_groups).values(group_id=group_id, user_id=user_id))
        db.commit()

    return GroupMembershipResponse(
        group_id=group_id,
        user_id=user_id,
        detail="User is in group",
    )


@router.delete(
    "/{group_id}/users/{user_id}",
    response_model=GroupMembershipResponse,
)
def remove_user_from_group(
    group_id: int,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> GroupMembershipResponse:
    ensure_group_and_user_exist(db, group_id=group_id, user_id=user_id)

    db.execute(
        delete(user_groups).where(
            user_groups.c.group_id == group_id,
            user_groups.c.user_id == user_id,
        )
    )
    db.commit()

    return GroupMembershipResponse(
        group_id=group_id,
        user_id=user_id,
        detail="User is not in group",
    )


def ensure_group_and_user_exist(
    db: Session,
    *,
    group_id: int,
    user_id: int,
) -> None:
    if db.get(Group, group_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
    if db.get(User, user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
