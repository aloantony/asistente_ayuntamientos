from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.users.models import User


def count_users(db: Session) -> int:
    return db.scalar(select(func.count(User.id))) or 0


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.lower()))


def create_user(
    db: Session,
    *,
    email: str,
    password: str,
    full_name: str,
    is_active: bool = True,
    is_superuser: bool = False,
) -> User:
    user = User(
        email=email.lower(),
        hashed_password=hash_password(password),
        full_name=full_name,
        is_active=is_active,
        is_superuser=is_superuser,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
