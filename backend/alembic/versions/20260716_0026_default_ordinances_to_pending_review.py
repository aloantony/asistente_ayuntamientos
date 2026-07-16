"""default new ordinances to pending review

Revision ID: 20260716_0026
Revises: 20260716_0025
Create Date: 2026-07-16 22:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260716_0026"
down_revision: Union[str, None] = "20260716_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing decisions remain immutable; only future inserts that omit the
    # column must enter the human-review queue.
    op.alter_column(
        "ordinances",
        "curation_status",
        existing_type=sa.String(length=30),
        existing_nullable=False,
        server_default=sa.text("'pending_review'"),
    )


def downgrade() -> None:
    op.alter_column(
        "ordinances",
        "curation_status",
        existing_type=sa.String(length=30),
        existing_nullable=False,
        server_default=sa.text("'approved'"),
    )
