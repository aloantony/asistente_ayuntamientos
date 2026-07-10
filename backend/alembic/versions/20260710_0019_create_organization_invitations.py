"""create organization invitations

Revision ID: 20260710_0019
Revises: 20260629_0018
Create Date: 2026-07-10 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260710_0019"
down_revision: Union[str, None] = "20260629_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
        sa.Column("accepted_by_user_id", sa.Integer(), nullable=True),
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
            "accepted_at is null or revoked_at is null",
            name="ck_organization_invitations_single_terminal_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_organization_invitations_organization_id"),
        "organization_invitations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_invitations_token_hash"),
        "organization_invitations",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_organization_invitations_invited_by_user_id"),
        "organization_invitations",
        ["invited_by_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_invitations_accepted_by_user_id"),
        "organization_invitations",
        ["accepted_by_user_id"],
        unique=False,
    )
    op.create_index(
        "uq_organization_invitations_pending_email",
        "organization_invitations",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at is null and revoked_at is null"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_organization_invitations_pending_email",
        table_name="organization_invitations",
    )
    op.drop_index(
        op.f("ix_organization_invitations_accepted_by_user_id"),
        table_name="organization_invitations",
    )
    op.drop_index(
        op.f("ix_organization_invitations_invited_by_user_id"),
        table_name="organization_invitations",
    )
    op.drop_index(
        op.f("ix_organization_invitations_token_hash"),
        table_name="organization_invitations",
    )
    op.drop_index(
        op.f("ix_organization_invitations_organization_id"),
        table_name="organization_invitations",
    )
    op.drop_table("organization_invitations")
