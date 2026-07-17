"""create global reference-layer catalog

Revision ID: 20260717_0031
Revises: 20260717_0030
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0031"
down_revision: str | None = "20260717_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_catalog_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_catalog_json", sa.JSON(), nullable=False),
        sa.Column("normalized_definition_json", sa.JSON(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_count", sa.Integer(), nullable=False),
        sa.Column("group_count", sa.Integer(), nullable=False),
        sa.Column("layer_count", sa.Integer(), nullable=False),
        sa.Column(
            "unresolved_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "is_current",
            sa.Boolean(),
            server_default=sa.text("false"),
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
        sa.CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_catalog_snapshots_provider_nonempty",
        ),
        sa.CheckConstraint(
            "source_url like 'https://%'",
            name="ck_reference_catalog_snapshots_source_https",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_catalog_snapshots_sha256",
        ),
        sa.CheckConstraint(
            "definition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_catalog_snapshots_definition_sha256",
        ),
        sa.CheckConstraint(
            "service_count >= 0 and group_count >= 0 and layer_count >= 0 "
            "and unresolved_count >= 0",
            name="ck_reference_catalog_snapshots_counts",
        ),
        sa.CheckConstraint(
            "status in ('validated', 'applied', 'rejected')",
            name="ck_reference_catalog_snapshots_status",
        ),
        sa.CheckConstraint(
            "not is_current or status = 'applied'",
            name="ck_reference_catalog_snapshots_current_applied",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "content_sha256",
            "definition_sha256",
            name="uq_reference_catalog_snapshots_provider_hashes",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_catalog_snapshots_provider_id",
        ),
    )
    op.create_index(
        "uq_reference_catalog_snapshots_current_provider",
        "reference_catalog_snapshots",
        ["provider_key"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "reference_services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_seen_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("upstream_protocol", sa.String(length=30), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("capabilities_url", sa.Text(), nullable=True),
        sa.Column("version", sa.String(length=30), nullable=True),
        sa.Column("default_crs", sa.String(length=64), nullable=True),
        sa.Column("default_format", sa.String(length=100), nullable=True),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column("license_name", sa.String(length=255), nullable=True),
        sa.Column("license_url", sa.Text(), nullable=True),
        sa.Column(
            "license_status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "cache_policy",
            sa.String(length=20),
            server_default="none",
            nullable=False,
        ),
        sa.Column("capabilities_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="active",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
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
            name="fk_reference_services_provider_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> '' "
            "and btrim(title) <> ''",
            name="ck_reference_services_identity_nonempty",
        ),
        sa.CheckConstraint(
            "upstream_protocol in "
            "('wms', 'wfs', 'wmts', 'xyz', 'arcgis_rest', 'local')",
            name="ck_reference_services_protocol",
        ),
        sa.CheckConstraint(
            "base_url ~ '^https?://' and "
            "(capabilities_url is null or capabilities_url ~ '^https?://') "
            "and (license_url is null or license_url ~ '^https?://')",
            name="ck_reference_services_urls",
        ),
        sa.CheckConstraint(
            "license_status in ('pending', 'approved', 'restricted')",
            name="ck_reference_services_license_status",
        ),
        sa.CheckConstraint(
            "cache_policy in ('none', 'on_demand', 'mirror')",
            name="ck_reference_services_cache_policy",
        ),
        sa.CheckConstraint(
            "status in ('active', 'degraded', 'missing', 'disabled')",
            name="ck_reference_services_status",
        ),
        sa.CheckConstraint(
            "capabilities_sha256 is null or "
            "capabilities_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_services_capabilities_sha256",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "source_key",
            name="uq_reference_services_provider_source",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_services_provider_id",
        ),
    )
    op.create_index(
        "ix_reference_services_snapshot",
        "reference_services",
        ["last_seen_snapshot_id"],
    )
    op.create_index(
        "ix_reference_services_status",
        "reference_services",
        ["provider_key", "status"],
    )

    op.create_table(
        "reference_layers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_seen_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("node_type", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("remote_name", sa.String(length=500), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=True),
        sa.Column("renderer", sa.String(length=30), nullable=True),
        sa.Column("delivery_mode", sa.String(length=20), nullable=True),
        sa.Column("style_name", sa.String(length=255), nullable=True),
        sa.Column("image_format", sa.String(length=100), nullable=True),
        sa.Column("supported_crs_json", sa.JSON(), nullable=True),
        sa.Column("bounds_json", sa.JSON(), nullable=True),
        sa.Column("options_json", sa.JSON(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "default_visible",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "default_opacity",
            sa.Numeric(4, 3),
            server_default="1",
            nullable=False,
        ),
        sa.Column("min_zoom", sa.SmallInteger(), nullable=True),
        sa.Column("max_zoom", sa.SmallInteger(), nullable=True),
        sa.Column("min_scale_denominator", sa.Numeric(18, 3), nullable=True),
        sa.Column("max_scale_denominator", sa.Numeric(18, 3), nullable=True),
        sa.Column(
            "queryable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "downloadable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("legend_url", sa.Text(), nullable=True),
        sa.Column("metadata_url", sa.Text(), nullable=True),
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
            name="fk_reference_layers_provider_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_layers_provider_service",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "parent_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layers_provider_parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> '' "
            "and btrim(title) <> ''",
            name="ck_reference_layers_identity_nonempty",
        ),
        sa.CheckConstraint(
            "node_type in ('group', 'layer')",
            name="ck_reference_layers_node_type",
        ),
        sa.CheckConstraint(
            "role is null or role in ('base', 'overlay')",
            name="ck_reference_layers_role",
        ),
        sa.CheckConstraint(
            "renderer is null or renderer in ('raster_tile', 'vector_tile')",
            name="ck_reference_layers_renderer",
        ),
        sa.CheckConstraint(
            "delivery_mode is null or delivery_mode in ('proxy', 'mirror')",
            name="ck_reference_layers_delivery_mode",
        ),
        sa.CheckConstraint(
            "status in ('active', 'degraded', 'missing', 'disabled')",
            name="ck_reference_layers_status",
        ),
        sa.CheckConstraint(
            "default_opacity >= 0 and default_opacity <= 1",
            name="ck_reference_layers_default_opacity",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_reference_layers_sort_order",
        ),
        sa.CheckConstraint(
            "(min_zoom is null or min_zoom between 0 and 24) and "
            "(max_zoom is null or max_zoom between 0 and 24) and "
            "(min_zoom is null or max_zoom is null or min_zoom <= max_zoom)",
            name="ck_reference_layers_zoom_range",
        ),
        sa.CheckConstraint(
            "(min_scale_denominator is null or min_scale_denominator > 0) and "
            "(max_scale_denominator is null or max_scale_denominator > 0)",
            name="ck_reference_layers_scale_positive",
        ),
        sa.CheckConstraint(
            "(legend_url is null or legend_url ~ '^https?://') and "
            "(metadata_url is null or metadata_url ~ '^https?://')",
            name="ck_reference_layers_urls",
        ),
        sa.CheckConstraint(
            "(node_type = 'group' and service_id is null and role is null "
            "and renderer is null and delivery_mode is null) or "
            "(node_type = 'layer' and service_id is not null "
            "and role is not null and renderer is not null "
            "and delivery_mode is not null)",
            name="ck_reference_layers_node_shape",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "source_key",
            name="uq_reference_layers_provider_source",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_layers_provider_id",
        ),
    )
    op.create_index(
        "ix_reference_layers_parent_order",
        "reference_layers",
        ["parent_id", "sort_order", "id"],
    )
    op.create_index(
        "ix_reference_layers_service_status",
        "reference_layers",
        ["service_id", "status"],
    )
    op.create_index(
        "ix_reference_layers_snapshot",
        "reference_layers",
        ["last_seen_snapshot_id"],
    )

    op.create_table(
        "organization_reference_layer_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=True),
        sa.Column("opacity", sa.Numeric(4, 3), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["layer_id"],
            ["reference_layers.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "visible is not null or opacity is not null",
            name="ck_org_reference_layer_settings_has_override",
        ),
        sa.CheckConstraint(
            "opacity is null or (opacity >= 0 and opacity <= 1)",
            name="ck_org_reference_layer_settings_opacity",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "layer_id",
            name="uq_org_reference_layer_settings_org_layer",
        ),
    )
    op.create_index(
        "ix_org_reference_layer_settings_layer",
        "organization_reference_layer_settings",
        ["layer_id"],
    )
    op.create_index(
        "ix_org_reference_layer_settings_updated_by",
        "organization_reference_layer_settings",
        ["updated_by_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    setting_count = connection.execute(
        sa.text("SELECT count(*) FROM organization_reference_layer_settings")
    ).scalar_one()
    if setting_count:
        raise RuntimeError(
            "Reference-layer organization settings contain data. Export or "
            "remove those overrides explicitly before downgrading 0031."
        )

    op.drop_index(
        "ix_org_reference_layer_settings_updated_by",
        table_name="organization_reference_layer_settings",
    )
    op.drop_index(
        "ix_org_reference_layer_settings_layer",
        table_name="organization_reference_layer_settings",
    )
    op.drop_table("organization_reference_layer_settings")
    op.drop_index("ix_reference_layers_snapshot", table_name="reference_layers")
    op.drop_index(
        "ix_reference_layers_service_status",
        table_name="reference_layers",
    )
    op.drop_index(
        "ix_reference_layers_parent_order",
        table_name="reference_layers",
    )
    op.drop_table("reference_layers")
    op.drop_index("ix_reference_services_status", table_name="reference_services")
    op.drop_index(
        "ix_reference_services_snapshot",
        table_name="reference_services",
    )
    op.drop_table("reference_services")
    op.drop_index(
        "uq_reference_catalog_snapshots_current_provider",
        table_name="reference_catalog_snapshots",
    )
    op.drop_table("reference_catalog_snapshots")
