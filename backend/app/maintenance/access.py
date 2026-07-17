from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.assets.access import require_asset_permission
from app.rbac.permissions import has_permission
from app.users.models import User


def has_maintenance_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> bool:
    return has_permission(
        current_user,
        "maintenance.manage",
        db,
        organization_id=organization_id,
    ) or has_permission(
        current_user,
        permission_code,
        db,
        organization_id=organization_id,
    )


def require_maintenance_permission(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> None:
    if has_maintenance_permission(
        db,
        current_user,
        organization_id,
        permission_code,
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission required: {permission_code}",
    )


def require_maintenance_response_permissions(
    db: Session,
    current_user: User,
    organization_id: int,
    permission_code: str,
) -> None:
    require_maintenance_permission(
        db,
        current_user,
        organization_id,
        permission_code,
    )
    require_asset_permission(
        db,
        current_user,
        organization_id,
        "assets.view",
    )


def require_maintenance_manager(
    db: Session,
    current_user: User,
    organization_id: int,
) -> None:
    if has_permission(
        current_user,
        "maintenance.manage",
        db,
        organization_id=organization_id,
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission required: maintenance.manage",
    )
