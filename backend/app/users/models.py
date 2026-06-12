from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.organizations.models import organization_users
from app.rbac.models import user_groups


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    # Revocation marker: JWTs with an iat older than this are rejected.
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    groups: Mapped[list["Group"]] = relationship(
        "Group",
        secondary=user_groups,
        back_populates="users",
    )
    organizations: Mapped[list["Organization"]] = relationship(
        "Organization",
        secondary=organization_users,
        back_populates="users",
    )
