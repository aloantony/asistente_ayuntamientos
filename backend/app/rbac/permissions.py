from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.rbac.models import Permission, group_roles, role_permissions, user_groups
from app.users.models import User

INITIAL_PERMISSION_DEFINITIONS: dict[str, str] = {
    "users.manage": "Gestionar usuarios",
    "groups.manage": "Gestionar grupos",
    "roles.manage": "Gestionar roles y permisos",
    "projects.create": "Crear proyectos",
    "projects.edit": "Editar proyectos",
    "projects.archive": "Archivar proyectos",
    "projects.manage_members": "Gestionar miembros de proyectos",
    "projects.view_all": "Ver todos los proyectos",
}

INITIAL_PERMISSION_CODES = tuple(INITIAL_PERMISSION_DEFINITIONS.keys())


def get_user_permission_codes(user: User, db: Session) -> set[str]:
    return set(
        db.scalars(
            select(Permission.code)
            .join(
                role_permissions,
                Permission.id == role_permissions.c.permission_id,
            )
            .join(
                group_roles,
                role_permissions.c.role_id == group_roles.c.role_id,
            )
            .join(
                user_groups,
                group_roles.c.group_id == user_groups.c.group_id,
            )
            .where(user_groups.c.user_id == user.id)
            .distinct()
        )
    )


def has_permission(user: User, code: str, db: Session) -> bool:
    if user.is_superuser:
        return True

    return code in get_user_permission_codes(user, db)


def require_permission(code: str) -> Callable[..., User]:
    def dependency(
        current_user: Annotated[User, Depends(get_current_user)],
        db: Annotated[Session, Depends(get_db)],
    ) -> User:
        if not has_permission(current_user, code, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {code}",
            )

        return current_user

    return dependency


def ensure_initial_permissions(db: Session) -> list[str]:
    existing_codes = set(
        db.scalars(
            select(Permission.code).where(
                Permission.code.in_(INITIAL_PERMISSION_CODES),
            )
        )
    )
    created_codes: list[str] = []

    for code in INITIAL_PERMISSION_CODES:
        if code in existing_codes:
            continue

        db.add(
            Permission(
                code=code,
                description=INITIAL_PERMISSION_DEFINITIONS[code],
            )
        )
        created_codes.append(code)

    if created_codes:
        db.commit()

    return created_codes
