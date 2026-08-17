"""create municipal data series

Revision ID: 20260804_0036
Revises: 20260804_0035
Create Date: 2026-08-04 22:33:47.327701
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260804_0036"
down_revision: Union[str, None] = "20260804_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('climate_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference_year', sa.Integer(), nullable=False),
    sa.Column('reference_month', sa.Integer(), nullable=True),
    sa.Column('avg_temperature_c', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('min_temperature_c', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('max_temperature_c', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('precipitation_mm', sa.Numeric(precision=7, scale=2), nullable=True),
    sa.Column('source', sa.String(length=20), server_default='municipal', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source in ('municipal', 'ine', 'aemet', 'other')", name='ck_climate_records_source'),
    sa.CheckConstraint('min_temperature_c is null or max_temperature_c is null or min_temperature_c <= max_temperature_c', name='ck_climate_records_temperature_range'),
    sa.CheckConstraint('precipitation_mm is null or precipitation_mm >= 0', name='ck_climate_records_precipitation'),
    sa.CheckConstraint('reference_month is null or reference_month between 1 and 12', name='ck_climate_records_month'),
    sa.CheckConstraint('reference_year between 1900 and 2200', name='ck_climate_records_year'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'reference_year', 'reference_month', name='uq_climate_records_org_period')
    )
    op.create_index('ix_climate_records_org_period', 'climate_records', ['organization_id', 'reference_year', 'reference_month'], unique=False)
    op.create_table('household_stats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference_year', sa.Integer(), nullable=False),
    sa.Column('total_dwellings', sa.Integer(), nullable=False),
    sa.Column('primary_dwellings', sa.Integer(), nullable=True),
    sa.Column('secondary_dwellings', sa.Integer(), nullable=True),
    sa.Column('empty_dwellings', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=20), server_default='municipal', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source in ('municipal', 'ine', 'aemet', 'other')", name='ck_household_stats_source'),
    sa.CheckConstraint('empty_dwellings is null or empty_dwellings >= 0', name='ck_household_stats_empty'),
    sa.CheckConstraint('primary_dwellings is null or primary_dwellings >= 0', name='ck_household_stats_primary'),
    sa.CheckConstraint('reference_year between 1900 and 2200', name='ck_household_stats_year'),
    sa.CheckConstraint('secondary_dwellings is null or secondary_dwellings >= 0', name='ck_household_stats_secondary'),
    sa.CheckConstraint('total_dwellings >= 0', name='ck_household_stats_total'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'reference_year', name='uq_household_stats_org_year')
    )
    op.create_index('ix_household_stats_org_year', 'household_stats', ['organization_id', 'reference_year'], unique=False)
    op.create_table('padron_annual_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference_year', sa.Integer(), nullable=False),
    sa.Column('population', sa.Integer(), nullable=False),
    sa.Column('men', sa.Integer(), nullable=True),
    sa.Column('women', sa.Integer(), nullable=True),
    sa.Column('births', sa.Integer(), nullable=True),
    sa.Column('deaths', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=20), server_default='municipal', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source in ('municipal', 'ine', 'aemet', 'other')", name='ck_padron_annual_records_source'),
    sa.CheckConstraint('births is null or births >= 0', name='ck_padron_annual_records_births'),
    sa.CheckConstraint('deaths is null or deaths >= 0', name='ck_padron_annual_records_deaths'),
    sa.CheckConstraint('men is null or men >= 0', name='ck_padron_annual_records_men'),
    sa.CheckConstraint('men is null or women is null or men + women <= population', name='ck_padron_annual_records_breakdown'),
    sa.CheckConstraint('population >= 0', name='ck_padron_annual_records_population'),
    sa.CheckConstraint('reference_year between 1900 and 2200', name='ck_padron_annual_records_year'),
    sa.CheckConstraint('women is null or women >= 0', name='ck_padron_annual_records_women'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'reference_year', name='uq_padron_annual_records_org_year')
    )
    op.create_index('ix_padron_annual_records_org_year', 'padron_annual_records', ['organization_id', 'reference_year'], unique=False)
    op.create_table('utility_supplies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('origin', sa.String(length=255), nullable=True),
    sa.Column('treatment', sa.String(length=30), server_default='none', nullable=False),
    sa.Column('last_analysis_date', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(name) <> ''", name='ck_utility_supplies_name'),
    sa.CheckConstraint("treatment in ('none', 'chlorination', 'filtration', 'osmosis', 'other')", name='ck_utility_supplies_treatment'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_utility_supplies_id_org')
    )
    op.create_index('ix_utility_supplies_org', 'utility_supplies', ['organization_id', 'id'], unique=False)
    op.create_table('water_meters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('municipality_id', sa.Integer(), nullable=False),
    sa.Column('supply_id', sa.Integer(), nullable=True),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('code', sa.String(length=100), nullable=False),
    sa.Column('address', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('installed_on', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(code) <> ''", name='ck_water_meters_code'),
    sa.CheckConstraint("status in ('active', 'inactive', 'removed')", name='ck_water_meters_status'),
    sa.ForeignKeyConstraint(['location_id', 'organization_id', 'municipality_id'], ['geo_locations.id', 'geo_locations.organization_id', 'geo_locations.municipality_id'], name='fk_water_meters_location_tenant', ondelete='NO ACTION', initially='DEFERRED', deferrable=True, match='SIMPLE'),
    sa.ForeignKeyConstraint(['location_id'], ['geo_locations.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id', 'municipality_id'], ['organizations.id', 'organizations.municipality_id'], name='fk_water_meters_organization_municipality', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['supply_id', 'organization_id'], ['utility_supplies.id', 'utility_supplies.organization_id'], name='fk_water_meters_supply_org', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_water_meters_id_org'),
    sa.UniqueConstraint('organization_id', 'code', name='uq_water_meters_org_code')
    )
    op.create_index(op.f('ix_water_meters_location_id'), 'water_meters', ['location_id'], unique=False)
    op.create_index('ix_water_meters_org_status', 'water_meters', ['organization_id', 'status', 'id'], unique=False)
    op.create_table('water_meter_readings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('meter_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('read_on', sa.Date(), nullable=False),
    sa.Column('reading_m3', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('reading_m3 >= 0', name='ck_water_meter_readings_value'),
    sa.ForeignKeyConstraint(['meter_id', 'organization_id'], ['water_meters.id', 'water_meters.organization_id'], name='fk_water_meter_readings_meter_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('meter_id', 'read_on', name='uq_water_meter_readings_meter_date')
    )
    op.create_index('ix_water_meter_readings_meter_date', 'water_meter_readings', ['meter_id', 'read_on', 'id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_water_meter_readings_meter_date', table_name='water_meter_readings')
    op.drop_table('water_meter_readings')
    op.drop_index('ix_water_meters_org_status', table_name='water_meters')
    op.drop_index(op.f('ix_water_meters_location_id'), table_name='water_meters')
    op.drop_table('water_meters')
    op.drop_index('ix_utility_supplies_org', table_name='utility_supplies')
    op.drop_table('utility_supplies')
    op.drop_index('ix_padron_annual_records_org_year', table_name='padron_annual_records')
    op.drop_table('padron_annual_records')
    op.drop_index('ix_household_stats_org_year', table_name='household_stats')
    op.drop_table('household_stats')
    op.drop_index('ix_climate_records_org_period', table_name='climate_records')
    op.drop_table('climate_records')
