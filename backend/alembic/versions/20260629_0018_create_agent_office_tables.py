"""create agent office tables

Revision ID: 20260629_0018
Revises: 20260628_0017
Create Date: 2026-06-29 13:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260629_0018"
down_revision: Union[str, None] = "20260628_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEPARTMENTS = (
    "front_desk",
    "requirements",
    "ordinances",
    "documents",
    "projects",
    "map",
    "admin_feedback",
    "daily_briefing",
)
TASK_STATUSES = (
    "pending_approval",
    "approved",
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
)
PRIORITIES = ("low", "medium", "high", "urgent")
APPROVAL_POLICIES = ("never", "before_execution", "after_draft", "always")
ROUTINE_KINDS = ("daily_briefing",)
ROUTINE_STATUSES = ("active", "paused", "archived")
ROUTINE_CADENCES = ("daily", "manual")
TARGET_CHANNELS = ("web", "telegram", "none")


def sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "agent_office_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("department", sa.String(length=40), nullable=False),
        sa.Column("requested_action", sa.String(length=120), nullable=False),
        sa.Column("priority", sa.String(length=20), server_default="medium", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending_approval", nullable=False),
        sa.Column("approval_policy", sa.String(length=30), server_default="before_execution", nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("assigned_agent_key", sa.String(length=100), nullable=True),
        sa.Column("routing_reason", sa.Text(), nullable=True),
        sa.Column("input_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("requested_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"department in ({sql_in(DEPARTMENTS)})", name="ck_agent_office_tasks_department"),
        sa.CheckConstraint(f"status in ({sql_in(TASK_STATUSES)})", name="ck_agent_office_tasks_status"),
        sa.CheckConstraint(f"priority in ({sql_in(PRIORITIES)})", name="ck_agent_office_tasks_priority"),
        sa.CheckConstraint(f"approval_policy in ({sql_in(APPROVAL_POLICIES)})", name="ck_agent_office_tasks_approval_policy"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "organization_id",
        "department",
        "status",
        "assigned_agent_key",
        "scheduled_for",
        "source_conversation_id",
        "source_message_id",
        "requested_by_id",
        "approved_by_id",
    ):
        op.create_index(op.f(f"ix_agent_office_tasks_{column}"), "agent_office_tasks", [column], unique=False)

    op.create_table(
        "agent_office_task_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_office_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("task_id", "event_type", "created_by_id"):
        op.create_index(op.f(f"ix_agent_office_task_events_{column}"), "agent_office_task_events", [column], unique=False)

    op.create_table(
        "agent_office_routines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("cadence", sa.String(length=20), server_default="daily", nullable=False),
        sa.Column("schedule_time", sa.String(length=5), server_default="09:15", nullable=False),
        sa.Column("timezone", sa.String(length=80), server_default="Europe/Madrid", nullable=False),
        sa.Column("target_channel", sa.String(length=30), server_default="web", nullable=False),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"kind in ({sql_in(ROUTINE_KINDS)})", name="ck_agent_office_routines_kind"),
        sa.CheckConstraint(f"status in ({sql_in(ROUTINE_STATUSES)})", name="ck_agent_office_routines_status"),
        sa.CheckConstraint(f"cadence in ({sql_in(ROUTINE_CADENCES)})", name="ck_agent_office_routines_cadence"),
        sa.CheckConstraint(f"target_channel in ({sql_in(TARGET_CHANNELS)})", name="ck_agent_office_routines_target_channel"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "kind", "status", "created_by_id", "next_run_at"):
        op.create_index(op.f(f"ix_agent_office_routines_{column}"), "agent_office_routines", [column], unique=False)


def downgrade() -> None:
    for column in ("next_run_at", "created_by_id", "status", "kind", "organization_id"):
        op.drop_index(op.f(f"ix_agent_office_routines_{column}"), table_name="agent_office_routines")
    op.drop_table("agent_office_routines")

    for column in ("created_by_id", "event_type", "task_id"):
        op.drop_index(op.f(f"ix_agent_office_task_events_{column}"), table_name="agent_office_task_events")
    op.drop_table("agent_office_task_events")

    for column in (
        "approved_by_id",
        "requested_by_id",
        "source_message_id",
        "source_conversation_id",
        "scheduled_for",
        "assigned_agent_key",
        "status",
        "department",
        "organization_id",
    ):
        op.drop_index(op.f(f"ix_agent_office_tasks_{column}"), table_name="agent_office_tasks")
    op.drop_table("agent_office_tasks")
