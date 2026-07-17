"""create assistant canvas documents

Revision ID: 20260717_0030
Revises: 20260717_0029
Create Date: 2026-07-17 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0030"
down_revision: Union[str, None] = "20260717_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_canvas_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("document_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "current_revision",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("creation_id", sa.String(length=255), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
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
            "current_revision >= 1",
            name="ck_assistant_canvas_documents_current_revision",
        ),
        sa.CheckConstraint(
            "status in ('draft', 'archived')",
            name="ck_assistant_canvas_documents_status",
        ),
        sa.CheckConstraint(
            "document_type in ('municipal_ordinance', 'regulation', "
            "'report', 'letter', 'minutes', 'other')",
            name="ck_assistant_canvas_documents_type",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["assistant_conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
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
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["assistant_messages.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "creation_id",
            name="uq_assistant_canvas_documents_conversation_creation",
        ),
    )
    for column_name in (
        "conversation_id",
        "organization_id",
        "status",
        "created_by_id",
        "updated_by_id",
        "source_message_id",
    ):
        op.create_index(
            op.f(f"ix_assistant_canvas_documents_{column_name}"),
            "assistant_canvas_documents",
            [column_name],
            unique=False,
        )

    op.create_table(
        "assistant_canvas_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("edit_source", sa.String(length=20), nullable=False),
        sa.Column("mutation_id", sa.String(length=255), nullable=True),
        sa.Column("source_tool_call_id", sa.String(length=255), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_assistant_canvas_revisions_number",
        ),
        sa.CheckConstraint(
            "edit_source in ('user', 'assistant', 'restore')",
            name="ck_assistant_canvas_revisions_source",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["assistant_canvas_documents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["assistant_messages.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "mutation_id",
            name="uq_assistant_canvas_revisions_document_mutation",
        ),
        sa.UniqueConstraint(
            "document_id",
            "revision_number",
            name="uq_assistant_canvas_revisions_document_number",
        ),
    )
    for column_name in ("document_id", "created_by_id", "source_message_id"):
        op.create_index(
            op.f(f"ix_assistant_canvas_revisions_{column_name}"),
            "assistant_canvas_revisions",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    for column_name in reversed(
        ("document_id", "created_by_id", "source_message_id")
    ):
        op.drop_index(
            op.f(f"ix_assistant_canvas_revisions_{column_name}"),
            table_name="assistant_canvas_revisions",
        )
    op.drop_table("assistant_canvas_revisions")

    for column_name in reversed(
        (
            "conversation_id",
            "organization_id",
            "status",
            "created_by_id",
            "updated_by_id",
            "source_message_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_assistant_canvas_documents_{column_name}"),
            table_name="assistant_canvas_documents",
        )
    op.drop_table("assistant_canvas_documents")
