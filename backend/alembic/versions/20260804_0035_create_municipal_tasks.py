"""create municipal roadmap tasks

Revision ID: 20260804_0035
Revises: 20260803_0034
Create Date: 2026-08-04 00:35:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260804_0035"
down_revision: Union[str, None] = "20260803_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TASK_STATUSES = "'pending', 'in_progress', 'blocked', 'completed', 'cancelled'"
TASK_PRIORITIES = "'low', 'normal', 'high', 'urgent'"
TASK_EVENT_TYPES = "'created', 'updated', 'status_changed', 'assigned'"


def upgrade() -> None:
    op.create_table(
        "municipal_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.String(length=20),
            server_default="normal",
            nullable=False,
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assignee_worker_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status in ({TASK_STATUSES})",
            name="ck_municipal_tasks_status",
        ),
        sa.CheckConstraint(
            f"priority in ({TASK_PRIORITIES})",
            name="ck_municipal_tasks_priority",
        ),
        sa.CheckConstraint(
            "btrim(title) <> ''",
            name="ck_municipal_tasks_title",
        ),
        sa.CheckConstraint(
            "status <> 'blocked' or blocked_reason is not null",
            name="ck_municipal_tasks_blocked_reason",
        ),
        sa.CheckConstraint(
            "completed_at is null or status = 'completed'",
            name="ck_municipal_tasks_completed_at",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["projects.id", "projects.organization_id"],
            name="fk_municipal_tasks_project_org",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_municipal_tasks_assignee_org",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_tasks_id_org",
        ),
    )
    op.create_index(
        "ix_municipal_tasks_org_status_due",
        "municipal_tasks",
        ["organization_id", "status", "due_date", "id"],
        unique=False,
    )
    op.create_index(
        "ix_municipal_tasks_assignee_status",
        "municipal_tasks",
        ["assignee_worker_id", "status", "id"],
        unique=False,
    )
    op.create_index(
        "ix_municipal_tasks_project_status",
        "municipal_tasks",
        ["project_id", "status", "id"],
        unique=False,
    )
    for column_name in ("created_by_id", "updated_by_id"):
        op.create_index(
            op.f(f"ix_municipal_tasks_{column_name}"),
            "municipal_tasks",
            [column_name],
            unique=False,
        )

    op.create_table(
        "municipal_task_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=True),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"event_type in ({TASK_EVENT_TYPES})",
            name="ck_municipal_task_events_type",
        ),
        sa.CheckConstraint(
            f"from_status is null or from_status in ({TASK_STATUSES})",
            name="ck_municipal_task_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status is null or to_status in ({TASK_STATUSES})",
            name="ck_municipal_task_events_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "organization_id"],
            ["municipal_tasks.id", "municipal_tasks.organization_id"],
            name="fk_municipal_task_events_task_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_municipal_task_events_task",
        "municipal_task_events",
        ["task_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_municipal_task_events_org_created",
        "municipal_task_events",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_municipal_task_events_actor_id"),
        "municipal_task_events",
        ["actor_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_municipal_task_events_actor_id"),
        table_name="municipal_task_events",
    )
    op.drop_index(
        "ix_municipal_task_events_org_created",
        table_name="municipal_task_events",
    )
    op.drop_index(
        "ix_municipal_task_events_task",
        table_name="municipal_task_events",
    )
    op.drop_table("municipal_task_events")

    for column_name in reversed(("created_by_id", "updated_by_id")):
        op.drop_index(
            op.f(f"ix_municipal_tasks_{column_name}"),
            table_name="municipal_tasks",
        )
    op.drop_index("ix_municipal_tasks_project_status", table_name="municipal_tasks")
    op.drop_index("ix_municipal_tasks_assignee_status", table_name="municipal_tasks")
    op.drop_index("ix_municipal_tasks_org_status_due", table_name="municipal_tasks")
    op.drop_table("municipal_tasks")
