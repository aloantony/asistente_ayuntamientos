"""create assistant memory entries

Revision ID: 20260615_0010
Revises: 20260612_0009
Create Date: 2026-06-15 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260615_0010"
down_revision: Union[str, None] = "20260612_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_memory_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column(
            "sensitivity",
            sa.String(length=30),
            server_default="normal",
            nullable=False,
        ),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("proposed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status in ('proposed', 'approved', 'rejected', 'archived', 'blocked')",
            name="ck_assistant_memory_entries_status",
        ),
        sa.CheckConstraint(
            "category in ('protocol', 'preference', 'context', 'decision', 'open_question')",
            name="ck_assistant_memory_entries_category",
        ),
        sa.CheckConstraint(
            "sensitivity in ('normal', 'personal', 'sensitive', 'legal')",
            name="ck_assistant_memory_entries_sensitivity",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["assistant_conversations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["assistant_messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_id"],
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
        op.f("ix_assistant_memory_entries_organization_id"),
        "assistant_memory_entries",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_memory_entries_status"),
        "assistant_memory_entries",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_memory_entries_source_conversation_id"),
        "assistant_memory_entries",
        ["source_conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_memory_entries_source_message_id"),
        "assistant_memory_entries",
        ["source_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_memory_entries_proposed_by_id"),
        "assistant_memory_entries",
        ["proposed_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_memory_entries_reviewed_by_id"),
        "assistant_memory_entries",
        ["reviewed_by_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_assistant_memory_entries_reviewed_by_id"),
        table_name="assistant_memory_entries",
    )
    op.drop_index(
        op.f("ix_assistant_memory_entries_proposed_by_id"),
        table_name="assistant_memory_entries",
    )
    op.drop_index(
        op.f("ix_assistant_memory_entries_source_message_id"),
        table_name="assistant_memory_entries",
    )
    op.drop_index(
        op.f("ix_assistant_memory_entries_source_conversation_id"),
        table_name="assistant_memory_entries",
    )
    op.drop_index(
        op.f("ix_assistant_memory_entries_status"),
        table_name="assistant_memory_entries",
    )
    op.drop_index(
        op.f("ix_assistant_memory_entries_organization_id"),
        table_name="assistant_memory_entries",
    )
    op.drop_table("assistant_memory_entries")
