"""create assistant admin feedback

Revision ID: 20260628_0017
Revises: 20260628_0016
Create Date: 2026-06-28 20:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260628_0017"
down_revision: Union[str, None] = "20260628_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
        sa.CheckConstraint("status in ('submitted', 'reviewed', 'dismissed', 'archived')", name="ck_assistant_admin_feedback_status"),
        sa.CheckConstraint("category in ('bug', 'improvement', 'missing_capability', 'data_issue', 'ux', 'other')", name="ck_assistant_admin_feedback_category"),
        sa.CheckConstraint("priority in ('low', 'medium', 'high', 'urgent')", name="ck_assistant_admin_feedback_priority"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["submitted_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "organization_id",
        "source_conversation_id",
        "source_message_id",
        "submitted_by_id",
        "reviewed_by_id",
        "status",
    ):
        op.create_index(
            op.f(f"ix_assistant_admin_feedback_{column}"),
            "assistant_admin_feedback",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in (
        "status",
        "reviewed_by_id",
        "submitted_by_id",
        "source_message_id",
        "source_conversation_id",
        "organization_id",
    ):
        op.drop_index(op.f(f"ix_assistant_admin_feedback_{column}"), table_name="assistant_admin_feedback")
    op.drop_table("assistant_admin_feedback")
