"""create assistant knowledge proposals

Revision ID: 20260701_0019
Revises: 20260629_0018
Create Date: 2026-07-01 03:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260701_0019"
down_revision: Union[str, None] = "20260629_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCE_TYPES = ("official", "public_administration", "news", "provider", "blog", "unknown")
CONFIDENCES = ("low", "medium", "high")
STATUSES = ("proposed", "approved", "rejected")
SENSITIVITIES = ("normal", "personal", "sensitive", "legal")


def sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "assistant_knowledge_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("source_title", sa.String(length=500), nullable=True),
        sa.Column("source_type", sa.String(length=40), server_default="unknown", nullable=False),
        sa.Column("confidence", sa.String(length=20), server_default="medium", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="proposed", nullable=False),
        sa.Column("sensitivity", sa.String(length=30), server_default="normal", nullable=False),
        sa.Column("requires_legal_review", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("proposed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"status in ({sql_in(STATUSES)})", name="ck_assistant_knowledge_proposals_status"),
        sa.CheckConstraint(f"source_type in ({sql_in(SOURCE_TYPES)})", name="ck_assistant_knowledge_proposals_source_type"),
        sa.CheckConstraint(f"confidence in ({sql_in(CONFIDENCES)})", name="ck_assistant_knowledge_proposals_confidence"),
        sa.CheckConstraint(f"sensitivity in ({sql_in(SENSITIVITIES)})", name="ck_assistant_knowledge_proposals_sensitivity"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proposed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "organization_id",
        "source_conversation_id",
        "source_message_id",
        "proposed_by_id",
        "reviewed_by_id",
        "status",
    ):
        op.create_index(
            op.f(f"ix_assistant_knowledge_proposals_{column}"),
            "assistant_knowledge_proposals",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in (
        "status",
        "reviewed_by_id",
        "proposed_by_id",
        "source_message_id",
        "source_conversation_id",
        "organization_id",
    ):
        op.drop_index(
            op.f(f"ix_assistant_knowledge_proposals_{column}"),
            table_name="assistant_knowledge_proposals",
        )
    op.drop_table("assistant_knowledge_proposals")
