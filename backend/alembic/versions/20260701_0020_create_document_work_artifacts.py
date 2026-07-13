"""create document work artifacts

Revision ID: 20260701_0020
Revises: 20260701_0019
Create Date: 2026-07-01 03:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260701_0020"
down_revision: Union[str, None] = "20260701_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DOCUMENT_WORK_ARTIFACT_TYPES = (
    "report",
    "note",
    "comparison",
    "communication",
    "checklist",
)
DOCUMENT_WORK_ARTIFACT_STATUSES = (
    "draft",
    "in_review",
    "approved",
    "changes_requested",
    "export_requested",
    "archived",
)


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "document_work_artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="draft", nullable=False),
        sa.Column("source_summary", sa.Text(), nullable=True),
        sa.Column("source_document_ids", sa.JSON(), nullable=False),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("export_format", sa.String(length=30), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("export_requested_by_id", sa.Integer(), nullable=True),
        sa.Column("exported_by_id", sa.Integer(), nullable=True),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("export_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
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
            f"artifact_type in ({_sql_in(DOCUMENT_WORK_ARTIFACT_TYPES)})",
            name="ck_document_work_artifacts_type",
        ),
        sa.CheckConstraint(
            f"status in ({_sql_in(DOCUMENT_WORK_ARTIFACT_STATUSES)})",
            name="ck_document_work_artifacts_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["export_requested_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exported_by_id"], ["users.id"], ondelete="SET NULL"),
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
        sa.PrimaryKeyConstraint("id"),
    )
    for column_name in (
        "organization_id",
        "project_id",
        "status",
        "created_by_id",
        "reviewed_by_id",
        "export_requested_by_id",
        "exported_by_id",
        "source_conversation_id",
        "source_message_id",
    ):
        op.create_index(
            op.f(f"ix_document_work_artifacts_{column_name}"),
            "document_work_artifacts",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    for column_name in reversed(
        (
            "organization_id",
            "project_id",
            "status",
            "created_by_id",
            "reviewed_by_id",
            "export_requested_by_id",
            "exported_by_id",
            "source_conversation_id",
            "source_message_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_document_work_artifacts_{column_name}"),
            table_name="document_work_artifacts",
        )
    op.drop_table("document_work_artifacts")
