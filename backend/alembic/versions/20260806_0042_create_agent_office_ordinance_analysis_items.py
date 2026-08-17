"""create durable agent office ordinance analysis items

Revision ID: 20260806_0042
Revises: 20260805_0041
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260806_0042"
down_revision: str | None = "20260805_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_office_ordinance_analysis_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("ordinance_id", sa.Integer(), nullable=True),
        sa.Column(
            "source_ordinance_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "source_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "source_hash",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "source_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
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
            "status in ('pending', 'running', 'completed', 'failed')",
            name="ck_agent_office_ord_analysis_items_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_agent_office_ord_analysis_items_attempts",
        ),
        sa.CheckConstraint(
            (
                "ordinance_id is null or "
                "ordinance_id = source_ordinance_id"
            ),
            name=(
                "ck_agent_office_ord_analysis_items_source_identity"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["agent_office_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ordinance_id"],
            ["ordinances.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "source_ordinance_id",
            name="uq_agent_office_ord_analysis_task_ordinance",
        ),
    )
    op.create_index(
        op.f("ix_agent_office_ordinance_analysis_items_ordinance_id"),
        "agent_office_ordinance_analysis_items",
        ["ordinance_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_office_ord_analysis_task_status_source",
        "agent_office_ordinance_analysis_items",
        ["task_id", "status", "source_ordinance_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_office_ord_analysis_task_status_source",
        table_name="agent_office_ordinance_analysis_items",
    )
    op.drop_index(
        op.f("ix_agent_office_ordinance_analysis_items_ordinance_id"),
        table_name="agent_office_ordinance_analysis_items",
    )
    op.drop_table("agent_office_ordinance_analysis_items")
