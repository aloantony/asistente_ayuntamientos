import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.invitations.models import OrganizationInvitation
from app.organizations.models import Organization, organization_users
from app.users.models import User

INVITATION_UNAVAILABLE = "Invitation is not available"
INVITATION_ALREADY_PENDING = "Invitation already pending"
INVITATION_IDENTITY_MISMATCH = "Invitation does not match authenticated user"
INVITATION_PENDING_LIMIT = "Organization invitation limit reached"
TOKEN_GENERATION_FAILED = "Could not create invitation"
TOKEN_BYTES = 32
TOKEN_GENERATION_ATTEMPTS = 5


def invitation_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_invitation(
    db: Session,
    *,
    organization_id: int,
    email: str,
    invited_by_user_id: int,
) -> tuple[OrganizationInvitation, str]:
    now = datetime.now(UTC)
    normalized_email = email.strip().lower()
    organization = db.scalar(
        select(Organization)
        .where(Organization.id == organization_id)
        .with_for_update()
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    pending_count = db.scalar(
        select(func.count(OrganizationInvitation.id)).where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
    ) or 0
    if pending_count >= settings.organization_invitation_max_pending_per_organization:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=INVITATION_PENDING_LIMIT,
        )
    existing = db.scalar(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.email == normalized_email,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.expires_at > now:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=INVITATION_ALREADY_PENDING,
            )
        existing.revoked_at = now
        db.flush()

    expires_at = now + timedelta(hours=settings.organization_invitation_expire_hours)
    for _attempt in range(TOKEN_GENERATION_ATTEMPTS):
        token = secrets.token_urlsafe(TOKEN_BYTES)
        invitation = OrganizationInvitation(
            organization_id=organization_id,
            email=normalized_email,
            token_hash=invitation_token_hash(token),
            expires_at=expires_at,
            invited_by_user_id=invited_by_user_id,
        )
        try:
            with db.begin_nested():
                db.add(invitation)
                db.flush()
        except IntegrityError:
            # A concurrent request for the same tenant/email wins uniformly;
            # a token-hash collision is retried without exposing its value.
            pending = db.scalar(
                select(OrganizationInvitation.id).where(
                    OrganizationInvitation.organization_id == organization_id,
                    OrganizationInvitation.email == normalized_email,
                    OrganizationInvitation.accepted_at.is_(None),
                    OrganizationInvitation.revoked_at.is_(None),
                )
            )
            if pending is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=INVITATION_ALREADY_PENDING,
                ) from None
            continue

        db.commit()
        db.refresh(invitation)
        return invitation, token

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=TOKEN_GENERATION_FAILED,
    )


def get_available_invitation(
    db: Session,
    token: str,
    *,
    lock: bool = False,
) -> OrganizationInvitation:
    query = (
        select(OrganizationInvitation)
        .options(selectinload(OrganizationInvitation.organization))
        .where(OrganizationInvitation.token_hash == invitation_token_hash(token))
    )
    if lock:
        query = query.with_for_update()

    invitation = db.scalar(query)
    now = datetime.now(UTC)
    if (
        invitation is None
        or invitation.accepted_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=INVITATION_UNAVAILABLE,
        )
    return invitation


def accept_invitation(
    db: Session,
    *,
    token: str,
    current_user: User,
) -> OrganizationInvitation:
    invitation = get_available_invitation(db, token, lock=True)
    if current_user.email.strip().lower() != invitation.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INVITATION_IDENTITY_MISMATCH,
        )

    db.execute(
        insert(organization_users)
        .values(
            organization_id=invitation.organization_id,
            user_id=current_user.id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                organization_users.c.organization_id,
                organization_users.c.user_id,
            ]
        )
    )
    invitation.accepted_at = datetime.now(UTC)
    invitation.accepted_by_user_id = current_user.id
    db.commit()
    db.refresh(invitation)
    return invitation
