"""harden assistant message attachment authorization audit

Revision ID: 20260716_0027
Revises: 20260716_0026
Create Date: 2026-07-16

Revision 0026 may already be present in persistent databases, so it remains
immutable. Existing attachment rows are retained with an explicit
``legacy_unverified`` scope instead of fabricating authorization evidence.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0027"
down_revision: str | None = "20260716_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assistant_message_attachments",
        sa.Column(
            "authorization_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "assistant_message_attachments",
        sa.Column("authorized_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "assistant_message_attachments",
        sa.Column("authorized_organization_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "assistant_message_attachments",
        sa.Column("authorized_project_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "assistant_message_attachments",
        sa.Column(
            "authorized_document_checksum_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "assistant_message_attachments",
        sa.Column("authorization_scope", sa.String(length=50), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE assistant_message_attachments AS attachment "
            "SET authorization_checked_at = attachment.created_at, "
            "authorized_by_id = conversation.created_by_id, "
            "authorized_organization_id = document.organization_id, "
            "authorized_project_id = document.project_id, "
            "authorized_document_checksum_sha256 = document.checksum_sha256, "
            "authorization_scope = 'legacy_unverified' "
            "FROM assistant_messages AS message, "
            "assistant_conversations AS conversation, "
            "documents AS document "
            "WHERE message.id = attachment.message_id "
            "AND conversation.id = message.conversation_id "
            "AND document.id = attachment.document_id"
        )
    )

    incomplete_row = op.get_bind().execute(
        sa.text(
            "SELECT id FROM assistant_message_attachments "
            "WHERE authorization_checked_at IS NULL "
            "OR authorized_organization_id IS NULL "
            "OR authorized_project_id IS NULL "
            "OR authorized_document_checksum_sha256 IS NULL "
            "OR authorization_scope IS NULL "
            "ORDER BY id LIMIT 1"
        )
    ).scalar_one_or_none()
    if incomplete_row is not None:
        raise RuntimeError(
            "Cannot backfill attachment authorization audit for row "
            f"{incomplete_row}; repair its message/document references before "
            "upgrading."
        )

    op.alter_column(
        "assistant_message_attachments",
        "authorization_checked_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "assistant_message_attachments",
        "authorized_organization_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "assistant_message_attachments",
        "authorized_project_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "assistant_message_attachments",
        "authorized_document_checksum_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "assistant_message_attachments",
        "authorization_scope",
        existing_type=sa.String(length=50),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_assistant_message_attachments_authorization_scope",
        "assistant_message_attachments",
        "authorization_scope in ("
        "'superuser', 'documents.manage', 'documents.view', 'legacy_unverified'"
        ")",
    )
    op.create_foreign_key(
        "fk_assistant_message_attachments_authorized_by",
        "assistant_message_attachments",
        "users",
        ["authorized_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_assistant_message_attachments_authorized_by_id"),
        "assistant_message_attachments",
        ["authorized_by_id"],
        unique=False,
    )


def downgrade() -> None:
    attachment_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM assistant_message_attachments")
    ).scalar_one()
    if attachment_count:
        raise RuntimeError(
            "Refusing to downgrade 20260716_0027: "
            "assistant_message_attachments contains "
            f"{attachment_count} attachment audit row(s). Preserve or migrate "
            "those records before retrying."
        )

    op.drop_index(
        op.f("ix_assistant_message_attachments_authorized_by_id"),
        table_name="assistant_message_attachments",
    )
    op.drop_constraint(
        "fk_assistant_message_attachments_authorized_by",
        "assistant_message_attachments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_assistant_message_attachments_authorization_scope",
        "assistant_message_attachments",
        type_="check",
    )
    op.drop_column("assistant_message_attachments", "authorization_scope")
    op.drop_column(
        "assistant_message_attachments",
        "authorized_document_checksum_sha256",
    )
    op.drop_column("assistant_message_attachments", "authorized_project_id")
    op.drop_column("assistant_message_attachments", "authorized_organization_id")
    op.drop_column("assistant_message_attachments", "authorized_by_id")
    op.drop_column("assistant_message_attachments", "authorization_checked_at")
