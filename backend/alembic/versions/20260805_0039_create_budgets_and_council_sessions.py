"""create budgets and council sessions

Revision ID: 20260805_0039
Revises: 20260805_0038
Create Date: 2026-08-05 16:49:49.535891
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260805_0039"
down_revision: Union[str, None] = "20260805_0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('municipal_budgets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference_year', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('approved_on', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status <> 'draft') = (approved_on is not null)", name='ck_municipal_budgets_approved_on'),
    sa.CheckConstraint("status in ('draft', 'approved', 'executing', 'settled')", name='ck_municipal_budgets_status'),
    sa.CheckConstraint('reference_year between 1900 and 2200', name='ck_municipal_budgets_year'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_municipal_budgets_id_org'),
    sa.UniqueConstraint('organization_id', 'reference_year', name='uq_municipal_budgets_org_year')
    )
    op.create_index('ix_municipal_budgets_org_year', 'municipal_budgets', ['organization_id', 'reference_year'], unique=False)
    op.create_table('treasury_movements',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('concept', sa.String(length=255), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('moved_on', sa.Date(), nullable=False),
    sa.Column('account_label', sa.String(length=120), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(concept) <> ''", name='ck_treasury_movements_concept'),
    sa.CheckConstraint("direction in ('inflow', 'outflow')", name='ck_treasury_movements_direction'),
    sa.CheckConstraint('amount > 0', name='ck_treasury_movements_amount'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_treasury_movements_org_date', 'treasury_movements', ['organization_id', 'moved_on', 'id'], unique=False)
    op.create_table('budget_amendments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('budget_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=True),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('approved_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'approved') = (approved_on is not null)", name='ck_budget_amendments_approved_on'),
    sa.CheckConstraint("kind in ('credit_transfer', 'extraordinary_credit', 'supplement', 'other')", name='ck_budget_amendments_kind'),
    sa.CheckConstraint("status in ('draft', 'approved', 'rejected')", name='ck_budget_amendments_status'),
    sa.CheckConstraint('amount <> 0', name='ck_budget_amendments_amount'),
    sa.ForeignKeyConstraint(['budget_id', 'organization_id'], ['municipal_budgets.id', 'municipal_budgets.organization_id'], name='fk_budget_amendments_budget_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_budget_amendments_budget_status', 'budget_amendments', ['budget_id', 'status', 'id'], unique=False)
    op.create_table('budget_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('budget_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(code) <> ''", name='ck_budget_lines_code'),
    sa.CheckConstraint("kind in ('income', 'expense')", name='ck_budget_lines_kind'),
    sa.CheckConstraint('amount >= 0', name='ck_budget_lines_amount'),
    sa.ForeignKeyConstraint(['budget_id', 'organization_id'], ['municipal_budgets.id', 'municipal_budgets.organization_id'], name='fk_budget_lines_budget_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('budget_id', 'code', name='uq_budget_lines_budget_code'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_budget_lines_id_org')
    )
    op.create_index('ix_budget_lines_budget_kind', 'budget_lines', ['budget_id', 'kind', 'code'], unique=False)
    op.create_table('budget_expenses',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('line_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('concept', sa.String(length=255), nullable=False),
    sa.Column('supplier', sa.String(length=255), nullable=True),
    sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('incurred_on', sa.Date(), nullable=False),
    sa.Column('invoice_reference', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(concept) <> ''", name='ck_budget_expenses_concept'),
    sa.CheckConstraint('amount >= 0', name='ck_budget_expenses_amount'),
    sa.ForeignKeyConstraint(['line_id', 'organization_id'], ['budget_lines.id', 'budget_lines.organization_id'], name='fk_budget_expenses_line_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_budget_expenses_line_date', 'budget_expenses', ['line_id', 'incurred_on', 'id'], unique=False)
    op.create_table('council_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='ordinary', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='convened', nullable=False),
    sa.Column('held_on', sa.Date(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('minutes_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('minutes_document_id', sa.Integer(), nullable=True),
    sa.Column('publish_to_sede', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind in ('ordinary', 'extraordinary', 'urgent', 'constitutive')", name='ck_council_sessions_kind'),
    sa.CheckConstraint("minutes_status <> 'approved' or minutes_document_id is not null", name='ck_council_sessions_minutes_document'),
    sa.CheckConstraint("minutes_status in ('pending', 'draft', 'approved')", name='ck_council_sessions_minutes_status'),
    sa.CheckConstraint("status <> 'cancelled' or minutes_status = 'pending'", name='ck_council_sessions_cancelled_minutes'),
    sa.CheckConstraint("status in ('convened', 'held', 'cancelled')", name='ck_council_sessions_status'),
    sa.ForeignKeyConstraint(['minutes_document_id'], ['documents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('id', 'organization_id', name='uq_council_sessions_id_org'),
    sa.UniqueConstraint('organization_id', 'held_on', 'kind', name='uq_council_sessions_org_date_kind')
    )
    op.create_index(op.f('ix_council_sessions_minutes_document_id'), 'council_sessions', ['minutes_document_id'], unique=False)
    op.create_index('ix_council_sessions_org_date', 'council_sessions', ['organization_id', 'held_on', 'id'], unique=False)
    op.create_table('council_agenda_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('votes_in_favour', sa.Integer(), nullable=True),
    sa.Column('votes_against', sa.Integer(), nullable=True),
    sa.Column('abstentions', sa.Integer(), nullable=True),
    sa.Column('outcome', sa.String(length=120), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("btrim(title) <> ''", name='ck_council_agenda_items_title'),
    sa.CheckConstraint('abstentions is null or abstentions >= 0', name='ck_council_agenda_items_abstentions'),
    sa.CheckConstraint('position >= 1', name='ck_council_agenda_items_position'),
    sa.CheckConstraint('votes_against is null or votes_against >= 0', name='ck_council_agenda_items_votes_against'),
    sa.CheckConstraint('votes_in_favour is null or votes_in_favour >= 0', name='ck_council_agenda_items_votes_favour'),
    sa.ForeignKeyConstraint(['session_id', 'organization_id'], ['council_sessions.id', 'council_sessions.organization_id'], name='fk_council_agenda_items_session_org', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id', 'position', name='uq_council_agenda_items_session_position')
    )
    op.create_index('ix_council_agenda_items_session', 'council_agenda_items', ['session_id', 'position'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_council_agenda_items_session', table_name='council_agenda_items')
    op.drop_table('council_agenda_items')
    op.drop_index('ix_council_sessions_org_date', table_name='council_sessions')
    op.drop_index(op.f('ix_council_sessions_minutes_document_id'), table_name='council_sessions')
    op.drop_table('council_sessions')
    op.drop_index('ix_budget_expenses_line_date', table_name='budget_expenses')
    op.drop_table('budget_expenses')
    op.drop_index('ix_budget_lines_budget_kind', table_name='budget_lines')
    op.drop_table('budget_lines')
    op.drop_index('ix_budget_amendments_budget_status', table_name='budget_amendments')
    op.drop_table('budget_amendments')
    op.drop_index('ix_treasury_movements_org_date', table_name='treasury_movements')
    op.drop_table('treasury_movements')
    op.drop_index('ix_municipal_budgets_org_year', table_name='municipal_budgets')
    op.drop_table('municipal_budgets')
