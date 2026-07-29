"""create town hall profile and content blocks

Revision ID: 20260729_0022
Revises: 20260713_0021
Create Date: 2026-07-29 00:22:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260729_0022"
down_revision: Union[str, None] = "20260713_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "municipal_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("weather_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("weather_location", sa.String(length=255), nullable=True),
        sa.Column("weather_latitude", sa.Float(), nullable=True),
        sa.Column("weather_longitude", sa.Float(), nullable=True),
        sa.Column("shield_storage_key", sa.String(length=1000), nullable=True),
        sa.Column("shield_content_type", sa.String(length=255), nullable=True),
        sa.Column("shield_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("weather_latitude is null or (weather_latitude >= -90 and weather_latitude <= 90)", name="ck_municipal_profiles_weather_latitude_range"),
        sa.CheckConstraint("weather_longitude is null or (weather_longitude >= -180 and weather_longitude <= 180)", name="ck_municipal_profiles_weather_longitude_range"),
        sa.CheckConstraint("shield_size_bytes is null or shield_size_bytes >= 0", name="ck_municipal_profiles_shield_size_bytes_positive"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_municipal_profiles_organization_id"), "municipal_profiles", ["organization_id"], unique=True)
    op.create_index(op.f("ix_municipal_profiles_updated_by_id"), "municipal_profiles", ["updated_by_id"], unique=False)

    op.create_table(
        "municipal_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("block_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("data_json", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("block_type in ('nav_section', 'nav_item', 'epigraph', 'section', 'item')", name="ck_municipal_blocks_block_type"),
        sa.CheckConstraint("status in ('active', 'archived')", name="ck_municipal_blocks_status"),
        sa.CheckConstraint("position >= 0", name="ck_municipal_blocks_position_positive"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_id"], ["municipal_blocks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_municipal_blocks_organization_id"), "municipal_blocks", ["organization_id"], unique=False)
    op.create_index(op.f("ix_municipal_blocks_parent_id"), "municipal_blocks", ["parent_id"], unique=False)
    op.create_index(op.f("ix_municipal_blocks_block_type"), "municipal_blocks", ["block_type"], unique=False)
    op.create_index(op.f("ix_municipal_blocks_created_by_id"), "municipal_blocks", ["created_by_id"], unique=False)
    op.create_index(op.f("ix_municipal_blocks_updated_by_id"), "municipal_blocks", ["updated_by_id"], unique=False)
    op.create_index("ix_municipal_blocks_org_parent_position", "municipal_blocks", ["organization_id", "parent_id", "position"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_municipal_blocks_org_parent_position", table_name="municipal_blocks")
    op.drop_index(op.f("ix_municipal_blocks_updated_by_id"), table_name="municipal_blocks")
    op.drop_index(op.f("ix_municipal_blocks_created_by_id"), table_name="municipal_blocks")
    op.drop_index(op.f("ix_municipal_blocks_block_type"), table_name="municipal_blocks")
    op.drop_index(op.f("ix_municipal_blocks_parent_id"), table_name="municipal_blocks")
    op.drop_index(op.f("ix_municipal_blocks_organization_id"), table_name="municipal_blocks")
    op.drop_table("municipal_blocks")
    op.drop_index(op.f("ix_municipal_profiles_updated_by_id"), table_name="municipal_profiles")
    op.drop_index(op.f("ix_municipal_profiles_organization_id"), table_name="municipal_profiles")
    op.drop_table("municipal_profiles")
