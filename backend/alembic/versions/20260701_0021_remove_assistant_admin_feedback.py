"""remove assistant admin feedback

Revision ID: 20260701_0021
Revises: 20260629_0018
Create Date: 2026-07-01 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision: str = "20260701_0021"
down_revision: Union[str, None] = "20260629_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ACTIVE_DEPARTMENTS = (
    "front_desk",
    "requirements",
    "ordinances",
    "documents",
    "projects",
    "map",
    "daily_briefing",
)
LEGACY_DEPARTMENTS = (
    "front_desk",
    "requirements",
    "ordinances",
    "documents",
    "projects",
    "map",
    "admin_feedback",
    "daily_briefing",
)
FEEDBACK_INDEX_COLUMNS = (
    "organization_id",
    "source_conversation_id",
    "source_message_id",
    "submitted_by_id",
    "reviewed_by_id",
    "status",
)


def sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def has_table(inspector: Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def has_check_constraint(
    inspector: Inspector,
    table_name: str,
    constraint_name: str,
) -> bool:
    return any(
        constraint.get("name") == constraint_name
        for constraint in inspector.get_check_constraints(table_name)
    )


def recreate_department_constraint(
    inspector: Inspector,
    departments: tuple[str, ...],
) -> None:
    if not has_table(inspector, "agent_office_tasks"):
        return
    constraint_name = "ck_agent_office_tasks_department"
    if has_check_constraint(inspector, "agent_office_tasks", constraint_name):
        op.drop_constraint(constraint_name, "agent_office_tasks", type_="check")
    op.create_check_constraint(
        constraint_name,
        "agent_office_tasks",
        f"department in ({sql_in(departments)})",
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if has_table(inspector, "agent_office_tasks"):
        op.execute(
            sa.text(
                """
                UPDATE agent_office_tasks
                SET department = 'front_desk',
                    requested_action = 'triage',
                    status = CASE
                        WHEN status IN (
                            'pending_approval',
                            'approved',
                            'queued',
                            'running',
                            'waiting_approval',
                            'failed'
                        )
                        THEN 'cancelled'
                        ELSE status
                    END,
                    error_message = COALESCE(
                        error_message,
                        'Deprecated admin feedback task disabled by migration.'
                    ),
                    updated_at = now()
                WHERE department = 'admin_feedback'
                   OR requested_action = 'send_admin_feedback'
                """
            )
        )
        recreate_department_constraint(inspector, ACTIVE_DEPARTMENTS)

    if has_table(inspector, "assistant_admin_feedback"):
        for column in reversed(FEEDBACK_INDEX_COLUMNS):
            op.drop_index(
                op.f(f"ix_assistant_admin_feedback_{column}"),
                table_name="assistant_admin_feedback",
            )
        op.drop_table("assistant_admin_feedback")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    recreate_department_constraint(inspector, LEGACY_DEPARTMENTS)

    if has_table(inspector, "assistant_admin_feedback"):
        return

    op.create_table(
        "assistant_admin_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=20), server_default="medium", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="submitted", nullable=False),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("submitted_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('submitted', 'reviewed', 'dismissed', 'archived')",
            name="ck_assistant_admin_feedback_status",
        ),
        sa.CheckConstraint(
            "category in ('bug', 'improvement', 'missing_capability', 'data_issue', 'ux', 'other')",
            name="ck_assistant_admin_feedback_category",
        ),
        sa.CheckConstraint(
            "priority in ('low', 'medium', 'high', 'urgent')",
            name="ck_assistant_admin_feedback_priority",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in FEEDBACK_INDEX_COLUMNS:
        op.create_index(
            op.f(f"ix_assistant_admin_feedback_{column}"),
            "assistant_admin_feedback",
            [column],
            unique=False,
        )
