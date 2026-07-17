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
        sa.Column(
            "creation_payload_sha256",
            sa.String(length=64),
            nullable=True,
        ),
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
            "(creation_id is null and creation_payload_sha256 is null) or "
            "(creation_id is not null and "
            "creation_payload_sha256 is not null and "
            "creation_payload_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_assistant_canvas_documents_creation_payload",
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
        sa.Column(
            "mutation_payload_sha256",
            sa.String(length=64),
            nullable=True,
        ),
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
            "(mutation_id is null and mutation_payload_sha256 is null) or "
            "(mutation_id is not null and "
            "mutation_payload_sha256 is not null and "
            "mutation_payload_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_assistant_canvas_revisions_mutation_payload",
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

    op.execute(
        """
        CREATE FUNCTION prevent_assistant_canvas_revision_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF pg_trigger_depth() > 1 THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION
                    'assistant canvas revisions are immutable';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.document_id IS DISTINCT FROM OLD.document_id
                OR NEW.revision_number IS DISTINCT FROM OLD.revision_number
                OR NEW.title IS DISTINCT FROM OLD.title
                OR NEW.content IS DISTINCT FROM OLD.content
                OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
                OR NEW.change_summary IS DISTINCT FROM OLD.change_summary
                OR NEW.edit_source IS DISTINCT FROM OLD.edit_source
                OR NEW.mutation_id IS DISTINCT FROM OLD.mutation_id
                OR NEW.mutation_payload_sha256 IS DISTINCT FROM
                    OLD.mutation_payload_sha256
                OR NEW.source_tool_call_id IS DISTINCT FROM
                    OLD.source_tool_call_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION
                    'assistant canvas revisions are immutable';
            END IF;
            IF (
                NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
                OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
            ) AND pg_trigger_depth() <= 1
            THEN
                RAISE EXCEPTION
                    'assistant canvas revisions are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_assistant_canvas_revisions_immutable
        BEFORE UPDATE OR DELETE ON assistant_canvas_revisions
        FOR EACH ROW
        EXECUTE FUNCTION prevent_assistant_canvas_revision_mutation()
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    op.execute(
        "LOCK TABLE assistant_canvas_documents, assistant_canvas_revisions "
        "IN ACCESS EXCLUSIVE MODE"
    )
    revision_count = connection.execute(
        sa.text("SELECT count(*) FROM assistant_canvas_revisions")
    ).scalar_one()
    document_count = connection.execute(
        sa.text("SELECT count(*) FROM assistant_canvas_documents")
    ).scalar_one()
    if revision_count or document_count:
        raise RuntimeError(
            "Cannot downgrade assistant canvas migration while documents or "
            "revisions exist; preserve or explicitly remove the draft data first"
        )

    op.execute(
        "DROP TRIGGER trg_assistant_canvas_revisions_immutable "
        "ON assistant_canvas_revisions"
    )
    op.execute("DROP FUNCTION prevent_assistant_canvas_revision_mutation()")
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
