import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.security import hash_password
from app.invitations.models import OrganizationInvitation
from app.invitations.schemas import InvitationAcceptRequest
from app.organizations.models import organization_users
from app.users.models import User

INVITATION_UNAVAILABLE = "Invitation is not available"
INVITATION_ALREADY_PENDING = "Invitation already pending"
REGISTRATION_DETAILS_REQUIRED = "Registration details required"
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
    payload: InvitationAcceptRequest,
) -> tuple[OrganizationInvitation, User]:
    invitation = get_available_invitation(db, payload.token, lock=True)
    user = db.scalar(
        select(User).where(User.email == invitation.email).with_for_update()
    )

    if user is None:
        if payload.full_name is None or payload.password is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=REGISTRATION_DETAILS_REQUIRED,
            )

        candidate = User(
            email=invitation.email,
            hashed_password=hash_password(payload.password),
            full_name=payload.full_name,
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
        except IntegrityError:
            # Two invitations for the same address may be accepted in
            # parallel. The global identity is unique; the loser links it.
            user = db.scalar(
                select(User).where(User.email == invitation.email).with_for_update()
            )
            if user is None:
                raise
        else:
            user = candidate

    db.execute(
        insert(organization_users)
        .values(
            organization_id=invitation.organization_id,
            user_id=user.id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                organization_users.c.organization_id,
                organization_users.c.user_id,
            ]
        )
    )
    invitation.accepted_at = datetime.now(UTC)
    invitation.accepted_by_user_id = user.id
    db.commit()
    db.refresh(invitation)
    return invitation, user
