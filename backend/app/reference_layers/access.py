from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.geo.access import (
    has_any_map_view_permission,
    has_map_view_permission,
)
from app.organizations.models import Organization
from app.rbac.permissions import has_permission
from app.users.models import User


def require_catalog_view(
    db: Session,
    current_user: User,
    organization_id: int | None,
) -> Organization | None:
    if organization_id is None:
        if has_any_map_view_permission(db, current_user):
            return None
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: map.view",
        )

    organization = require_organization(db, organization_id)
    if has_map_view_permission(db, current_user, organization_id):
        return organization
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission required: map.view",
    )


def require_catalog_manage(
    db: Session,
    current_user: User,
    organization_id: int,
) -> Organization:
    organization = require_organization(db, organization_id)
    if has_permission(
        current_user,
        "map.manage",
        db,
        organization_id=organization_id,
    ):
        return organization
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission required: map.manage",
    )


def require_organization(db: Session, organization_id: int) -> Organization:
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return organization
