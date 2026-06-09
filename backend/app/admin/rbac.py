from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.admin.schemas import (
    GroupRoleResponse,
    PermissionBootstrapResponse,
    PermissionRead,
    RoleCreate,
    RoleDeleteResponse,
    RolePermissionResponse,
    RoleRead,
    RoleUpdate,
)
from app.auth.dependencies import require_superuser
from app.db.session import get_db
from app.rbac.models import Group, Permission, Role, group_roles, role_permissions
from app.rbac.permissions import ensure_initial_permissions, require_permission

permissions_router = APIRouter(
    prefix="/admin/permissions",
    tags=["admin-permissions"],
)
roles_router = APIRouter(
    prefix="/admin/roles",
    tags=["admin-roles"],
    dependencies=[Depends(require_permission("roles.manage"))],
)
group_roles_router = APIRouter(
    prefix="/admin/groups",
    tags=["admin-rbac"],
    dependencies=[Depends(require_permission("roles.manage"))],
)


@permissions_router.get(
    "",
    response_model=list[PermissionRead],
    dependencies=[Depends(require_permission("roles.manage"))],
)
def list_permissions(db: Annotated[Session, Depends(get_db)]) -> list[Permission]:
    return list(db.scalars(select(Permission).order_by(Permission.code)))


@permissions_router.post(
    "/bootstrap",
    response_model=PermissionBootstrapResponse,
    dependencies=[Depends(require_superuser)],
)
def bootstrap_permissions(
    db: Annotated[Session, Depends(get_db)],
) -> PermissionBootstrapResponse:
    created_codes = ensure_initial_permissions(db)
    permissions = list(db.scalars(select(Permission).order_by(Permission.code)))

    return PermissionBootstrapResponse(
        created_codes=created_codes,
        permissions=permissions,
        detail="Base permissions initialized",
    )


@roles_router.get("", response_model=list[RoleRead])
def list_roles(db: Annotated[Session, Depends(get_db)]) -> list[Role]:
    return list(
        db.scalars(
            select(Role).options(selectinload(Role.permissions)).order_by(Role.id)
        )
    )


@roles_router.post("", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    db: Annotated[Session, Depends(get_db)],
) -> Role:
    role = Role(name=payload.name, description=payload.description)
    db.add(role)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role already exists",
        ) from None

    return get_role_with_permissions(db, role.id)


@roles_router.patch("/{role_id}", response_model=RoleRead)
def update_role(
    role_id: int,
    payload: RoleUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> Role:
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(role, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role already exists",
        ) from None

    return get_role_with_permissions(db, role_id)


@roles_router.delete("/{role_id}", response_model=RoleDeleteResponse)
def delete_role(
    role_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> RoleDeleteResponse:
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )

    db.execute(delete(group_roles).where(group_roles.c.role_id == role.id))
    db.execute(delete(role_permissions).where(role_permissions.c.role_id == role.id))
    db.delete(role)
    db.commit()

    return RoleDeleteResponse(role_id=role_id, detail="Role deleted")


@roles_router.post(
    "/{role_id}/permissions/{permission_id}",
    response_model=RolePermissionResponse,
    status_code=status.HTTP_200_OK,
)
def assign_permission_to_role(
    role_id: int,
    permission_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> RolePermissionResponse:
    ensure_role_and_permission_exist(
        db,
        role_id=role_id,
        permission_id=permission_id,
    )

    exists = db.execute(
        select(role_permissions).where(
            role_permissions.c.role_id == role_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).first()
    if exists is None:
        db.execute(
            insert(role_permissions).values(
                role_id=role_id,
                permission_id=permission_id,
            )
        )
        db.commit()

    return RolePermissionResponse(
        role_id=role_id,
        permission_id=permission_id,
        detail="Permission is assigned to role",
    )


@roles_router.delete(
    "/{role_id}/permissions/{permission_id}",
    response_model=RolePermissionResponse,
)
def remove_permission_from_role(
    role_id: int,
    permission_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> RolePermissionResponse:
    ensure_role_and_permission_exist(
        db,
        role_id=role_id,
        permission_id=permission_id,
    )

    db.execute(
        delete(role_permissions).where(
            role_permissions.c.role_id == role_id,
            role_permissions.c.permission_id == permission_id,
        )
    )
    db.commit()

    return RolePermissionResponse(
        role_id=role_id,
        permission_id=permission_id,
        detail="Permission is not assigned to role",
    )


@group_roles_router.post(
    "/{group_id}/roles/{role_id}",
    response_model=GroupRoleResponse,
    status_code=status.HTTP_200_OK,
)
def assign_role_to_group(
    group_id: int,
    role_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> GroupRoleResponse:
    ensure_group_and_role_exist(db, group_id=group_id, role_id=role_id)

    exists = db.execute(
        select(group_roles).where(
            group_roles.c.group_id == group_id,
            group_roles.c.role_id == role_id,
        )
    ).first()
    if exists is None:
        db.execute(insert(group_roles).values(group_id=group_id, role_id=role_id))
        db.commit()

    return GroupRoleResponse(
        group_id=group_id,
        role_id=role_id,
        detail="Role is assigned to group",
    )


@group_roles_router.delete(
    "/{group_id}/roles/{role_id}",
    response_model=GroupRoleResponse,
)
def remove_role_from_group(
    group_id: int,
    role_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> GroupRoleResponse:
    ensure_group_and_role_exist(db, group_id=group_id, role_id=role_id)

    db.execute(
        delete(group_roles).where(
            group_roles.c.group_id == group_id,
            group_roles.c.role_id == role_id,
        )
    )
    db.commit()

    return GroupRoleResponse(
        group_id=group_id,
        role_id=role_id,
        detail="Role is not assigned to group",
    )


def get_role_with_permissions(db: Session, role_id: int) -> Role:
    role = db.scalar(
        select(Role)
        .options(selectinload(Role.permissions))
        .where(Role.id == role_id)
    )
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )

    return role


def ensure_role_and_permission_exist(
    db: Session,
    *,
    role_id: int,
    permission_id: int,
) -> None:
    if db.get(Role, role_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )
    if db.get(Permission, permission_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found",
        )


def ensure_group_and_role_exist(
    db: Session,
    *,
    group_id: int,
    role_id: int,
) -> None:
    if db.get(Group, group_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Group not found",
        )
    if db.get(Role, role_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found",
        )
