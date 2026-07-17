"""enable PostGIS platform capability

Revision ID: 20260717_0030
Revises: 20260717_0029
Create Date: 2026-07-17

PostGIS is a shared database capability. This revision enables it without
changing the existing geo_locations model or introducing SIUR feature tables.
The extension is deliberately retained on downgrade, matching the existing
pgvector migration policy and avoiding destructive removal of shared types.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0030"
down_revision: str | None = "20260717_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    available_version = connection.execute(
        sa.text(
            "SELECT default_version FROM pg_available_extensions "
            "WHERE name = 'postgis'"
        )
    ).scalar_one_or_none()
    if available_version is None:
        raise RuntimeError(
            "PostGIS extension files are unavailable. Deploy the reviewed "
            "PostgreSQL 17 image containing postgresql-17-postgis-3 before "
            "running migration 20260717_0030."
        )

    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    installed_version = connection.execute(
        sa.text(
            "SELECT extversion FROM pg_extension WHERE extname = 'postgis'"
        )
    ).scalar_one_or_none()
    if installed_version is None:
        raise RuntimeError(
            "PostGIS did not become available after CREATE EXTENSION"
        )


def downgrade() -> None:
    # Keep shared spatial types/functions and any future dependent data. A
    # deliberate extension removal, if ever required, needs a separate audited
    # operational procedure rather than an application-schema downgrade.
    pass
