from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.organizations.models import Organization, organization_users
from app.rbac.permissions import has_permission
from app.users.models import User

VIEW_CODES = ("town_hall.view", "town_hall.manage")
EDIT_CODES = ("town_hall.edit", "town_hall.manage")


def _has_any_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    codes: tuple[str, ...],
) -> bool:
    if current_user.is_superuser:
        return True
    return any(
        has_permission(current_user, code, db, organization_id=organization_id)
        for code in codes
    )


def resolve_organization(
    db: Session,
    current_user: User,
    organization_id: int | None,
) -> Organization:
    """Resuelve la organización cuyo Ayuntamiento se está consultando.

    Sin `organization_id` explícito se usa la primera organización del usuario,
    la misma convención que aplica el sidebar del frontend. El superusuario no
    tiene organización propia, así que debe indicarla siempre.
    """
    if organization_id is not None:
        organization = db.get(Organization, organization_id)
        if organization is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )
        return organization

    if current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id is required",
        )

    organization = db.scalar(
        select(Organization)
        .join(
            organization_users,
            organization_users.c.organization_id == Organization.id,
        )
        .where(organization_users.c.user_id == current_user.id)
        .order_by(Organization.id)
        .limit(1)
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User has no organization",
        )
    return organization


def require_town_hall_view(
    db: Session,
    current_user: User,
    organization_id: int,
) -> None:
    if not _has_any_permission(db, current_user, organization_id, VIEW_CODES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: town_hall.view",
        )


def require_town_hall_edit(
    db: Session,
    current_user: User,
    organization_id: int,
) -> None:
    if not _has_any_permission(db, current_user, organization_id, EDIT_CODES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: town_hall.edit",
        )
