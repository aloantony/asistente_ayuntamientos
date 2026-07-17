"""create municipal asset inventory

Revision ID: 20260715_0022
Revises: 20260713_0021
Create Date: 2026-07-15 00:22:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0022"
down_revision: Union[str, None] = "20260713_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TAXONOMY_CODE_PATTERN = r"^[a-z0-9]+([-_][a-z0-9]+)*$"


def upgrade() -> None:
    op.create_table(
        "municipal_asset_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
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
            f"code ~ '{TAXONOMY_CODE_PATTERN}'",
            name="ck_municipal_asset_categories_code",
        ),
        sa.CheckConstraint(
            "color is null or color ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_municipal_asset_categories_color",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_municipal_asset_categories_sort_order",
        ),
        sa.CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipal_asset_categories_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "code",
            name="uq_municipal_asset_categories_org_code",
        ),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_asset_categories_id_org",
        ),
    )
    op.create_index(
        "ix_municipal_asset_categories_org_status_sort",
        "municipal_asset_categories",
        ["organization_id", "status", "sort_order", "id"],
        unique=False,
    )
    for column_name in ("created_by_id", "updated_by_id"):
        op.create_index(
            op.f(f"ix_municipal_asset_categories_{column_name}"),
            "municipal_asset_categories",
            [column_name],
            unique=False,
        )

    op.create_table(
        "municipal_asset_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
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
            f"code ~ '{TAXONOMY_CODE_PATTERN}'",
            name="ck_municipal_asset_types_code",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_municipal_asset_types_sort_order",
        ),
        sa.CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipal_asset_types_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "organization_id"],
            [
                "municipal_asset_categories.id",
                "municipal_asset_categories.organization_id",
            ],
            name="fk_municipal_asset_types_category_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "category_id",
            "code",
            name="uq_municipal_asset_types_org_category_code",
        ),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_asset_types_id_org",
        ),
    )
    op.create_index(
        "ix_municipal_asset_types_org_status_sort",
        "municipal_asset_types",
        ["organization_id", "status", "sort_order", "id"],
        unique=False,
    )
    op.create_index(
        "ix_municipal_asset_types_category_sort",
        "municipal_asset_types",
        ["category_id", "sort_order", "id"],
        unique=False,
    )
    for column_name in ("created_by_id", "updated_by_id"):
        op.create_index(
            op.f(f"ix_municipal_asset_types_{column_name}"),
            "municipal_asset_types",
            [column_name],
            unique=False,
        )

    op.create_unique_constraint(
        "uq_organizations_id_municipality",
        "organizations",
        ["id", "municipality_id"],
    )
    op.create_unique_constraint(
        "uq_geo_locations_id_org_municipality",
        "geo_locations",
        ["id", "organization_id", "municipality_id"],
    )

    op.create_table(
        "municipal_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("municipality_id", sa.Integer(), nullable=False),
        sa.Column("asset_type_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("code", sa.String(length=100), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column(
            "condition_status",
            sa.String(length=30),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("material", sa.String(length=255), nullable=True),
        sa.Column("dimensions", sa.String(length=500), nullable=True),
        sa.Column("installed_on", sa.Date(), nullable=True),
        sa.Column("last_inspected_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
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
            "code is null or btrim(code) <> ''",
            name="ck_municipal_assets_code",
        ),
        sa.CheckConstraint(
            "status in ('active', 'inactive', 'retired', 'archived')",
            name="ck_municipal_assets_status",
        ),
        sa.CheckConstraint(
            "condition_status in ('good', 'fair', 'poor', 'unknown')",
            name="ck_municipal_assets_condition_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "municipality_id"],
            ["organizations.id", "organizations.municipality_id"],
            name="fk_municipal_assets_organization_municipality",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"],
            ["municipalities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_type_id", "organization_id"],
            [
                "municipal_asset_types.id",
                "municipal_asset_types.organization_id",
            ],
            name="fk_municipal_assets_asset_type_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["geo_locations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["location_id", "organization_id", "municipality_id"],
            [
                "geo_locations.id",
                "geo_locations.organization_id",
                "geo_locations.municipality_id",
            ],
            name="fk_municipal_assets_location_tenant",
            ondelete="NO ACTION",
            match="SIMPLE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "code",
            name="uq_municipal_assets_org_code",
        ),
    )
    op.create_index(
        "ix_municipal_assets_org_status_id",
        "municipal_assets",
        ["organization_id", "status", "id"],
        unique=False,
    )
    op.create_index(
        "ix_municipal_assets_org_type_id",
        "municipal_assets",
        ["organization_id", "asset_type_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_municipal_assets_municipality_id",
        "municipal_assets",
        ["municipality_id", "id"],
        unique=False,
    )
    for column_name in (
        "asset_type_id",
        "location_id",
        "created_by_id",
        "updated_by_id",
    ):
        op.create_index(
            op.f(f"ix_municipal_assets_{column_name}"),
            "municipal_assets",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    for column_name in reversed(
        (
            "asset_type_id",
            "location_id",
            "created_by_id",
            "updated_by_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_municipal_assets_{column_name}"),
            table_name="municipal_assets",
        )
    op.drop_index("ix_municipal_assets_municipality_id", table_name="municipal_assets")
    op.drop_index("ix_municipal_assets_org_type_id", table_name="municipal_assets")
    op.drop_index("ix_municipal_assets_org_status_id", table_name="municipal_assets")
    op.drop_table("municipal_assets")
    op.drop_constraint(
        "uq_geo_locations_id_org_municipality",
        "geo_locations",
        type_="unique",
    )
    op.drop_constraint(
        "uq_organizations_id_municipality",
        "organizations",
        type_="unique",
    )

    for column_name in reversed(("created_by_id", "updated_by_id")):
        op.drop_index(
            op.f(f"ix_municipal_asset_types_{column_name}"),
            table_name="municipal_asset_types",
        )
    op.drop_index("ix_municipal_asset_types_category_sort", table_name="municipal_asset_types")
    op.drop_index("ix_municipal_asset_types_org_status_sort", table_name="municipal_asset_types")
    op.drop_table("municipal_asset_types")

    for column_name in reversed(("created_by_id", "updated_by_id")):
        op.drop_index(
            op.f(f"ix_municipal_asset_categories_{column_name}"),
            table_name="municipal_asset_categories",
        )
    op.drop_index(
        "ix_municipal_asset_categories_org_status_sort",
        table_name="municipal_asset_categories",
    )
    op.drop_table("municipal_asset_categories")
