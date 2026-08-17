from datetime import date, datetime
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.projects.models import Project
    from app.staff.models import StaffWorker
    from app.users.models import User


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# Vocabulario en inglés como el resto de dominios del producto; los rótulos en
# español viven en la interfaz. "Vencida" NO aparece aquí a propósito: no es un
# estado sino una lectura del calendario sobre `due_date`, y guardarla obligaría
# a un proceso que reescribiese filas cada medianoche.
MUNICIPAL_TASK_STATUSES = (
    "pending",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
)

MUNICIPAL_TASK_OPEN_STATUSES = ("pending", "in_progress", "blocked")

MUNICIPAL_TASK_TERMINAL_STATUSES = ("completed", "cancelled")

MUNICIPAL_TASK_PRIORITIES = ("low", "normal", "high", "urgent")

MUNICIPAL_TASK_EVENT_TYPES = (
    "created",
    "updated",
    "status_changed",
    "assigned",
)


class MunicipalTask(TimestampMixin, Base):
    """Trabajo pendiente del ayuntamiento, con responsable y fecha límite.

    Una tarea puede colgar de un proyecto y asignarse a alguien de la plantilla,
    pero no necesita ninguna de las dos cosas: buena parte del trabajo de un
    municipio pequeño nace de un aviso suelto y se resuelve sin expediente.
    """

    __tablename__ = "municipal_tasks"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in(MUNICIPAL_TASK_STATUSES)})",
            name="ck_municipal_tasks_status",
        ),
        CheckConstraint(
            f"priority in ({_sql_in(MUNICIPAL_TASK_PRIORITIES)})",
            name="ck_municipal_tasks_priority",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_municipal_tasks_title",
        ),
        # Una tarea bloqueada sin explicar por qué no se puede desbloquear:
        # nadie sabría qué hay que resolver.
        CheckConstraint(
            "status <> 'blocked' or blocked_reason is not null",
            name="ck_municipal_tasks_blocked_reason",
        ),
        CheckConstraint(
            "completed_at is null or status = 'completed'",
            name="ck_municipal_tasks_completed_at",
        ),
        ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            name="fk_municipal_tasks_project_org",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["assignee_worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_municipal_tasks_assignee_org",
            ondelete="SET NULL",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_tasks_id_org",
        ),
        Index(
            "ix_municipal_tasks_org_status_due",
            "organization_id",
            "status",
            "due_date",
            "id",
        ),
        Index(
            "ix_municipal_tasks_assignee_status",
            "assignee_worker_id",
            "status",
            "id",
        ),
        Index(
            "ix_municipal_tasks_project_status",
            "project_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        server_default="normal",
        nullable=False,
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    assignee_worker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    assignee: Mapped["StaffWorker | None"] = relationship(
        "StaffWorker",
        foreign_keys=[assignee_worker_id],
        overlaps="organization",
    )
    project: Mapped["Project | None"] = relationship(
        "Project",
        foreign_keys=[project_id],
        overlaps="assignee,organization",
    )
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
    events: Mapped[list["MunicipalTaskEvent"]] = relationship(
        "MunicipalTaskEvent",
        back_populates="task",
        # Del más reciente al más antiguo: al abrir una tarea interesa lo último
        # que le pasó, no cómo empezó.
        order_by="MunicipalTaskEvent.id.desc()",
        viewonly=True,
    )


class MunicipalTaskEvent(Base):
    """Rastro append-only de lo que le ha pasado a una tarea.

    La hoja de ruta es el sitio donde se rinde cuentas del trabajo municipal, así
    que un cambio de estado o de responsable deja constancia de quién lo hizo y
    cuándo, en lugar de sobrescribirse.
    """

    __tablename__ = "municipal_task_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type in ({_sql_in(MUNICIPAL_TASK_EVENT_TYPES)})",
            name="ck_municipal_task_events_type",
        ),
        CheckConstraint(
            "from_status is null or "
            f"from_status in ({_sql_in(MUNICIPAL_TASK_STATUSES)})",
            name="ck_municipal_task_events_from_status",
        ),
        CheckConstraint(
            "to_status is null or "
            f"to_status in ({_sql_in(MUNICIPAL_TASK_STATUSES)})",
            name="ck_municipal_task_events_to_status",
        ),
        ForeignKeyConstraint(
            ["task_id", "organization_id"],
            ["municipal_tasks.id", "municipal_tasks.organization_id"],
            name="fk_municipal_task_events_task_org",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_municipal_task_events_task",
            "task_id",
            "id",
        ),
        Index(
            "ix_municipal_task_events_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, nullable=False)
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

    task: Mapped["MunicipalTask"] = relationship(
        "MunicipalTask",
        back_populates="events",
        viewonly=True,
    )
