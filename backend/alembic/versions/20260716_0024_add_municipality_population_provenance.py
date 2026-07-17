"""add municipality population provenance

Revision ID: 20260716_0024
Revises: 20260715_0023
Create Date: 2026-07-16 18:55:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260716_0024"
down_revision: Union[str, None] = "20260715_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "municipalities",
        sa.Column("population_reference_year", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "municipalities",
        sa.Column("population_source_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "municipalities",
        sa.Column("population_source_sha256", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_municipalities_population_reference_year",
        "municipalities",
        "population_reference_year is null "
        "or population_reference_year between 1900 and 9999",
    )
    op.create_check_constraint(
        "ck_municipalities_population_provenance_complete",
        "municipalities",
        "num_nonnulls(population_reference_year, population_source_url, "
        "population_source_sha256) in (0, 3)",
    )
    op.create_check_constraint(
        "ck_municipalities_population_provenance_has_population",
        "municipalities",
        "population_reference_year is null or population is not null",
    )
    op.create_check_constraint(
        "ck_municipalities_population_source_url",
        "municipalities",
        "population_source_url is null "
        "or population_source_url like 'https://%'",
    )
    op.create_check_constraint(
        "ck_municipalities_population_source_sha256",
        "municipalities",
        "population_source_sha256 is null "
        "or population_source_sha256 ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_municipalities_population_source_sha256",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_population_source_url",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_population_provenance_has_population",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_population_provenance_complete",
        "municipalities",
        type_="check",
    )
    op.drop_constraint(
        "ck_municipalities_population_reference_year",
        "municipalities",
        type_="check",
    )
    op.drop_column("municipalities", "population_source_sha256")
    op.drop_column("municipalities", "population_source_url")
    op.drop_column("municipalities", "population_reference_year")
