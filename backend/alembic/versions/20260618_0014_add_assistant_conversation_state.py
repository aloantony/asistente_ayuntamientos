"""add assistant conversation state

Revision ID: 20260618_0014
Revises: 20260618_0013
Create Date: 2026-06-18 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260618_0014"
down_revision: Union[str, None] = "20260618_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column("state", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assistant_conversations", "state")
