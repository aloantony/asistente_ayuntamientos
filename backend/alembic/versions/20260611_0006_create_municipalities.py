"""create municipalities

Revision ID: 20260611_0006
Revises: 20260610_0005
Create Date: 2026-06-11 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260611_0006"
down_revision: Union[str, None] = "20260610_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "municipalities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("province", sa.String(length=255), nullable=False),
        sa.Column("autonomous_community", sa.String(length=255), nullable=False),
        sa.Column(
            "country",
            sa.String(length=100),
            server_default="España",
            nullable=False,
        ),
        sa.Column("ine_code", sa.String(length=20), nullable=True),
        sa.Column("population", sa.Integer(), nullable=True),
        sa.Column("surface_km2", sa.Float(), nullable=True),
        sa.Column("density", sa.Float(), nullable=True),
        sa.Column("postal_codes", sa.Text(), nullable=True),
        sa.Column(
            "municipality_type",
            sa.String(length=50),
            server_default="municipality",
            nullable=False,
        ),
        sa.Column(
            "rural_urban_profile",
            sa.String(length=50),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("economic_profile", sa.Text(), nullable=True),
        sa.Column("tourism_profile", sa.Text(), nullable=True),
        sa.Column("geographic_notes", sa.Text(), nullable=True),
        sa.Column("administrative_notes", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="active",
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipalities_status",
        ),
        sa.CheckConstraint(
            "municipality_type in ('municipality', 'minor_local_entity', 'district', 'other')",
            name="ck_municipalities_municipality_type",
        ),
        sa.CheckConstraint(
            "rural_urban_profile in ('rural', 'semi_rural', 'urban', 'mixed', 'unknown')",
            name="ck_municipalities_rural_urban_profile",
        ),
        sa.CheckConstraint(
            "population is null or population >= 0",
            name="ck_municipalities_population_non_negative",
        ),
        sa.CheckConstraint(
            "surface_km2 is null or surface_km2 >= 0",
            name="ck_municipalities_surface_km2_non_negative",
        ),
        sa.CheckConstraint(
            "density is null or density >= 0",
            name="ck_municipalities_density_non_negative",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_municipalities_name"),
        "municipalities",
        ["name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_municipalities_province"),
        "municipalities",
        ["province"],
        unique=False,
    )
    op.create_index(
        op.f("ix_municipalities_autonomous_community"),
        "municipalities",
        ["autonomous_community"],
        unique=False,
    )
    op.create_index(
        op.f("ix_municipalities_ine_code"),
        "municipalities",
        ["ine_code"],
        unique=True,
    )
    op.create_index(
        op.f("ix_municipalities_status"),
        "municipalities",
        ["status"],
        unique=False,
    )

    op.add_column(
        "organizations",
        sa.Column("municipality_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_organizations_municipality_id"),
        "organizations",
        ["municipality_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_organizations_municipality_id_municipalities",
        "organizations",
        "municipalities",
        ["municipality_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_organizations_municipality_id_municipalities",
        "organizations",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_organizations_municipality_id"),
        table_name="organizations",
    )
    op.drop_column("organizations", "municipality_id")

    op.drop_index(op.f("ix_municipalities_status"), table_name="municipalities")
    op.drop_index(op.f("ix_municipalities_ine_code"), table_name="municipalities")
    op.drop_index(
        op.f("ix_municipalities_autonomous_community"),
        table_name="municipalities",
    )
    op.drop_index(op.f("ix_municipalities_province"), table_name="municipalities")
    op.drop_index(op.f("ix_municipalities_name"), table_name="municipalities")
    op.drop_table("municipalities")
