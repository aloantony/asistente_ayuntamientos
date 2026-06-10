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
from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.organizations.access import get_user_organization_ids
from app.organizations.models import Organization, organization_users
from app.projects.models import Project, project_groups
from app.rbac.models import Group, group_roles, user_groups
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(
    prefix="/admin/groups",
    tags=["admin-groups"],
)


@router.get("", response_model=list[AdminGroupRead])
def list_groups(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[Group]:
    query = (
        select(Group)
        .options(
            selectinload(Group.organization),
            selectinload(Group.users),
            selectinload(Group.roles),
        )
        .order_by(Group.id)
    )
    if not current_user.is_superuser:
        organization_ids = get_visible_group_organization_ids(db, current_user)
        if not organization_ids:
            raise_permission_required("groups.manage")
        query = query.where(Group.organization_id.in_(organization_ids))

    return list(db.scalars(query))


@router.post("", response_model=AdminGroupRead, status_code=status.HTTP_201_CREATED)
def create_group(
    payload: AdminGroupCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Group:
    ensure_organization_exists(db, payload.organization_id)
    require_groups_manage(db, current_user, payload.organization_id)

    group = Group(
        name=payload.name,
        description=payload.description,
        organization_id=payload.organization_id,
    )
    db.add(group)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Group already exists",
        ) from None

    return get_existing_group(db, group.id)


@router.get("/{group_id}", response_model=AdminGroupRead)
def get_group(
    group_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Group:
    group = get_existing_group(db, group_id)
    require_groups_manage(db, current_user, group.organization_id)
    return group


@router.patch("/{group_id}", response_model=AdminGroupRead)
def update_group(
    group_id: int,
    payload: AdminGroupUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Group:
    group = get_existing_group(db, group_id)
    require_groups_manage(db, current_user, group.organization_id)

    updates = payload.model_dump(exclude_unset=True)
    requested_organization_id = updates.pop("organization_id", None)
    if (
        requested_organization_id is not None
        and requested_organization_id != group.organization_id
    ):
        ensure_organization_exists(db, requested_organization_id)
        require_groups_manage(db, current_user, requested_organization_id)
        ensure_group_can_move_to_organization(
            db,
            group,
            requested_organization_id,
        )
        group.organization_id = requested_organization_id

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

    return get_existing_group(db, group_id)


@router.delete("/{group_id}", response_model=AdminGroupDeleteResponse)
def delete_group(
    group_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AdminGroupDeleteResponse:
    group = db.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
    require_groups_manage(db, current_user, group.organization_id)

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
    current_user: Annotated[User, Depends(get_current_user)],
) -> GroupMembershipResponse:
    group, user = ensure_group_and_user_exist(
        db,
        group_id=group_id,
        user_id=user_id,
    )
    require_groups_manage(db, current_user, group.organization_id)
    ensure_user_belongs_to_organization(
        db,
        user,
        group.organization_id,
        detail="User does not belong to the group organization",
    )

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
    current_user: Annotated[User, Depends(get_current_user)],
) -> GroupMembershipResponse:
    group, _ = ensure_group_and_user_exist(
        db,
        group_id=group_id,
        user_id=user_id,
    )
    require_groups_manage(db, current_user, group.organization_id)

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
) -> tuple[Group, User]:
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return group, user


def get_existing_group(db: Session, group_id: int) -> Group:
    group = db.scalar(
        select(Group)
        .options(
            selectinload(Group.organization),
            selectinload(Group.users),
            selectinload(Group.roles),
        )
        .where(Group.id == group_id)
    )
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )

    return group


def ensure_organization_exists(db: Session, organization_id: int) -> None:
    if db.get(Organization, organization_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )


def ensure_user_belongs_to_organization(
    db: Session,
    user: User,
    organization_id: int,
    *,
    detail: str,
) -> None:
    exists = db.execute(
        select(organization_users).where(
            organization_users.c.organization_id == organization_id,
            organization_users.c.user_id == user.id,
        )
    ).first()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        )


def ensure_group_can_move_to_organization(
    db: Session,
    group: Group,
    organization_id: int,
) -> None:
    invalid_user = db.execute(
        select(user_groups.c.user_id)
        .outerjoin(
            organization_users,
            (organization_users.c.user_id == user_groups.c.user_id)
            & (organization_users.c.organization_id == organization_id),
        )
        .where(
            user_groups.c.group_id == group.id,
            organization_users.c.user_id.is_(None),
        )
        .limit(1)
    ).first()
    if invalid_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot move group because a group user is outside the target organization",
        )

    invalid_project = db.execute(
        select(project_groups.c.project_id)
        .join(Project, Project.id == project_groups.c.project_id)
        .where(
            project_groups.c.group_id == group.id,
            Project.organization_id != organization_id,
        )
        .limit(1)
    ).first()
    if invalid_project is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot move group because it is assigned to a project outside the target organization",
        )


def get_visible_group_organization_ids(
    db: Session,
    current_user: User,
) -> list[int]:
    return [
        organization_id
        for organization_id in get_user_organization_ids(db, current_user)
        if any(
            has_permission(
                current_user,
                permission_code,
                db,
                organization_id=organization_id,
            )
            for permission_code in (
                "groups.manage",
                "roles.manage",
                "projects.manage_members",
            )
        )
    ]


def require_groups_manage(
    db: Session,
    current_user: User,
    organization_id: int,
) -> None:
    if has_permission(
        current_user,
        "groups.manage",
        db,
        organization_id=organization_id,
    ):
        return

    raise_permission_required("groups.manage")


def raise_permission_required(permission_code: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )
