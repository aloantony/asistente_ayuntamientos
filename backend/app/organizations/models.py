from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.municipalities.models import Municipality  # noqa: F401

if TYPE_CHECKING:
    from app.projects.models import Project
    from app.rbac.models import Group
    from app.users.models import User

organization_users = Table(
    "organization_users",
    Base.metadata,
    Column(
        "organization_id",
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_organization_users_user_id", "user_id"),
)


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'paused', 'archived')",
            name="ck_organizations_status",
        ),
        UniqueConstraint(
            "id",
            "municipality_id",
            name="uq_organizations_id_municipality",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    municipality_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )

    users: Mapped[list["User"]] = relationship(
        "User",
        secondary=organization_users,
        back_populates="organizations",
    )
    groups: Mapped[list["Group"]] = relationship(
        "Group",
        back_populates="organization",
    )
    municipality: Mapped["Municipality | None"] = relationship(
        "Municipality",
        back_populates="organizations",
    )
    projects: Mapped[list["Project"]] = relationship(
        "Project",
        back_populates="organization",
    )
