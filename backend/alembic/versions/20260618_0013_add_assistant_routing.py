"""add assistant message routing metadata

Revision ID: 20260618_0013
Revises: 20260617_0012
Create Date: 2026-06-18 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260618_0013"
down_revision: Union[str, None] = "20260617_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assistant_messages",
        sa.Column("agent_key", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "assistant_messages",
        sa.Column("routing", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_assistant_messages_agent_key"),
        "assistant_messages",
        ["agent_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_assistant_messages_agent_key"),
        table_name="assistant_messages",
    )
    op.drop_column("assistant_messages", "routing")
    op.drop_column("assistant_messages", "agent_key")
