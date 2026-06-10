from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.rbac.models import Group
    from app.users.models import User

project_users = Table(
    "project_users",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_project_users_user_id", "user_id"),
)

project_groups = Table(
    "project_groups",
    Base.metadata,
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_project_groups_group_id", "group_id"),
)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'paused', 'completed', 'archived')",
            name="ck_projects_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship(
        "Organization",
        back_populates="projects",
    )
    users: Mapped[list["User"]] = relationship(
        "User",
        secondary=project_users,
    )
    groups: Mapped[list["Group"]] = relationship(
        "Group",
        secondary=project_groups,
    )
