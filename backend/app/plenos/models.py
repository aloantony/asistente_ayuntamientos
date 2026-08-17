from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.documents.models import Document
    from app.organizations.models import Organization


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


SESSION_KINDS = ("ordinary", "extraordinary", "urgent", "constitutive")

SESSION_STATUSES = ("convened", "held", "cancelled")

# El acta se aprueba en la sesión siguiente; hasta entonces es un borrador.
MINUTES_STATUSES = ("pending", "draft", "approved")


class CouncilSession(TimestampMixin, Base):
    """Sesión del pleno municipal, con su convocatoria y su acta.

    El acta no se guarda como texto suelto: es un documento de `documents`,
    con su control de acceso y su checksum, igual que el escudo en ADR-038.
    """

    __tablename__ = "council_sessions"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(SESSION_KINDS)})",
            name="ck_council_sessions_kind",
        ),
        CheckConstraint(
            f"status in ({_sql_in(SESSION_STATUSES)})",
            name="ck_council_sessions_status",
        ),
        CheckConstraint(
            f"minutes_status in ({_sql_in(MINUTES_STATUSES)})",
            name="ck_council_sessions_minutes_status",
        ),
        # Un acta aprobada tiene documento; sin él no hay nada que aprobar.
        CheckConstraint(
            "minutes_status <> 'approved' or minutes_document_id is not null",
            name="ck_council_sessions_minutes_document",
        ),
        # Una sesión cancelada no celebra nada, así que no produce acta.
        CheckConstraint(
            "status <> 'cancelled' or minutes_status = 'pending'",
            name="ck_council_sessions_cancelled_minutes",
        ),
        UniqueConstraint(
            "organization_id",
            "held_on",
            "kind",
            name="uq_council_sessions_org_date_kind",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_council_sessions_id_org",
        ),
        Index(
            "ix_council_sessions_org_date",
            "organization_id",
            "held_on",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        String(20),
        default="ordinary",
        server_default="ordinary",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="convened",
        server_default="convened",
        nullable=False,
    )
    held_on: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    minutes_status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    minutes_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    publish_to_sede: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    minutes_document: Mapped["Document | None"] = relationship("Document")
    agenda_items: Mapped[list["CouncilAgendaItem"]] = relationship(
        "CouncilAgendaItem",
        back_populates="session",
        order_by="CouncilAgendaItem.position",
        viewonly=True,
    )


class CouncilAgendaItem(TimestampMixin, Base):
    """Punto del orden del día, con el resultado de su votación."""

    __tablename__ = "council_agenda_items"
    __table_args__ = (
        CheckConstraint(
            "position >= 1",
            name="ck_council_agenda_items_position",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_council_agenda_items_title",
        ),
        CheckConstraint(
            "votes_in_favour is null or votes_in_favour >= 0",
            name="ck_council_agenda_items_votes_favour",
        ),
        CheckConstraint(
            "votes_against is null or votes_against >= 0",
            name="ck_council_agenda_items_votes_against",
        ),
        CheckConstraint(
            "abstentions is null or abstentions >= 0",
            name="ck_council_agenda_items_abstentions",
        ),
        ForeignKeyConstraint(
            ["session_id", "organization_id"],
            ["council_sessions.id", "council_sessions.organization_id"],
            name="fk_council_agenda_items_session_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "session_id",
            "position",
            name="uq_council_agenda_items_session_position",
        ),
        Index(
            "ix_council_agenda_items_session",
            "session_id",
            "position",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nulos mientras no se vota: un punto informativo no tiene votación, y cero
    # votos a favor no es lo mismo que no haberse votado.
    votes_in_favour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    votes_against: Mapped[int | None] = mapped_column(Integer, nullable=True)
    abstentions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(120), nullable=True)

    session: Mapped["CouncilSession"] = relationship(
        "CouncilSession",
        back_populates="agenda_items",
        viewonly=True,
    )
