from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.schemas import BootstrapAdminRequest, LoginRequest, Token
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.rbac.permissions import get_user_permission_codes
from app.users.crud import count_users, create_user, get_user_by_email
from app.users.models import User
from app.users.schemas import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(payload: LoginRequest, db: Annotated[Session, Depends(get_db)]) -> Token:
    user = get_user_by_email(db, str(payload.email))
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )

    access_token = create_access_token(subject=str(user.id))
    return Token(access_token=access_token)


@router.get("/me", response_model=UserRead)
def read_me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UserRead:
    return UserRead.model_validate(current_user).model_copy(
        update={
            "permissions": sorted(get_user_permission_codes(current_user, db)),
        }
    )


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
    if bootstrap_token != settings.bootstrap_admin_token:
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
