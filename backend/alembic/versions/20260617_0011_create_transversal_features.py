"""create transversal assistant features

Revision ID: 20260617_0011
Revises: 20260615_0010
Create Date: 2026-06-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260617_0011"
down_revision: Union[str, None] = "20260615_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_transversal_features",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_requirement_id", sa.Integer(), nullable=True),
        sa.Column("source_organization_id", sa.Integer(), nullable=False),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column(
            "sensitivity",
            sa.String(length=30),
            server_default="normal",
            nullable=False,
        ),
        sa.Column(
            "auto_activatable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("proposed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
            status in (
                'proposed',
                'approved',
                'developed',
                'available',
                'rejected',
                'archived',
                'blocked'
            )
            """,
            name="ck_assistant_transversal_features_status",
        ),
        sa.CheckConstraint(
            """
            category in (
                'process',
                'compliance',
                'automation',
                'documents',
                'citizen_service',
                'other'
            )
            """,
            name="ck_assistant_transversal_features_category",
        ),
        sa.CheckConstraint(
            "sensitivity in ('normal', 'personal', 'sensitive', 'legal')",
            name="ck_assistant_transversal_features_sensitivity",
        ),
        sa.ForeignKeyConstraint(
            ["source_requirement_id"],
            ["requirements.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["assistant_conversations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["assistant_messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_source_requirement_id"),
        "assistant_transversal_features",
        ["source_requirement_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_source_organization_id"),
        "assistant_transversal_features",
        ["source_organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_source_conversation_id"),
        "assistant_transversal_features",
        ["source_conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_source_message_id"),
        "assistant_transversal_features",
        ["source_message_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_status"),
        "assistant_transversal_features",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_proposed_by_id"),
        "assistant_transversal_features",
        ["proposed_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_features_reviewed_by_id"),
        "assistant_transversal_features",
        ["reviewed_by_id"],
        unique=False,
    )

    op.create_table(
        "assistant_transversal_feature_adoptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feature_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="suggested",
            nullable=False,
        ),
        sa.Column("requested_by_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_id", sa.Integer(), nullable=True),
        sa.Column("source_conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
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
            "status in ('suggested', 'accepted', 'activation_pending', 'active', 'rejected', 'paused')",
            name="ck_assistant_transversal_feature_adoptions_status",
        ),
        sa.ForeignKeyConstraint(
            ["feature_id"],
            ["assistant_transversal_features.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["assistant_conversations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["assistant_messages.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "feature_id",
            "organization_id",
            name="uq_assistant_transversal_feature_adoptions_feature_org",
        ),
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_feature_id"),
        "assistant_transversal_feature_adoptions",
        ["feature_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_organization_id"),
        "assistant_transversal_feature_adoptions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_status"),
        "assistant_transversal_feature_adoptions",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_requested_by_id"),
        "assistant_transversal_feature_adoptions",
        ["requested_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_approved_by_id"),
        "assistant_transversal_feature_adoptions",
        ["approved_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_source_conversation_id"),
        "assistant_transversal_feature_adoptions",
        ["source_conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_transversal_feature_adoptions_source_message_id"),
        "assistant_transversal_feature_adoptions",
        ["source_message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_source_message_id"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_source_conversation_id"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_approved_by_id"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_requested_by_id"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_status"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_organization_id"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_feature_adoptions_feature_id"),
        table_name="assistant_transversal_feature_adoptions",
    )
    op.drop_table("assistant_transversal_feature_adoptions")

    op.drop_index(
        op.f("ix_assistant_transversal_features_reviewed_by_id"),
        table_name="assistant_transversal_features",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_features_proposed_by_id"),
        table_name="assistant_transversal_features",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_features_status"),
        table_name="assistant_transversal_features",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_features_source_message_id"),
        table_name="assistant_transversal_features",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_features_source_conversation_id"),
        table_name="assistant_transversal_features",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_features_source_organization_id"),
        table_name="assistant_transversal_features",
    )
    op.drop_index(
        op.f("ix_assistant_transversal_features_source_requirement_id"),
        table_name="assistant_transversal_features",
    )
    op.drop_table("assistant_transversal_features")
