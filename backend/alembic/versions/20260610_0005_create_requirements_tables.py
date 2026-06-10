"""create requirements tables

Revision ID: 20260610_0005
Revises: 20260610_0004
Create Date: 2026-06-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260610_0005"
down_revision: Union[str, None] = "20260610_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "requirements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("problem", sa.Text(), nullable=True),
        sa.Column("current_process", sa.Text(), nullable=True),
        sa.Column("desired_process", sa.Text(), nullable=True),
        sa.Column("affected_users", sa.Text(), nullable=True),
        sa.Column("involved_documents", sa.Text(), nullable=True),
        sa.Column("data_sensitivity_notes", sa.Text(), nullable=True),
        sa.Column("legal_notes", sa.Text(), nullable=True),
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
        sa.Column("open_questions", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=20), server_default="medium", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="draft", nullable=False),
        sa.Column("source_type", sa.String(length=30), server_default="manual", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
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
            "priority in ('low', 'medium', 'high', 'urgent')",
            name="ck_requirements_priority",
        ),
        sa.CheckConstraint(
            """
            status in (
                'draft',
                'submitted',
                'in_review',
                'needs_clarification',
                'accepted',
                'rejected',
                'converted',
                'archived'
            )
            """,
            name="ck_requirements_status",
        ),
        sa.CheckConstraint(
            "source_type in ('manual', 'conversation', 'phone_call', 'meeting', 'other')",
            name="ck_requirements_source_type",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_requirements_organization_id"),
        "requirements",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_requirements_project_id"),
        "requirements",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_requirements_created_by_id"),
        "requirements",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_requirements_reviewed_by_id"),
        "requirements",
        ["reviewed_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_requirements_status"),
        "requirements",
        ["status"],
        unique=False,
    )

    op.create_table(
        "requirement_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("requirement_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("message_type", sa.String(length=30), server_default="note", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "message_type in ('note', 'question', 'answer', 'clarification', 'decision')",
            name="ck_requirement_messages_message_type",
        ),
        sa.ForeignKeyConstraint(
            ["requirement_id"],
            ["requirements.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_requirement_messages_requirement_id"),
        "requirement_messages",
        ["requirement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_requirement_messages_author_id"),
        "requirement_messages",
        ["author_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_requirement_messages_author_id"),
        table_name="requirement_messages",
    )
    op.drop_index(
        op.f("ix_requirement_messages_requirement_id"),
        table_name="requirement_messages",
    )
    op.drop_table("requirement_messages")

    op.drop_index(op.f("ix_requirements_status"), table_name="requirements")
    op.drop_index(op.f("ix_requirements_reviewed_by_id"), table_name="requirements")
    op.drop_index(op.f("ix_requirements_created_by_id"), table_name="requirements")
    op.drop_index(op.f("ix_requirements_project_id"), table_name="requirements")
    op.drop_index(op.f("ix_requirements_organization_id"), table_name="requirements")
    op.drop_table("requirements")
