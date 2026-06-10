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
from app.organizations.access import get_user_organization_ids
from app.organizations.models import organization_users
from app.projects.models import project_users
from app.rbac.models import Group, user_groups
from app.rbac.permissions import has_permission
from app.users.crud import create_user
from app.users.models import User

router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
)


@router.get("", response_model=list[AdminUserRead])
def list_users(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[User]:
    query = (
        select(User)
        .options(
            selectinload(User.groups).selectinload(Group.organization),
            selectinload(User.organizations),
        )
        .order_by(User.id)
    )
    if not current_user.is_superuser:
        visible_organization_ids = get_visible_user_organization_ids(db, current_user)
        if not visible_organization_ids:
            raise_permission_required("users.manage")

        query = (
            query.join(
                organization_users,
                organization_users.c.user_id == User.id,
            )
            .where(
                organization_users.c.organization_id.in_(visible_organization_ids)
            )
            .distinct()
        )

    return list(db.scalars(query))


@router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
def create_admin_user(
    payload: AdminUserCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    require_users_manage(db, current_user)

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
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    user = db.scalar(
        select(User)
        .options(
            selectinload(User.groups).selectinload(Group.organization),
            selectinload(User.organizations),
        )
        .where(User.id == user_id)
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    if not current_user.is_superuser:
        visible_organization_ids = set(
            get_visible_user_organization_ids(db, current_user)
        )
        if not any(
            organization.id in visible_organization_ids
            for organization in user.organizations
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User access denied",
            )
    return user


@router.patch("/{user_id}", response_model=AdminUserRead)
def update_admin_user(
    user_id: int,
    payload: AdminUserUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    require_users_manage(db, current_user)

    user = db.scalar(
        select(User)
        .options(
            selectinload(User.groups).selectinload(Group.organization),
            selectinload(User.organizations),
        )
        .where(User.id == user_id)
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
    require_users_manage(db, current_user)

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
    db.execute(delete(project_users).where(project_users.c.user_id == user.id))
    db.execute(
        delete(organization_users).where(organization_users.c.user_id == user.id)
    )
    db.delete(user)
    db.commit()

    return AdminUserDeleteResponse(user_id=user_id, detail="User deleted")


def get_visible_user_organization_ids(db: Session, current_user: User) -> list[int]:
    organization_ids = get_user_organization_ids(db, current_user)
    return [
        organization_id
        for organization_id in organization_ids
        if any(
            has_permission(
                current_user,
                permission_code,
                db,
                organization_id=organization_id,
            )
            for permission_code in (
                "users.manage",
                "groups.manage",
                "organizations.manage",
                "projects.manage_members",
            )
        )
    ]


def require_users_manage(db: Session, current_user: User) -> None:
    if has_permission(current_user, "users.manage", db):
        return

    raise_permission_required("users.manage")


def raise_permission_required(permission_code: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )
