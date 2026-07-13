"""remove abandoned prototype tables

Revision ID: 20260713_0021
Revises: 20260701_0020
Create Date: 2026-07-13 18:00:00.000000

The preceding revisions were applied to persistent databases before their
feature branches were discarded. They remain in the repository as immutable
history; this successor removes only their unused schema.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260713_0021"
down_revision: Union[str, None] = "20260701_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ABANDONED_TABLES = (
    "assistant_knowledge_proposals",
    "document_work_artifacts",
)

SOURCE_TYPES = ("official", "public_administration", "news", "provider", "blog", "unknown")
CONFIDENCES = ("low", "medium", "high")
KNOWLEDGE_STATUSES = ("proposed", "approved", "rejected")
SENSITIVITIES = ("normal", "personal", "sensitive", "legal")

ARTIFACT_TYPES = (
    "report",
    "note",
    "comparison",
    "communication",
    "checklist",
)
ARTIFACT_STATUSES = (
    "draft",
    "in_review",
    "approved",
    "changes_requested",
    "export_requested",
    "archived",
)


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _assert_tables_are_empty(
    bind: sa.engine.Connection,
    existing_tables: set[str],
) -> None:
    populated_tables = [
        table_name
        for table_name in ABANDONED_TABLES
        if table_name in existing_tables
        and bind.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table_name})")
        ).scalar_one()
    ]
    if populated_tables:
        names = ", ".join(populated_tables)
        raise RuntimeError(
            "Refusing to remove populated prototype tables: "
            f"{names}. Export or migrate their data before retrying."
        )


def _lock_existing_tables(
    bind: sa.engine.Connection,
    existing_tables: set[str],
) -> None:
    for table_name in ABANDONED_TABLES:
        if table_name in existing_tables:
            bind.execute(
                sa.text(f"LOCK TABLE {table_name} IN ACCESS EXCLUSIVE MODE")
            )


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    _lock_existing_tables(bind, existing_tables)
    _assert_tables_are_empty(bind, existing_tables)

    for table_name in reversed(ABANDONED_TABLES):
        if table_name in existing_tables:
            op.drop_table(table_name)


def downgrade() -> None:
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
        sa.CheckConstraint(
            f"status in ({_sql_in(KNOWLEDGE_STATUSES)})",
            name="ck_assistant_knowledge_proposals_status",
        ),
        sa.CheckConstraint(
            f"source_type in ({_sql_in(SOURCE_TYPES)})",
            name="ck_assistant_knowledge_proposals_source_type",
        ),
        sa.CheckConstraint(
            f"confidence in ({_sql_in(CONFIDENCES)})",
            name="ck_assistant_knowledge_proposals_confidence",
        ),
        sa.CheckConstraint(
            f"sensitivity in ({_sql_in(SENSITIVITIES)})",
            name="ck_assistant_knowledge_proposals_sensitivity",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["proposed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column_name in (
        "organization_id",
        "source_conversation_id",
        "source_message_id",
        "proposed_by_id",
        "reviewed_by_id",
        "status",
    ):
        op.create_index(
            op.f(f"ix_assistant_knowledge_proposals_{column_name}"),
            "assistant_knowledge_proposals",
            [column_name],
            unique=False,
        )

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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            f"artifact_type in ({_sql_in(ARTIFACT_TYPES)})",
            name="ck_document_work_artifacts_type",
        ),
        sa.CheckConstraint(
            f"status in ({_sql_in(ARTIFACT_STATUSES)})",
            name="ck_document_work_artifacts_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["export_requested_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exported_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_conversation_id"], ["assistant_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["assistant_messages.id"], ondelete="SET NULL"),
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
