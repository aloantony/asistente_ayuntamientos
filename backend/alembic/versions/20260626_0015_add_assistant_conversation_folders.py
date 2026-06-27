"""add assistant conversation folders

Revision ID: 20260626_0015
Revises: 20260618_0014
Create Date: 2026-06-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260626_0015"
down_revision: Union[str, None] = "20260618_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversation_folders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "created_by_id",
            "name",
            name="uq_assistant_conversation_folders_user_name",
        ),
    )
    op.create_index(
        op.f("ix_assistant_conversation_folders_created_by_id"),
        "assistant_conversation_folders",
        ["created_by_id"],
        unique=False,
    )
    op.add_column(
        "assistant_conversations",
        sa.Column("folder_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_assistant_conversations_folder_id"),
        "assistant_conversations",
        ["folder_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_assistant_conversations_folder_id_assistant_conversation_folders"),
        "assistant_conversations",
        "assistant_conversation_folders",
        ["folder_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_assistant_conversations_folder_id_assistant_conversation_folders"),
        "assistant_conversations",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_assistant_conversations_folder_id"),
        table_name="assistant_conversations",
    )
    op.drop_column("assistant_conversations", "folder_id")
    op.drop_index(
        op.f("ix_assistant_conversation_folders_created_by_id"),
        table_name="assistant_conversation_folders",
    )
    op.drop_table("assistant_conversation_folders")
