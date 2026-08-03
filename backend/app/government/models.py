from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User


# Niveles de la corporación municipal. El orden de la tupla es el orden
# protocolario en que se presenta la estructura de gobierno.
GOVERNMENT_LEVELS = (
    "alcaldia",
    "tenencia",
    "concejalia",
    "secretaria",
)

GOVERNMENT_MEMBER_STATUSES = ("active", "archived")


class GovernmentMember(TimestampMixin, Base):
    """Cargo de la corporación municipal.

    Describe quién ocupa cada puesto electo o de habilitación nacional, no la
    plantilla laboral: el personal del ayuntamiento vive en `staff_workers`.
    Un cargo vacante se archiva en lugar de borrarse, para que las actas y los
    acuerdos antiguos sigan siendo interpretables.
    """

    __tablename__ = "government_members"
    __table_args__ = (
        CheckConstraint(
            "level in ('" + "', '".join(GOVERNMENT_LEVELS) + "')",
            name="ck_government_members_level",
        ),
        CheckConstraint(
            "status in ('" + "', '".join(GOVERNMENT_MEMBER_STATUSES) + "')",
            name="ck_government_members_status",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_government_members_sort_order",
        ),
        CheckConstraint(
            "term_end_date is null"
            " or term_start_date is null"
            " or term_end_date >= term_start_date",
            name="ck_government_members_term_range",
        ),
        Index(
            "ix_government_members_org_status_sort",
            "organization_id",
            "status",
            "sort_order",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    level: Mapped[str] = mapped_column(String(30), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Denominación literal del cargo ("Concejal de Urbanismo"); el nivel sólo
    # dice de qué tipo es.
    role_title: Mapped[str] = mapped_column(String(255), nullable=False)
    political_group: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    biography: Mapped[str | None] = mapped_column(Text, nullable=True)
    term_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    term_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
