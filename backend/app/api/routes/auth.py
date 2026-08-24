import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.auth.schemas import (
    BootstrapAdminRequest,
    ChangePasswordRequest,
    DetailResponse,
    LoginRequest,
    Token,
)
from app.core.config import settings
from app.core.rate_limit import (
    RateLimiter,
    RateLimitUnavailable,
    change_password_rate_limiter,
    login_rate_limiter,
)
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.organizations.access import get_accessible_organizations_query
from app.organizations.models import Organization
from app.rbac.permissions import get_user_permission_codes
from app.users.crud import count_users, create_user, get_user_by_email
from app.users.models import User
from app.users.schemas import (
    SidebarShortcutsRead,
    SidebarShortcutsUpdate,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["auth"])

ACCESS_TOKEN_COOKIE = "access_token"


def acquire_rate_limit_slot(limiter: RateLimiter, key: str) -> str | None:
    try:
        return limiter.try_acquire(key)
    except RateLimitUnavailable:
        # Fail closed: authenticating without a checkable budget would hand
        # an attacker unlimited attempts. The error carries no Redis URL or
        # logical key, so an outage cannot leak them to the client.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication temporarily unavailable",
        ) from None


def refund_rate_limit_slot(
    limiter: RateLimiter,
    key: str,
    reservation_id: str,
) -> None:
    try:
        limiter.refund(key, reservation_id)
    except RateLimitUnavailable:
        # The guarded check already succeeded; the reservation expires with
        # the window on its own, so a failed refund must not fail the request.
        return


def set_session_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=access_token,
        httponly=True,
        samesite="lax",
        secure=settings.environment not in ("development", "test"),
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


@router.post("/login", response_model=Token)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> Token:
    # The key combines client and account: behind docker-proxy (or a future
    # reverse proxy) every browser shares one IP, and an IP-only key would
    # turn the limit into a global budget locking the whole organization out
    # of the login. The slot is reserved atomically before the slow password
    # check (so concurrent requests cannot exceed the budget) and refunded on
    # success: only failed attempts end up consuming quota.
    client_host = request.client.host if request.client else "unknown"
    rate_key = f"{client_host}:{str(payload.email).lower()}"
    reservation_id = acquire_rate_limit_slot(login_rate_limiter, rate_key)
    if reservation_id is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
        )

    user = get_user_by_email(db, str(payload.email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    refund_rate_limit_slot(login_rate_limiter, rate_key, reservation_id)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )

    access_token = create_access_token(subject=str(user.id))
    # httpOnly session cookie for the browser; the token is also returned in
    # the body for API clients and tests using Authorization: Bearer.
    set_session_cookie(response, access_token)
    return Token(access_token=access_token)


@router.post("/logout", response_model=DetailResponse)
def logout(
    response: Response,
    _current_user: Annotated[User, Depends(get_current_user)],
) -> DetailResponse:
    # Requiring a session prevents a cross-site form from logging the victim
    # out (the deleting Set-Cookie would apply in a first-party context).
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")
    return DetailResponse(detail="Logged out")


@router.post("/change-password", response_model=DetailResponse)
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DetailResponse:
    # Verifying the current password is the defense against a hijacked
    # session; without an attempt limit it would be brute-forceable. The slot
    # is reserved atomically and refunded only when the check passes.
    rate_key = str(current_user.id)
    reservation_id = acquire_rate_limit_slot(change_password_rate_limiter, rate_key)
    if reservation_id is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many password attempts",
        )
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    refund_rate_limit_slot(change_password_rate_limiter, rate_key, reservation_id)

    current_user.hashed_password = hash_password(payload.new_password)
    current_user.password_changed_at = datetime.now(UTC)
    db.commit()
    # Previously issued tokens are now revoked; refresh the cookie so the
    # user's own session stays alive.
    set_session_cookie(response, create_access_token(subject=str(current_user.id)))
    return DetailResponse(detail="Password updated")


@router.get("/me", response_model=UserRead)
def read_me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UserRead:
    organizations = list(
        db.scalars(
            get_accessible_organizations_query(current_user).options(
                selectinload(Organization.municipality)
            )
        )
    )
    return UserRead.model_validate(current_user).model_copy(
        update={
            "permissions": sorted(get_user_permission_codes(current_user, db)),
            "organizations": organizations,
        }
    )


@router.put("/me/sidebar-shortcuts", response_model=SidebarShortcutsRead)
def update_sidebar_shortcuts(
    payload: SidebarShortcutsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SidebarShortcutsRead:
    current_user.sidebar_shortcut_ids = list(payload.shortcut_ids)
    db.commit()
    db.refresh(current_user)
    return SidebarShortcutsRead(shortcut_ids=current_user.sidebar_shortcut_ids)


@router.delete("/me/sidebar-shortcuts", response_model=SidebarShortcutsRead)
def reset_sidebar_shortcuts(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SidebarShortcutsRead:
    current_user.sidebar_shortcut_ids = None
    db.commit()
    return SidebarShortcutsRead(shortcut_ids=None)


@router.post(
    "/bootstrap-admin",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
def bootstrap_admin(
    payload: BootstrapAdminRequest,
    db: Annotated[Session, Depends(get_db)],
    bootstrap_token: Annotated[
        str | None,
        Header(alias="X-Bootstrap-Admin-Token"),
    ] = None,
) -> User:
    if not settings.bootstrap_admin_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bootstrap admin token is not configured",
        )
    if not secrets.compare_digest(
        bootstrap_token or "",
        settings.bootstrap_admin_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bootstrap admin token",
        )
    if count_users(db) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bootstrap admin can only be created before any users exist",
        )

    try:
        return create_user(
            db,
            email=str(payload.email),
            password=payload.password,
            full_name=payload.full_name,
            is_active=True,
            is_superuser=True,
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already exists",
        ) from None
