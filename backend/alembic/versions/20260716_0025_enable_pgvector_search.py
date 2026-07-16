"""enable pgvector for complete ordinance search

Revision ID: 20260716_0025
Revises: 20260716_0024
Create Date: 2026-07-16 19:30:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260716_0025"
down_revision: Union[str, None] = "20260716_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # The extension is a database capability that may be shared by other
    # schemas. Removing it during an application downgrade is unsafe; leaving
    # it installed has no effect on the schema at revision 0024.
    pass
