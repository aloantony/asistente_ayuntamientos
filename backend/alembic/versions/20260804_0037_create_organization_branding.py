"""create organization branding

Revision ID: 20260804_0037
Revises: 20260804_0036
Create Date: 2026-08-04 22:45:49.728785
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260804_0037"
down_revision: Union[str, None] = "20260804_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('organization_branding',
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('crest_document_id', sa.Integer(), nullable=True),
    sa.Column('crest_alt_text', sa.String(length=255), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['crest_document_id'], ['documents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('organization_id')
    )
    op.create_index(op.f('ix_organization_branding_crest_document_id'), 'organization_branding', ['crest_document_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_organization_branding_crest_document_id'), table_name='organization_branding')
    op.drop_table('organization_branding')
