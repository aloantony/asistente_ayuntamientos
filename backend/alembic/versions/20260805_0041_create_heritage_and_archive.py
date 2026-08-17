"""create heritage and archive

Revision ID: 20260805_0041
Revises: 20260805_0040
Create Date: 2026-08-17 14:38:00.320647
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260805_0041"
down_revision: Union[str, None] = "20260805_0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('heritage_assets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=140), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='building', nullable=False),
    sa.Column('period', sa.String(length=120), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('protection_level', sa.String(length=20), server_default='none', nullable=False),
    sa.Column('protection_reference', sa.String(length=255), nullable=True),
    sa.Column('conservation_state', sa.String(length=20), server_default='unknown', nullable=False),
    sa.Column('last_survey_date', sa.Date(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(name) <> ''", name='ck_heritage_assets_name'),
    sa.CheckConstraint("conservation_state in ('good', 'fair', 'poor', 'ruin', 'unknown')", name='ck_heritage_assets_conservation'),
    sa.CheckConstraint("kind in ('building', 'archaeological', 'natural', 'movable', 'intangible', 'other')", name='ck_heritage_assets_kind'),
    sa.CheckConstraint("protection_level = 'none' or protection_reference is not null", name='ck_heritage_assets_protection_reference'),
    sa.CheckConstraint("protection_level in ('none', 'local', 'regional', 'bic', 'unesco')", name='ck_heritage_assets_protection'),
    sa.ForeignKeyConstraint(['location_id'], ['geo_locations.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_heritage_assets_id_org'),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_heritage_assets_org_slug')
    )
    op.create_index(op.f('ix_heritage_assets_location_id'), 'heritage_assets', ['location_id'], unique=False)
    op.create_index('ix_heritage_assets_org_kind', 'heritage_assets', ['organization_id', 'kind', 'name'], unique=False)
    op.create_table('archive_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='document', nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('start_year', sa.Integer(), nullable=True),
    sa.Column('end_year', sa.Integer(), nullable=True),
    sa.Column('physical_location', sa.String(length=255), nullable=True),
    sa.Column('conservation_state', sa.String(length=20), server_default='unknown', nullable=False),
    sa.Column('digitisation_state', sa.String(length=20), server_default='not_digitised', nullable=False),
    sa.Column('document_id', sa.Integer(), nullable=True),
    sa.Column('heritage_asset_id', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(title) <> ''", name='ck_archive_items_title'),
    sa.CheckConstraint("conservation_state in ('good', 'fair', 'poor', 'ruin', 'unknown')", name='ck_archive_items_conservation'),
    sa.CheckConstraint("digitisation_state <> 'digitised' or document_id is not null", name='ck_archive_items_digitised_document'),
    sa.CheckConstraint("digitisation_state in ('not_digitised', 'in_progress', 'digitised')", name='ck_archive_items_digitisation'),
    sa.CheckConstraint("kind in ('document', 'photograph', 'map', 'book', 'audio', 'video', 'other')", name='ck_archive_items_kind'),
    sa.CheckConstraint('end_year is null or start_year is null or end_year >= start_year', name='ck_archive_items_year_range'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['heritage_asset_id'], ['heritage_assets.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'reference', name='uq_archive_items_org_reference')
    )
    op.create_index(op.f('ix_archive_items_document_id'), 'archive_items', ['document_id'], unique=False)
    op.create_index(op.f('ix_archive_items_heritage_asset_id'), 'archive_items', ['heritage_asset_id'], unique=False)
    op.create_index('ix_archive_items_org_kind', 'archive_items', ['organization_id', 'kind', 'title'], unique=False)
    op.create_index('ix_archive_items_org_years', 'archive_items', ['organization_id', 'start_year'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_archive_items_org_years', table_name='archive_items')
    op.drop_index('ix_archive_items_org_kind', table_name='archive_items')
    op.drop_index(op.f('ix_archive_items_heritage_asset_id'), table_name='archive_items')
    op.drop_index(op.f('ix_archive_items_document_id'), table_name='archive_items')
    op.drop_table('archive_items')
    op.drop_index('ix_heritage_assets_org_kind', table_name='heritage_assets')
    op.drop_index(op.f('ix_heritage_assets_location_id'), table_name='heritage_assets')
    op.drop_table('heritage_assets')
