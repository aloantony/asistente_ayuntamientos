import json
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User


AGENT_OFFICE_DEPARTMENTS = (
    "front_desk",
    "requirements",
    "ordinances",
    "documents",
    "projects",
    "map",
    "daily_briefing",
)
AGENT_OFFICE_TASK_STATUSES = (
    "pending_approval",
    "approved",
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
)
AGENT_OFFICE_PRIORITIES = ("low", "medium", "high", "urgent")
AGENT_OFFICE_APPROVAL_POLICIES = (
    "never",
    "before_execution",
    "after_draft",
    "always",
)
AGENT_OFFICE_ROUTINE_KINDS = ("daily_briefing",)
AGENT_OFFICE_ROUTINE_STATUSES = ("active", "paused", "archived")
AGENT_OFFICE_ROUTINE_CADENCES = ("daily", "manual")
AGENT_OFFICE_TARGET_CHANNELS = ("web", "telegram", "none")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _decode_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class AgentOfficeTask(TimestampMixin, Base):
    __tablename__ = "agent_office_tasks"
    __table_args__ = (
        CheckConstraint(
            f"department in ({_sql_in(AGENT_OFFICE_DEPARTMENTS)})",
            name="ck_agent_office_tasks_department",
        ),
        CheckConstraint(
            f"status in ({_sql_in(AGENT_OFFICE_TASK_STATUSES)})",
            name="ck_agent_office_tasks_status",
        ),
        CheckConstraint(
            f"priority in ({_sql_in(AGENT_OFFICE_PRIORITIES)})",
            name="ck_agent_office_tasks_priority",
        ),
        CheckConstraint(
            f"approval_policy in ({_sql_in(AGENT_OFFICE_APPROVAL_POLICIES)})",
            name="ck_agent_office_tasks_approval_policy",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    requested_action: Mapped[str] = mapped_column(String(120), nullable=False)
    priority: Mapped[str] = mapped_column(
        String(20),
        default="medium",
        server_default="medium",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="pending_approval",
        server_default="pending_approval",
        nullable=False,
    )
    approval_policy: Mapped[str] = mapped_column(
        String(30),
        default="before_execution",
        server_default="before_execution",
        nullable=False,
    )
    requires_human_approval: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    assigned_agent_key: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    source_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    requested_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    requested_by: Mapped["User | None"] = relationship("User", foreign_keys=[requested_by_id])
    approved_by: Mapped["User | None"] = relationship("User", foreign_keys=[approved_by_id])
    events: Mapped[list["AgentOfficeTaskEvent"]] = relationship(
        "AgentOfficeTaskEvent",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="AgentOfficeTaskEvent.id",
    )

    @property
    def input(self) -> dict:
        return _decode_json_object(self.input_json)

    @property
    def result(self) -> dict:
        return _decode_json_object(self.result_json)


class AgentOfficeTaskEvent(Base):
    __tablename__ = "agent_office_task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_office_tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    task: Mapped[AgentOfficeTask] = relationship("AgentOfficeTask", back_populates="events")
    created_by: Mapped["User | None"] = relationship("User")

    @property
    def payload(self) -> dict:
        return _decode_json_object(self.payload_json)


class AgentOfficeRoutine(TimestampMixin, Base):
    __tablename__ = "agent_office_routines"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(AGENT_OFFICE_ROUTINE_KINDS)})",
            name="ck_agent_office_routines_kind",
        ),
        CheckConstraint(
            f"status in ({_sql_in(AGENT_OFFICE_ROUTINE_STATUSES)})",
            name="ck_agent_office_routines_status",
        ),
        CheckConstraint(
            f"cadence in ({_sql_in(AGENT_OFFICE_ROUTINE_CADENCES)})",
            name="ck_agent_office_routines_cadence",
        ),
        CheckConstraint(
            f"target_channel in ({_sql_in(AGENT_OFFICE_TARGET_CHANNELS)})",
            name="ck_agent_office_routines_target_channel",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="active",
        server_default="active",
        nullable=False,
    )
    cadence: Mapped[str] = mapped_column(
        String(20),
        default="daily",
        server_default="daily",
        nullable=False,
    )
    schedule_time: Mapped[str] = mapped_column(String(5), default="09:15", server_default="09:15", nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Madrid", server_default="Europe/Madrid", nullable=False)
    target_channel: Mapped[str] = mapped_column(
        String(30),
        default="web",
        server_default="web",
        nullable=False,
    )
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    created_by: Mapped["User | None"] = relationship("User")

    @property
    def config(self) -> dict:
        return _decode_json_object(self.config_json)
