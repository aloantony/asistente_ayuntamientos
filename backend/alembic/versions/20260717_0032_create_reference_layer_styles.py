"""create reference-layer styles

Revision ID: 20260717_0032
Revises: 20260717_0031
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0032"
down_revision: str | None = "20260717_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_layer_styles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_seen_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("legend_url", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="active",
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["provider_key", "last_seen_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_layer_styles_provider_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_styles_provider_layer",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> '' "
            "and btrim(title) <> ''",
            name="ck_reference_layer_styles_identity_nonempty",
        ),
        sa.CheckConstraint(
            "legend_url is null or legend_url ~ '^https?://'",
            name="ck_reference_layer_styles_legend_url",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_reference_layer_styles_sort_order",
        ),
        sa.CheckConstraint(
            "status in ('active', 'degraded', 'missing', 'disabled')",
            name="ck_reference_layer_styles_status",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_key",
            name="uq_reference_layer_styles_provider_layer_source",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_layer_styles_provider_id",
        ),
    )
    op.create_index(
        "uq_reference_layer_styles_default",
        "reference_layer_styles",
        ["provider_key", "layer_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.create_index(
        "ix_reference_layer_styles_layer_order",
        "reference_layer_styles",
        ["layer_id", "sort_order", "id"],
    )
    op.create_index(
        "ix_reference_layer_styles_snapshot",
        "reference_layer_styles",
        ["last_seen_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reference_layer_styles_snapshot",
        table_name="reference_layer_styles",
    )
    op.drop_index(
        "ix_reference_layer_styles_layer_order",
        table_name="reference_layer_styles",
    )
    op.drop_index(
        "uq_reference_layer_styles_default",
        table_name="reference_layer_styles",
    )
    op.drop_table("reference_layer_styles")
