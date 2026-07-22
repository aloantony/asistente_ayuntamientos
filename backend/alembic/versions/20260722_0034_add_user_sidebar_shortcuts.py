"""add per-user sidebar shortcuts

Revision ID: 20260722_0034
Revises: 20260717_0033
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260722_0034"
down_revision: str | None = "20260717_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("sidebar_shortcut_ids", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated_users = bind.execute(
        sa.text(
            "SELECT count(*) FROM users "
            "WHERE sidebar_shortcut_ids IS NOT NULL"
        )
    ).scalar_one()
    if int(populated_users):
        raise RuntimeError(
            "cannot downgrade: user sidebar shortcut preferences exist "
            f"(users={populated_users})"
        )
    op.drop_column("users", "sidebar_shortcut_ids")
