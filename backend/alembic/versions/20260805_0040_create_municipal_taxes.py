"""create municipal taxes

Revision ID: 20260805_0040
Revises: 20260805_0039
Create Date: 2026-08-17 13:56:25.017894
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260805_0040"
down_revision: Union[str, None] = "20260805_0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('municipal_taxes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='tax', nullable=False),
    sa.Column('rate_kind', sa.String(length=20), server_default='tariff', nullable=False),
    sa.Column('rate_value', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('rate_description', sa.String(length=255), nullable=True),
    sa.Column('taxable_base', sa.String(length=255), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('ordinance_id', sa.Integer(), nullable=True),
    sa.Column('publish_to_sede', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(rate_kind = 'tariff') = (rate_value is null)", name='ck_municipal_taxes_rate_shape'),
    sa.CheckConstraint("btrim(name) <> ''", name='ck_municipal_taxes_name'),
    sa.CheckConstraint("kind in ('tax', 'fee', 'special_levy', 'price', 'other')", name='ck_municipal_taxes_kind'),
    sa.CheckConstraint("rate_kind in ('percentage', 'fixed_amount', 'tariff')", name='ck_municipal_taxes_rate_kind'),
    sa.CheckConstraint('rate_value is null or rate_value >= 0', name='ck_municipal_taxes_rate_value'),
    sa.ForeignKeyConstraint(['ordinance_id'], ['ordinances.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_municipal_taxes_org_slug')
    )
    op.create_index(op.f('ix_municipal_taxes_ordinance_id'), 'municipal_taxes', ['ordinance_id'], unique=False)
    op.create_index('ix_municipal_taxes_org_kind', 'municipal_taxes', ['organization_id', 'kind', 'name'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_municipal_taxes_org_kind', table_name='municipal_taxes')
    op.drop_index(op.f('ix_municipal_taxes_ordinance_id'), table_name='municipal_taxes')
    op.drop_table('municipal_taxes')
