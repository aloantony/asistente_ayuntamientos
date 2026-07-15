from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.organizations.models import Organization
from app.rbac.permissions import has_permission
from app.users.models import User


def require_asset_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> None:
    if has_permission(
        current_user,
        "assets.manage",
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


def get_asset_organization_for_read(
    db: Session,
    organization_id: int,
) -> Organization:
    organization = get_existing_organization(db, organization_id)
    if organization.status not in {"active", "paused"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization is not available for asset inventory reads",
        )
    return organization


def get_asset_organization_for_write(
    db: Session,
    organization_id: int,
) -> Organization:
    organization = get_existing_organization(db, organization_id)
    if organization.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization must be active to modify asset inventory",
        )
    if organization.municipality_id is None or organization.municipality is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization must have a municipality to modify asset inventory",
        )
    if organization.municipality.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization municipality must be active to modify asset inventory",
        )
    return organization


def get_existing_organization(db: Session, organization_id: int) -> Organization:
    organization = db.scalar(
        select(Organization)
        .options(selectinload(Organization.municipality))
        .where(Organization.id == organization_id)
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return organization
