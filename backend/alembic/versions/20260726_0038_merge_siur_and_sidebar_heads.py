"""merge SIUR mirror and sidebar shortcut migration heads

Revision ID: 20260726_0038
Revises: 20260723_0037, 20260722_0034
Create Date: 2026-07-26
"""

from collections.abc import Sequence


revision: str = "20260726_0038"
down_revision: tuple[str, str] = (
    "20260723_0037",
    "20260722_0034",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
