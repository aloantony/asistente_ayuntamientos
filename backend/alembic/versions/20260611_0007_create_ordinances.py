"""create ordinances

Revision ID: 20260611_0007
Revises: 20260611_0006
Create Date: 2026-06-11 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260611_0007"
down_revision: Union[str, None] = "20260611_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ordinances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("municipality_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("subtopic", sa.String(length=255), nullable=True),
        sa.Column("ordinance_type", sa.String(length=50), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("official_bulletin", sa.String(length=255), nullable=True),
        sa.Column("bulletin_number", sa.String(length=100), nullable=True),
        sa.Column("approval_date", sa.Date(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("legal_review_notes", sa.Text(), nullable=True),
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
            """
            ordinance_type in (
                'ordinance',
                'regulation',
                'bylaw',
                'tax_ordinance',
                'urban_planning',
                'other'
            )
            """,
            name="ck_ordinances_ordinance_type",
        ),
        sa.CheckConstraint(
            """
            status in (
                'active',
                'repealed',
                'partially_repealed',
                'superseded',
                'unknown',
                'archived'
            )
            """,
            name="ck_ordinances_status",
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"],
            ["municipalities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="SET NULL",
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
    )
    op.create_index(
        op.f("ix_ordinances_municipality_id"),
        "ordinances",
        ["municipality_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_document_id"),
        "ordinances",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_topic"),
        "ordinances",
        ["topic"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_status"),
        "ordinances",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_publication_date"),
        "ordinances",
        ["publication_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_approval_date"),
        "ordinances",
        ["approval_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_created_by_id"),
        "ordinances",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinances_updated_by_id"),
        "ordinances",
        ["updated_by_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ordinances_updated_by_id"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_created_by_id"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_approval_date"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_publication_date"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_status"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_topic"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_document_id"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_municipality_id"), table_name="ordinances")
    op.drop_table("ordinances")
