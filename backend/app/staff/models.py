from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# Un puesto ("Secretario") frente a un contenedor que agrupa puestos
# ("Servicios técnicos"). El contenedor no se puede ocupar.
STAFF_POST_KINDS = ("post", "container")

STAFF_WORKER_STATUSES = ("active", "vacation", "leave", "archived")

STAFF_ABSENCE_TYPES = ("vacation", "personal", "sick_leave", "other")

# El diario es la nota breve del día; el reporte, el parte con más detalle.
STAFF_REPORT_TYPES = ("diary", "report")

STAFF_CONTRACT_TYPES = ("permanent", "temporary", "interim", "external", "other")

STAFF_HISTORY_EVENT_TYPES = (
    "created",
    "updated",
    "status_changed",
    "post_changed",
    "archived",
)


class StaffPost(TimestampMixin, Base):
    """Puesto de la plantilla municipal, o contenedor que agrupa puestos.

    La plantilla se modela aparte de la persona porque el puesto sobrevive a
    quien lo ocupa: un ayuntamiento pequeño puede tener el puesto de arquitecto
    cubierto a tiempo parcial, vacante, o compartido con otro municipio.
    """

    __tablename__ = "staff_posts"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(STAFF_POST_KINDS)})",
            name="ck_staff_posts_kind",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_staff_posts_sort_order",
        ),
        CheckConstraint(
            "parent_id is null or parent_id <> id",
            name="ck_staff_posts_parent_not_self",
        ),
        ForeignKeyConstraint(
            ["parent_id", "organization_id"],
            ["staff_posts.id", "staff_posts.organization_id"],
            name="fk_staff_posts_parent_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_staff_posts_id_org",
        ),
        Index(
            "ix_staff_posts_org_sort",
            "organization_id",
            "sort_order",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(
        String(20),
        default="post",
        server_default="post",
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    parent: Mapped["StaffPost | None"] = relationship(
        "StaffPost",
        remote_side="StaffPost.id",
        foreign_keys=[parent_id],
    )


class StaffWorker(TimestampMixin, Base):
    """Persona de la plantilla municipal.

    `post_id` es único: un puesto lo ocupa como mucho una persona a la vez. Una
    persona sin puesto sigue siendo válida (contrataciones en trámite), y un
    puesto sin persona es simplemente una vacante.
    """

    __tablename__ = "staff_workers"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in(STAFF_WORKER_STATUSES)})",
            name="ck_staff_workers_status",
        ),
        CheckConstraint(
            "contract_type is null or "
            f"contract_type in ({_sql_in(STAFF_CONTRACT_TYPES)})",
            name="ck_staff_workers_contract_type",
        ),
        CheckConstraint(
            "contract_end_date is null"
            " or contract_start_date is null"
            " or contract_end_date >= contract_start_date",
            name="ck_staff_workers_contract_range",
        ),
        CheckConstraint(
            "vacation_days_limit is null or vacation_days_limit >= 0",
            name="ck_staff_workers_vacation_limit",
        ),
        CheckConstraint(
            "personal_days_limit is null or personal_days_limit >= 0",
            name="ck_staff_workers_personal_limit",
        ),
        CheckConstraint(
            "weekly_hours is null or weekly_hours >= 0",
            name="ck_staff_workers_weekly_hours",
        ),
        ForeignKeyConstraint(
            ["post_id", "organization_id"],
            ["staff_posts.id", "staff_posts.organization_id"],
            name="fk_staff_workers_post_org",
            ondelete="SET NULL",
        ),
        UniqueConstraint(
            "post_id",
            name="uq_staff_workers_post",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_staff_workers_id_org",
        ),
        Index(
            "ix_staff_workers_org_status",
            "organization_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )
    # Horario en texto libre ("martes de 9 a 14") más los días concretos, que
    # son lo único que la interfaz necesita consultar de forma estructurada.
    schedule_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schedule_days: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    weekly_hours: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    contract_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    contract_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    vacation_days_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    personal_days_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Personal externo que factura al ayuntamiento en lugar de estar en nómina.
    bills_invoices: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
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
    post: Mapped["StaffPost | None"] = relationship(
        "StaffPost",
        foreign_keys=[post_id],
        overlaps="organization",
    )
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )


class StaffAbsence(TimestampMixin, Base):
    """Ausencia de una persona de la plantilla, con su periodo."""

    __tablename__ = "staff_absences"
    __table_args__ = (
        CheckConstraint(
            f"absence_type in ({_sql_in(STAFF_ABSENCE_TYPES)})",
            name="ck_staff_absences_type",
        ),
        CheckConstraint(
            "end_date >= start_date",
            name="ck_staff_absences_range",
        ),
        ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_absences_worker_org",
            ondelete="CASCADE",
        ),
        Index(
            "ix_staff_absences_worker_start",
            "worker_id",
            "start_date",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    absence_type: Mapped[str] = mapped_column(String(30), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class StaffReport(TimestampMixin, Base):
    """Entrada del diario o parte de trabajo de una persona de la plantilla."""

    __tablename__ = "staff_reports"
    __table_args__ = (
        CheckConstraint(
            f"report_type in ({_sql_in(STAFF_REPORT_TYPES)})",
            name="ck_staff_reports_type",
        ),
        ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_reports_worker_org",
            ondelete="CASCADE",
        ),
        Index(
            "ix_staff_reports_worker_date",
            "worker_id",
            "report_date",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    report_type: Mapped[str] = mapped_column(String(20), nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    closing: Mapped[str | None] = mapped_column(Text, nullable=True)
    incident: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )


class StaffInvoice(TimestampMixin, Base):
    """Factura de personal externo que no está en nómina."""

    __tablename__ = "staff_invoices"
    __table_args__ = (
        CheckConstraint(
            "hours is null or hours >= 0",
            name="ck_staff_invoices_hours",
        ),
        CheckConstraint(
            "amount is null or amount >= 0",
            name="ck_staff_invoices_amount",
        ),
        ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_invoices_worker_org",
            ondelete="CASCADE",
        ),
        Index(
            "ix_staff_invoices_worker_date",
            "worker_id",
            "issued_on",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    concept: Mapped[str] = mapped_column(String(255), nullable=False)
    hours: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)


class StaffHistoryEvent(Base):
    """Rastro append-only de los cambios sobre una persona de la plantilla.

    Los datos de personal son sensibles y su edición debe poder auditarse, así
    que las modificaciones dejan un evento en lugar de sobrescribir en silencio.
    """

    __tablename__ = "staff_history_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type in ({_sql_in(STAFF_HISTORY_EVENT_TYPES)})",
            name="ck_staff_history_events_type",
        ),
        CheckConstraint(
            "from_status is null or "
            f"from_status in ({_sql_in(STAFF_WORKER_STATUSES)})",
            name="ck_staff_history_events_from_status",
        ),
        CheckConstraint(
            "to_status is null or "
            f"to_status in ({_sql_in(STAFF_WORKER_STATUSES)})",
            name="ck_staff_history_events_to_status",
        ),
        ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_history_events_worker_org",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_staff_history_events_worker",
            "worker_id",
            "id",
        ),
        Index(
            "ix_staff_history_events_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    changed_fields: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
