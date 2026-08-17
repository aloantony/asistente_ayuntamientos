"""create administration and communications

Revision ID: 20260805_0038
Revises: 20260804_0037
Create Date: 2026-08-05 16:30:48.405031
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260805_0038"
down_revision: Union[str, None] = "20260804_0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('municipal_contracts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('procedure_type', sa.String(length=30), server_default='minor', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('base_amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('awarded_amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('awarded_to', sa.String(length=255), nullable=True),
    sa.Column('published_on', sa.Date(), nullable=True),
    sa.Column('awarded_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("procedure_type in ('minor', 'open', 'negotiated', 'framework', 'other')", name='ck_municipal_contracts_procedure'),
    sa.CheckConstraint("status <> 'awarded' or (awarded_to is not null and awarded_amount is not null)", name='ck_municipal_contracts_award_details'),
    sa.CheckConstraint("status in ('draft', 'published', 'awarded', 'executed', 'cancelled')", name='ck_municipal_contracts_status'),
    sa.CheckConstraint('awarded_amount is null or awarded_amount >= 0', name='ck_municipal_contracts_awarded_amount'),
    sa.CheckConstraint('base_amount is null or base_amount >= 0', name='ck_municipal_contracts_base_amount'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'reference', name='uq_municipal_contracts_org_reference')
    )
    op.create_index('ix_municipal_contracts_org_status', 'municipal_contracts', ['organization_id', 'status', 'published_on'], unique=False)
    op.create_table('municipal_grants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('funder', sa.String(length=255), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
    sa.Column('requested_amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('granted_amount', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('application_deadline', sa.Date(), nullable=True),
    sa.Column('resolved_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(title) <> ''", name='ck_municipal_grants_title'),
    sa.CheckConstraint("status in ('open', 'applied', 'granted', 'denied', 'settled')", name='ck_municipal_grants_status'),
    sa.CheckConstraint('granted_amount is null or granted_amount >= 0', name='ck_municipal_grants_granted'),
    sa.CheckConstraint('requested_amount is null or requested_amount >= 0', name='ck_municipal_grants_requested'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_municipal_grants_org_status', 'municipal_grants', ['organization_id', 'status', 'id'], unique=False)
    op.create_table('municipal_licences',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('applicant', sa.String(length=255), nullable=False),
    sa.Column('address', sa.String(length=255), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='requested', nullable=False),
    sa.Column('requested_on', sa.Date(), nullable=False),
    sa.Column('resolved_on', sa.Date(), nullable=True),
    sa.Column('fee_amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status in ('granted', 'denied')) = (resolved_on is not null)", name='ck_municipal_licences_resolution_presence'),
    sa.CheckConstraint("kind in ('works', 'opening', 'occupancy', 'environmental', 'other')", name='ck_municipal_licences_kind'),
    sa.CheckConstraint("status in ('requested', 'in_review', 'granted', 'denied', 'expired', 'withdrawn')", name='ck_municipal_licences_status'),
    sa.CheckConstraint('fee_amount is null or fee_amount >= 0', name='ck_municipal_licences_fee'),
    sa.CheckConstraint('resolved_on is null or resolved_on >= requested_on', name='ck_municipal_licences_resolution_order'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'reference', name='uq_municipal_licences_org_reference')
    )
    op.create_index('ix_municipal_licences_org_status_requested', 'municipal_licences', ['organization_id', 'status', 'requested_on'], unique=False)
    op.create_table('municipal_news',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=160), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('summary', sa.String(length=500), nullable=True),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('published_on', sa.Date(), nullable=True),
    sa.Column('publish_to_sede', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'published') = (published_on is not null)", name='ck_municipal_news_published_on'),
    sa.CheckConstraint("btrim(title) <> ''", name='ck_municipal_news_title'),
    sa.CheckConstraint("status in ('draft', 'published', 'archived')", name='ck_municipal_news_status'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_municipal_news_org_slug')
    )
    op.create_index(op.f('ix_municipal_news_created_by_id'), 'municipal_news', ['created_by_id'], unique=False)
    op.create_index('ix_municipal_news_org_status_published', 'municipal_news', ['organization_id', 'status', 'published_on'], unique=False)
    op.create_table('municipal_notices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='bando', nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('published_on', sa.Date(), nullable=True),
    sa.Column('expires_on', sa.Date(), nullable=True),
    sa.Column('publish_to_sede', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(title) <> ''", name='ck_municipal_notices_title'),
    sa.CheckConstraint("kind in ('bando', 'edicto', 'convocatoria', 'other')", name='ck_municipal_notices_kind'),
    sa.CheckConstraint("status <> 'published' or published_on is not null", name='ck_municipal_notices_published_on'),
    sa.CheckConstraint("status in ('draft', 'published', 'withdrawn', 'expired')", name='ck_municipal_notices_status'),
    sa.CheckConstraint('expires_on is null or published_on is null or expires_on >= published_on', name='ck_municipal_notices_period'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_municipal_notices_id_org')
    )
    op.create_index(op.f('ix_municipal_notices_created_by_id'), 'municipal_notices', ['created_by_id'], unique=False)
    op.create_index('ix_municipal_notices_org_status_published', 'municipal_notices', ['organization_id', 'status', 'published_on'], unique=False)
    op.create_table('municipal_procedures',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('channel', sa.String(length=20), server_default='in_person', nullable=False),
    sa.Column('deadline_days', sa.Integer(), nullable=True),
    sa.Column('fee_description', sa.String(length=255), nullable=True),
    sa.Column('publish_to_sede', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(name) <> ''", name='ck_municipal_procedures_name'),
    sa.CheckConstraint("channel in ('in_person', 'online', 'both')", name='ck_municipal_procedures_channel'),
    sa.CheckConstraint("status in ('active', 'archived')", name='ck_municipal_procedures_status'),
    sa.CheckConstraint('deadline_days is null or deadline_days >= 0', name='ck_municipal_procedures_deadline'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_municipal_procedures_org_slug')
    )
    op.create_index('ix_municipal_procedures_org_status', 'municipal_procedures', ['organization_id', 'status', 'name'], unique=False)
    op.create_table('office_hours',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('office_name', sa.String(length=255), nullable=False),
    sa.Column('weekday', sa.String(length=20), nullable=False),
    sa.Column('opens_at', sa.Integer(), nullable=False),
    sa.Column('closes_at', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(office_name) <> ''", name='ck_office_hours_office_name'),
    sa.CheckConstraint("weekday in ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')", name='ck_office_hours_weekday'),
    sa.CheckConstraint('closes_at > opens_at', name='ck_office_hours_range'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_office_hours_org_weekday', 'office_hours', ['organization_id', 'weekday', 'opens_at'], unique=False)
    op.create_table('transparency_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('area', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('reference_period', sa.String(length=100), nullable=True),
    sa.Column('published_on', sa.Date(), nullable=True),
    sa.Column('publish_to_sede', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("area in ('institutional', 'regulatory', 'economic', 'contracts', 'grants', 'other')", name='ck_transparency_items_area'),
    sa.CheckConstraint("btrim(title) <> ''", name='ck_transparency_items_title'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_transparency_items_org_area', 'transparency_items', ['organization_id', 'area', 'id'], unique=False)
    op.create_table('municipal_notice_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('notice_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=20), nullable=False),
    sa.Column('from_status', sa.String(length=20), nullable=True),
    sa.Column('to_status', sa.String(length=20), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("event_type in ('created', 'updated', 'published', 'withdrawn')", name='ck_municipal_notice_events_type'),
    sa.CheckConstraint("from_status is null or from_status in ('draft', 'published', 'withdrawn', 'expired')", name='ck_municipal_notice_events_from_status'),
    sa.CheckConstraint("to_status is null or to_status in ('draft', 'published', 'withdrawn', 'expired')", name='ck_municipal_notice_events_to_status'),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['notice_id', 'organization_id'], ['municipal_notices.id', 'municipal_notices.organization_id'], name='fk_municipal_notice_events_notice_org', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_municipal_notice_events_actor_id'), 'municipal_notice_events', ['actor_id'], unique=False)
    op.create_index('ix_municipal_notice_events_notice', 'municipal_notice_events', ['notice_id', 'id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_municipal_notice_events_notice', table_name='municipal_notice_events')
    op.drop_index(op.f('ix_municipal_notice_events_actor_id'), table_name='municipal_notice_events')
    op.drop_table('municipal_notice_events')
    op.drop_index('ix_transparency_items_org_area', table_name='transparency_items')
    op.drop_table('transparency_items')
    op.drop_index('ix_office_hours_org_weekday', table_name='office_hours')
    op.drop_table('office_hours')
    op.drop_index('ix_municipal_procedures_org_status', table_name='municipal_procedures')
    op.drop_table('municipal_procedures')
    op.drop_index('ix_municipal_notices_org_status_published', table_name='municipal_notices')
    op.drop_index(op.f('ix_municipal_notices_created_by_id'), table_name='municipal_notices')
    op.drop_table('municipal_notices')
    op.drop_index('ix_municipal_news_org_status_published', table_name='municipal_news')
    op.drop_index(op.f('ix_municipal_news_created_by_id'), table_name='municipal_news')
    op.drop_table('municipal_news')
    op.drop_index('ix_municipal_licences_org_status_requested', table_name='municipal_licences')
    op.drop_table('municipal_licences')
    op.drop_index('ix_municipal_grants_org_status', table_name='municipal_grants')
    op.drop_table('municipal_grants')
    op.drop_index('ix_municipal_contracts_org_status', table_name='municipal_contracts')
    op.drop_table('municipal_contracts')
