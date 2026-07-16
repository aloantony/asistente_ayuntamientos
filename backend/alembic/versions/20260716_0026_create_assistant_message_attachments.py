"""create assistant message attachments

Revision ID: 20260716_0026
Revises: 20260716_0025
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0026"
down_revision: str | None = "20260716_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_message_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("context_status", sa.String(length=30), nullable=False),
        sa.Column(
            "context_char_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
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
            "position >= 0",
            name="ck_assistant_message_attachments_position",
        ),
        sa.CheckConstraint(
            "context_status in ("
            "'ready', 'empty', 'unsupported', 'vision_unavailable', "
            "'too_large', 'unavailable', 'failed'"
            ")",
            name="ck_assistant_message_attachments_context_status",
        ),
        sa.CheckConstraint(
            "context_char_count >= 0",
            name="ck_assistant_message_attachments_context_char_count",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["assistant_messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            "document_id",
            name="uq_assistant_message_attachments_message_document",
        ),
        sa.UniqueConstraint(
            "message_id",
            "position",
            name="uq_assistant_message_attachments_message_position",
        ),
    )
    op.create_index(
        op.f("ix_assistant_message_attachments_message_id"),
        "assistant_message_attachments",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_message_attachments_document_id"),
        "assistant_message_attachments",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_assistant_message_attachments_document_id"),
        table_name="assistant_message_attachments",
    )
    op.drop_index(
        op.f("ix_assistant_message_attachments_message_id"),
        table_name="assistant_message_attachments",
    )
    op.drop_table("assistant_message_attachments")
