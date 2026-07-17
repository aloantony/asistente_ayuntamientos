from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
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
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.maintenance.guards import (
    ensure_user_has_no_maintenance_history,
    ensure_user_has_no_open_assignments,
    lock_maintenance_users,
)
from app.organizations.access import get_user_organization_ids
from app.organizations.models import organization_users
from app.projects.models import project_users
from app.rbac.models import Group, user_groups
from app.api.routes.auth import set_session_cookie
from app.core.security import create_access_token, hash_password
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
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
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

    return list(db.scalars(paginate(db, query, page, response)))


@router.post("", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
def create_admin_user(
    payload: AdminUserCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    require_users_manage(db, current_user)
    if payload.is_superuser and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superusers can change superuser status",
        )

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
    response: Response,
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

    require_users_manage_for_target(db, current_user, user)

    updates = payload.model_dump(exclude_unset=True)

    if user.is_active and updates.get("is_active") is False:
        ensure_user_has_no_open_assignments(db, user.id)

    new_password = updates.pop("password", None)
    if new_password is not None:
        # Resetting a superuser's password would be an account takeover; the
        # same boundary as granting superuser status applies.
        if user.is_superuser and not current_user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only superusers can reset a superuser password",
            )
        user.hashed_password = hash_password(new_password)
        # Revoke tokens issued before the reset (e.g. the sessions of a
        # compromised account whose password is being rotated).
        user.password_changed_at = datetime.now(UTC)
        if user.id == current_user.id:
            # An admin resetting their own password from the users table
            # would revoke their own session mid-flight; refresh the cookie
            # like the self-service change does.
            set_session_cookie(response, create_access_token(subject=str(user.id)))

    if (
        "is_superuser" in updates
        and updates["is_superuser"] != user.is_superuser
        and not current_user.is_superuser
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superusers can change superuser status",
        )

    if (
        user.is_active
        and user.is_superuser
        and (
            updates.get("is_active") is False
            or updates.get("is_superuser") is False
        )
    ):
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
        if len(active_superuser_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot demote the last active superuser",
            )

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
    # Maintenance assignment and audit writers take this advisory lock before
    # locking User rows. Keep the same order to avoid a User/advisory deadlock.
    lock_maintenance_users(db, user_id)

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

    require_users_manage_for_target(db, current_user, user)

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

    ensure_user_has_no_maintenance_history(db, user.id)

    db.execute(delete(user_groups).where(user_groups.c.user_id == user.id))
    db.execute(delete(project_users).where(project_users.c.user_id == user.id))
    db.execute(
        delete(organization_users).where(organization_users.c.user_id == user.id)
    )
    db.delete(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User has linked records and cannot be deleted",
        ) from None

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


def require_users_manage_for_target(
    db: Session,
    current_user: User,
    target_user: User,
) -> None:
    if current_user.is_superuser:
        return

    # Query the ids instead of touching target_user.organizations: loading the
    # relationship here breaks the delete path, where association rows are
    # removed manually before db.delete() and a populated collection makes the
    # ORM cascade try to delete them again (StaleDataError).
    if any(
        has_permission(
            current_user,
            "users.manage",
            db,
            organization_id=organization_id,
        )
        for organization_id in get_user_organization_ids(db, target_user)
    ):
        return

    raise_permission_required("users.manage")


def raise_permission_required(permission_code: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )
