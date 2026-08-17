from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.organizations.models import Organization
from app.rbac.permissions import has_permission
from app.users.models import User


def require_staff_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> None:
    if has_permission(
        current_user,
        "staff.manage",
        db,
        organization_id=organization_id,
    ) or has_permission(
        current_user,
        permission_code,
        db,
        organization_id=organization_id,
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )


def get_staff_organization_for_read(
    db: Session,
    organization_id: int,
) -> Organization:
    organization = get_existing_organization(db, organization_id)
    if organization.status not in {"active", "paused"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization is not available for staff reads",
        )
    return organization


def get_staff_organization_for_write(
    db: Session,
    organization_id: int,
) -> Organization:
    # La plantilla no exige municipio vinculado, a diferencia del inventario:
    # es información laboral de la organización, no del territorio.
    organization = get_existing_organization(db, organization_id)
    if organization.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization must be active to modify staff records",
        )
    return organization


def get_existing_organization(db: Session, organization_id: int) -> Organization:
    organization = db.scalar(
        select(Organization).where(Organization.id == organization_id)
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return organization
