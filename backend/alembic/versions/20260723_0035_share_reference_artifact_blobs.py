"""allow reference sources to share content-addressed blobs

Revision ID: 20260723_0035
Revises: 20260717_0034
Create Date: 2026-07-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260723_0035"
down_revision: str | None = "20260717_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The filesystem store is deliberately content-addressed globally.  The
    # same capabilities document or dataset can therefore be evidence for
    # several layer sources.  Artifact ownership remains source-scoped through
    # uq_reference_source_artifacts_content; only the physical blob is shared.
    op.drop_constraint(
        "uq_reference_source_artifacts_storage_key",
        "reference_source_artifacts",
        type_="unique",
    )
    op.create_index(
        "ix_reference_source_artifacts_storage",
        "reference_source_artifacts",
        ["storage_backend", "storage_key"],
        unique=False,
    )


def downgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT storage_backend, storage_key
            FROM reference_source_artifacts
            GROUP BY storage_backend, storage_key
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot restore unique artifact storage keys while shared "
            "content-addressed blob references exist"
        )
    op.drop_index(
        "ix_reference_source_artifacts_storage",
        table_name="reference_source_artifacts",
    )
    op.create_unique_constraint(
        "uq_reference_source_artifacts_storage_key",
        "reference_source_artifacts",
        ["storage_backend", "storage_key"],
    )
