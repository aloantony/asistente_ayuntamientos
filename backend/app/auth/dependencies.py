from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db
from app.users.models import User

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
    access_token_cookie: Annotated[
        str | None,
        Cookie(alias="access_token"),
    ] = None,
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Browser sessions use the httpOnly cookie; API clients and tests keep
    # using Authorization: Bearer, which takes precedence when present.
    token: str | None = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    elif access_token_cookie:
        token = access_token_cookie

    if token is None:
        raise credentials_error

    try:
        payload = decode_access_token(token)
        subject = payload.get("sub")
        if not isinstance(subject, str):
            raise credentials_error
        user_id = int(subject)
    except (InvalidTokenError, ValueError):
        raise credentials_error from None
    issued_at = payload.get("iat")

    user = db.get(User, user_id)
    if user is None:
        raise credentials_error
    # Tokens issued before the last password change/reset are revoked; tokens
    # predating the iat claim carry no claim and count as issued in the past.
    if user.password_changed_at is not None and (
        not isinstance(issued_at, (int, float))
        or issued_at < user.password_changed_at.timestamp()
    ):
        raise credentials_error
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return user


def require_superuser(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superuser privileges required",
        )
    return current_user
