from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.admin.schemas import (
    AdminUserCreate,
    AdminUserDeleteResponse,
    AdminUserRead,
    AdminUserUpdate,
)
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.rbac.permissions import require_permission
from app.rbac.models import user_groups
from app.users.crud import create_user
from app.users.models import User

router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(require_permission("users.manage"))],
)


@router.get("", response_model=list[AdminUserRead])
def list_users(db: Annotated[Session, Depends(get_db)]) -> list[User]:
    query = select(User).options(selectinload(User.groups)).order_by(User.id)
    return list(db.scalars(query))


@router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
def create_admin_user(
    payload: AdminUserCreate,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    try:
        return create_user(
            db,
            email=str(payload.email),
            password=payload.password,
            full_name=payload.full_name,
            is_active=payload.is_active,
            is_superuser=payload.is_superuser,
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already exists",
        ) from None


@router.get("/{user_id}", response_model=AdminUserRead)
def get_admin_user(
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = db.scalar(
        select(User).options(selectinload(User.groups)).where(User.id == user_id)
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


@router.patch("/{user_id}", response_model=AdminUserRead)
def update_admin_user(
    user_id: int,
    payload: AdminUserUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = db.scalar(
        select(User).options(selectinload(User.groups)).where(User.id == user_id)
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", response_model=AdminUserDeleteResponse)
def delete_admin_user(
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminUserDeleteResponse:
    active_superuser_ids = list(
        db.scalars(
            select(User.id)
            .where(
                User.is_active.is_(True),
                User.is_superuser.is_(True),
            )
            .order_by(User.id)
            .with_for_update()
        )
    )

    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.is_active and user.is_superuser and len(active_superuser_ids) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the last active superuser",
        )

    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    db.execute(delete(user_groups).where(user_groups.c.user_id == user.id))
    db.delete(user)
    db.commit()

    return AdminUserDeleteResponse(user_id=user_id, detail="User deleted")
