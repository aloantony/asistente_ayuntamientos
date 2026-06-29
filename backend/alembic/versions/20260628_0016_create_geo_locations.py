"""create geo locations

Revision ID: 20260628_0016
Revises: 20260626_0015
Create Date: 2026-06-28 00:16:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260628_0016"
down_revision: Union[str, None] = "20260626_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "geo_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("municipality_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("geometry_type", sa.String(length=30), server_default="point", nullable=False),
        sa.Column("geometry_json", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("address_text", sa.String(length=500), nullable=True),
        sa.Column("place_name", sa.String(length=255), nullable=True),
        sa.Column("cadastral_reference", sa.String(length=100), nullable=True),
        sa.Column("source", sa.String(length=30), server_default="user_provided", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("review_status", sa.String(length=30), server_default="proposed", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("geometry_type in ('point', 'line', 'polygon')", name="ck_geo_locations_geometry_type"),
        sa.CheckConstraint("source in ('user_provided', 'assistant_extracted', 'geocoded', 'imported', 'manual_review')", name="ck_geo_locations_source"),
        sa.CheckConstraint("review_status in ('draft', 'proposed', 'reviewed', 'rejected')", name="ck_geo_locations_review_status"),
        sa.CheckConstraint("latitude is null or (latitude >= -90 and latitude <= 90)", name="ck_geo_locations_latitude_range"),
        sa.CheckConstraint("longitude is null or (longitude >= -180 and longitude <= 180)", name="ck_geo_locations_longitude_range"),
        sa.CheckConstraint("confidence is null or (confidence >= 0 and confidence <= 1)", name="ck_geo_locations_confidence_range"),
        sa.CheckConstraint("geometry_type != 'point' or (latitude is not null and longitude is not null)", name="ck_geo_locations_point_coordinates"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["municipality_id"], ["municipalities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_geo_locations_organization_id"), "geo_locations", ["organization_id"], unique=False)
    op.create_index(op.f("ix_geo_locations_municipality_id"), "geo_locations", ["municipality_id"], unique=False)
    op.create_index("ix_geo_locations_latitude", "geo_locations", ["latitude"], unique=False)
    op.create_index("ix_geo_locations_longitude", "geo_locations", ["longitude"], unique=False)
    op.create_index(op.f("ix_geo_locations_created_by_id"), "geo_locations", ["created_by_id"], unique=False)
    op.create_index(op.f("ix_geo_locations_reviewed_by_id"), "geo_locations", ["reviewed_by_id"], unique=False)

    op.create_table(
        "entity_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=30), server_default="primary", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("entity_type in ('requirement', 'project')", name="ck_entity_locations_entity_type"),
        sa.CheckConstraint("role in ('primary', 'affected_area', 'reference')", name="ck_entity_locations_role"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["location_id"], ["geo_locations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_type", "entity_id", "role", name="uq_entity_locations_entity_role"),
    )
    op.create_index(op.f("ix_entity_locations_location_id"), "entity_locations", ["location_id"], unique=False)
    op.create_index(op.f("ix_entity_locations_entity_type"), "entity_locations", ["entity_type"], unique=False)
    op.create_index(op.f("ix_entity_locations_entity_id"), "entity_locations", ["entity_id"], unique=False)
    op.create_index(op.f("ix_entity_locations_created_by_id"), "entity_locations", ["created_by_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_entity_locations_created_by_id"), table_name="entity_locations")
    op.drop_index(op.f("ix_entity_locations_entity_id"), table_name="entity_locations")
    op.drop_index(op.f("ix_entity_locations_entity_type"), table_name="entity_locations")
    op.drop_index(op.f("ix_entity_locations_location_id"), table_name="entity_locations")
    op.drop_table("entity_locations")
    op.drop_index(op.f("ix_geo_locations_reviewed_by_id"), table_name="geo_locations")
    op.drop_index(op.f("ix_geo_locations_created_by_id"), table_name="geo_locations")
    op.drop_index("ix_geo_locations_longitude", table_name="geo_locations")
    op.drop_index("ix_geo_locations_latitude", table_name="geo_locations")
    op.drop_index(op.f("ix_geo_locations_municipality_id"), table_name="geo_locations")
    op.drop_index(op.f("ix_geo_locations_organization_id"), table_name="geo_locations")
    op.drop_table("geo_locations")
