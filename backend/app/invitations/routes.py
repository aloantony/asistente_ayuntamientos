from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.invitations.models import OrganizationInvitation
from app.invitations.schemas import (
    InvitationAccepted,
    InvitationAcceptRequest,
    InvitationAdminRead,
    InvitationCreate,
    InvitationCreated,
    InvitationPreview,
    InvitationPreviewRequest,
    InvitationRevoked,
)
from app.invitations.service import (
    accept_invitation,
    create_invitation,
    get_available_invitation,
)
from app.organizations.models import Organization
from app.rbac.permissions import has_permission
from app.users.models import User

admin_router = APIRouter(tags=["organization-invitations"])
public_router = APIRouter(prefix="/auth/invitations", tags=["auth-invitations"])


@admin_router.get(
    "/organizations/{organization_id}/invitations",
    response_model=list[InvitationAdminRead],
)
def list_invitations(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[OrganizationInvitation]:
    require_invitation_management(db, current_user, organization_id)
    ensure_organization_exists(db, organization_id)
    return list(
        db.scalars(
            select(OrganizationInvitation)
            .where(OrganizationInvitation.organization_id == organization_id)
            .order_by(OrganizationInvitation.id.desc())
        )
    )


@admin_router.post(
    "/organizations/{organization_id}/invitations",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
def invite_user(
    organization_id: int,
    payload: InvitationCreate,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> InvitationCreated:
    require_invitation_management(db, current_user, organization_id)
    ensure_organization_exists(db, organization_id)
    invitation, token = create_invitation(
        db,
        organization_id=organization_id,
        email=str(payload.email),
        invited_by_user_id=current_user.id,
    )
    response.headers["Cache-Control"] = "no-store"
    invitation_data = InvitationAdminRead.model_validate(invitation).model_dump()
    return InvitationCreated(**invitation_data, token=token)


@admin_router.delete(
    "/organizations/{organization_id}/invitations/{invitation_id}",
    response_model=InvitationRevoked,
)
def revoke_invitation(
    organization_id: int,
    invitation_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> InvitationRevoked:
    require_invitation_management(db, current_user, organization_id)
    ensure_organization_exists(db, organization_id)
    invitation = db.scalar(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.organization_id == organization_id,
        )
        .with_for_update()
    )
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )
    if invitation.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Accepted invitation cannot be revoked",
        )
    if invitation.revoked_at is None:
        invitation.revoked_at = datetime.now(UTC)
        db.commit()
    return InvitationRevoked(
        invitation_id=invitation.id,
        detail="Invitation revoked",
    )


@public_router.post("/preview", response_model=InvitationPreview)
def preview_invitation(
    payload: InvitationPreviewRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> InvitationPreview:
    invitation = get_available_invitation(db, payload.token)
    requires_registration = db.scalar(
        select(User.id).where(User.email == invitation.email)
    ) is None
    response.headers["Cache-Control"] = "no-store"
    return InvitationPreview(
        email=invitation.email,
        organization_name=invitation.organization.name,
        expires_at=invitation.expires_at,
        requires_registration=requires_registration,
    )


@public_router.post("/accept", response_model=InvitationAccepted)
def accept_organization_invitation(
    payload: InvitationAcceptRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> InvitationAccepted:
    invitation, _user = accept_invitation(db, payload)
    response.headers["Cache-Control"] = "no-store"
    return InvitationAccepted(
        detail="Invitation accepted",
        organization_id=invitation.organization_id,
        organization_name=invitation.organization.name,
    )


def require_invitation_management(
    db: Session,
    current_user: User,
    organization_id: int,
) -> None:
    if has_permission(
        current_user,
        "users.manage",
        db,
        organization_id=organization_id,
    ):
        return
    # The same response is used for nonexistent and out-of-scope tenants so
    # their ids cannot be enumerated through this administrative surface.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Permission required: users.manage",
    )


def ensure_organization_exists(db: Session, organization_id: int) -> Organization:
    organization = db.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return organization
