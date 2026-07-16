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


def _documents_project_foreign_key_name() -> str:
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys("documents")
    matches = [
        foreign_key
        for foreign_key in foreign_keys
        if foreign_key["referred_table"] == "projects"
        and foreign_key["constrained_columns"] == ["project_id"]
    ]
    if len(matches) != 1 or not matches[0].get("name"):
        raise RuntimeError(
            "Expected exactly one named documents(project_id) foreign key "
            "before upgrading 20260716_0026"
        )
    return str(matches[0]["name"])


def upgrade() -> None:
    inconsistent_document = op.get_bind().execute(
        sa.text(
            "SELECT d.id AS document_id, d.organization_id AS document_organization_id, "
            "p.organization_id AS project_organization_id "
            "FROM documents AS d "
            "JOIN projects AS p ON p.id = d.project_id "
            "WHERE d.organization_id IS DISTINCT FROM p.organization_id "
            "ORDER BY d.id LIMIT 1"
        )
    ).mappings().first()
    if inconsistent_document is not None:
        raise RuntimeError(
            "Cannot enforce document/project tenant integrity: document "
            f"{inconsistent_document['document_id']} belongs to organization "
            f"{inconsistent_document['document_organization_id']} but its project "
            "belongs to organization "
            f"{inconsistent_document['project_organization_id']}. Repair the row "
            "before upgrading."
        )

    simple_project_foreign_key = _documents_project_foreign_key_name()
    op.create_unique_constraint(
        "uq_projects_id_organization_id",
        "projects",
        ["id", "organization_id"],
    )
    op.drop_constraint(
        simple_project_foreign_key,
        "documents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_documents_project_organization",
        "documents",
        "projects",
        ["project_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="RESTRICT",
    )

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
    op.drop_constraint(
        "fk_documents_project_organization",
        "documents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "documents_project_id_fkey",
        "documents",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_projects_id_organization_id",
        "projects",
        type_="unique",
    )
