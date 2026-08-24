"""create versioned local reference-layer mirror

Revision ID: 20260717_0034
Revises: 20260717_0033
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0034"
down_revision: str | None = "20260717_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_TABLES = (
    (
        "reference_source_artifacts",
        "prevent_reference_source_artifact_mutation",
        "trg_reference_source_artifacts_immutable",
    ),
    (
        "reference_sync_run_artifacts",
        "prevent_reference_sync_run_artifact_mutation",
        "trg_reference_sync_run_artifacts_immutable",
    ),
    (
        "reference_delivery_versions",
        "prevent_reference_delivery_version_mutation",
        "trg_reference_delivery_versions_immutable",
    ),
    (
        "reference_delivery_version_artifacts",
        "prevent_reference_delivery_version_artifact_mutation",
        "trg_reference_delivery_version_artifacts_immutable",
    ),
    (
        "reference_delivery_assets",
        "prevent_reference_delivery_asset_mutation",
        "trg_reference_delivery_assets_immutable",
    ),
    (
        "reference_delivery_promotions",
        "prevent_reference_delivery_promotion_mutation",
        "trg_reference_delivery_promotions_immutable",
    ),
)

MIRROR_TABLES = (
    "reference_layer_delivery_state",
    "reference_delivery_promotions",
    "reference_delivery_assets",
    "reference_delivery_version_artifacts",
    "reference_delivery_versions",
    "reference_sync_run_artifacts",
    "reference_source_artifacts",
    "reference_sync_runs",
    "reference_layer_sources",
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "reference_layer_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=True),
        sa.Column("remote_name", sa.Text(), nullable=True),
        sa.Column("source_format", sa.Text(), nullable=True),
        sa.Column("sync_strategy", sa.String(length=32), nullable=False),
        sa.Column(
            "config_json",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("definition_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "check_interval_seconds",
            sa.Integer(),
            server_default="86400",
            nullable=False,
        ),
        sa.Column(
            "full_refresh_interval_seconds",
            sa.Integer(),
            server_default="2592000",
            nullable=False,
        ),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> ''",
            name="ck_reference_layer_sources_identity_nonempty",
        ),
        sa.CheckConstraint(
            "protocol in ('wfs', 'ogc_api_features', 'wcs', "
            "'arcgis_rest', 'atom', 'download', 'wmts', 'xyz', "
            "'wms_tiles', 'local')",
            name="ck_reference_layer_sources_protocol",
        ),
        sa.CheckConstraint(
            "target_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_layer_sources_target_kind",
        ),
        sa.CheckConstraint(
            "sync_strategy in ('conditional_get', 'full_snapshot', "
            "'paged_snapshot', 'tile_seed', 'manual')",
            name="ck_reference_layer_sources_sync_strategy",
        ),
        sa.CheckConstraint(
            "(protocol = 'local' and endpoint_url is null) or "
            "(protocol <> 'local' and endpoint_url like 'https://%')",
            name="ck_reference_layer_sources_endpoint",
        ),
        sa.CheckConstraint(
            "definition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_layer_sources_definition_sha256",
        ),
        sa.CheckConstraint(
            "priority >= 0 and check_interval_seconds >= 300 and "
            "full_refresh_interval_seconds >= check_interval_seconds",
            name="ck_reference_layer_sources_schedule",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_sources_provider_layer",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_key",
            name="uq_reference_layer_sources_layer_source",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_layer_sources_provider_layer_id",
        ),
    )
    op.create_index(
        "uq_reference_layer_sources_primary",
        "reference_layer_sources",
        ["provider_key", "layer_id"],
        unique=True,
        postgresql_where=sa.text("enabled and is_primary"),
    )
    op.create_index(
        "ix_reference_layer_sources_due",
        "reference_layer_sources",
        ["next_check_at", "id"],
        postgresql_where=sa.text("enabled"),
    )
    op.create_index(
        "ix_reference_layer_sources_layer_priority",
        "reference_layer_sources",
        ["layer_id", "enabled", "priority", "id"],
    )

    op.create_table(
        "reference_sync_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by_id", sa.Integer(), nullable=True),
        sa.Column("source_definition_json", sa.JSON(), nullable=False),
        sa.Column(
            "source_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("trigger_kind", sa.String(length=20), nullable=False),
        sa.Column("check_mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "attempt_no",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "expected_active_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_etag", sa.Text(), nullable=True),
        sa.Column(
            "observed_last_modified",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("observed_version", sa.Text(), nullable=True),
        sa.Column(
            "observed_manifest_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column(
            "stats_json",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and attempt_no > 0 "
            "and expected_active_generation >= 0",
            name="ck_reference_sync_runs_identity",
        ),
        sa.CheckConstraint(
            "trigger_kind in ('scheduled', 'manual', 'retry', 'backfill')",
            name="ck_reference_sync_runs_trigger_kind",
        ),
        sa.CheckConstraint(
            "check_mode in ('conditional', 'full')",
            name="ck_reference_sync_runs_check_mode",
        ),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'unchanged', 'succeeded', "
            "'rejected', 'failed', 'cancelled')",
            name="ck_reference_sync_runs_status",
        ),
        sa.CheckConstraint(
            "observed_manifest_sha256 is null or "
            "observed_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_sync_runs_manifest_sha256",
        ),
        sa.CheckConstraint(
            "(status = 'queued' and started_at is null and finished_at is null "
            "and lease_token is null and lease_expires_at is null and "
            "heartbeat_at is null) or "
            "(status = 'running' and started_at is not null and "
            "finished_at is null and lease_token is not null and "
            "lease_expires_at is not null and heartbeat_at is not null) or "
            "(status in ('unchanged', 'succeeded', 'rejected', 'failed', "
            "'cancelled') and finished_at is not null and lease_token is null "
            "and lease_expires_at is null)",
            name="ck_reference_sync_runs_lifecycle",
        ),
        sa.CheckConstraint(
            "status not in ('rejected', 'failed') or "
            "(error_code is not null and btrim(error_code) <> '')",
            name="ck_reference_sync_runs_failure_error",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["reference_layer_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "source_id",
            "id",
            name="uq_reference_sync_runs_source_id",
        ),
    )
    op.create_index(
        "uq_reference_sync_runs_open_source",
        "reference_sync_runs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status in ('queued', 'running')"),
    )
    op.create_index(
        "ix_reference_sync_runs_queued",
        "reference_sync_runs",
        ["queued_at", "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_reference_sync_runs_running_lease",
        "reference_sync_runs",
        ["lease_expires_at", "id"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_reference_sync_runs_source_history",
        "reference_sync_runs",
        ["source_id", "id"],
    )
    op.create_index(
        "ix_reference_sync_runs_requested_by",
        "reference_sync_runs",
        ["requested_by_id"],
    )

    op.create_table(
        "reference_source_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_kind", sa.String(length=24), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("upstream_etag", sa.Text(), nullable=True),
        sa.Column(
            "upstream_last_modified",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "artifact_kind in ('capabilities', 'manifest', 'dataset', "
            "'style', 'metadata', 'tile_archive')",
            name="ck_reference_source_artifacts_kind",
        ),
        sa.CheckConstraint(
            "storage_backend in ('filesystem', 's3')",
            name="ck_reference_source_artifacts_storage_backend",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 and sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_source_artifacts_content",
        ),
        sa.CheckConstraint(
            "(source_url is null or source_url like 'https://%') and "
            "(final_url is null or final_url like 'https://%')",
            name="ck_reference_source_artifacts_urls",
        ),
        sa.CheckConstraint(
            "btrim(media_type) <> '' and btrim(storage_key) <> ''",
            name="ck_reference_source_artifacts_required_text",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["reference_layer_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_id",
            "artifact_kind",
            "sha256",
            name="uq_reference_source_artifacts_content",
        ),
        sa.UniqueConstraint(
            "source_id",
            "id",
            name="uq_reference_source_artifacts_source_id",
        ),
        sa.UniqueConstraint(
            "storage_backend",
            "storage_key",
            name="uq_reference_source_artifacts_storage_key",
        ),
    )
    op.create_index(
        "ix_reference_source_artifacts_source_history",
        "reference_source_artifacts",
        ["source_id", "id"],
    )

    op.create_table(
        "reference_sync_run_artifacts",
        sa.Column("source_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), primary_key=True),
        sa.Column("artifact_id", sa.BigInteger(), primary_key=True),
        sa.Column("role", sa.String(length=20), primary_key=True),
        _created_at(),
        sa.CheckConstraint(
            "role in ('observation', 'input', 'style', 'metadata')",
            name="ck_reference_sync_run_artifacts_role",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id"],
            ["reference_sync_runs.source_id", "reference_sync_runs.id"],
            name="fk_reference_sync_run_artifacts_source_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_sync_run_artifacts_source_artifact",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_reference_sync_run_artifacts_artifact",
        "reference_sync_run_artifacts",
        ["artifact_id", "run_id"],
    )

    op.create_table(
        "reference_delivery_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Integer(), nullable=False),
        sa.Column(
            "catalog_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("delivery_kind", sa.String(length=16), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("validation_sha256", sa.String(length=64), nullable=False),
        sa.Column("reference_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crs", sa.Text(), nullable=False),
        sa.Column("bounds_json", sa.JSON(), nullable=False),
        sa.Column("feature_count", sa.BigInteger(), nullable=True),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "delivery_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_delivery_versions_kind",
        ),
        sa.CheckConstraint(
            "sequence_number > 0 and "
            "(feature_count is null or feature_count >= 0)",
            name="ck_reference_delivery_versions_counts",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$' and "
            "manifest_sha256 ~ '^[0-9a-f]{64}$' and "
            "validation_sha256 ~ '^[0-9a-f]{64}$' and "
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_delivery_versions_hashes",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(crs) <> ''",
            name="ck_reference_delivery_versions_required_text",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_delivery_versions_layer_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "sync_run_id"],
            ["reference_sync_runs.source_id", "reference_sync_runs.id"],
            name="fk_reference_delivery_versions_source_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "catalog_snapshot_id",
                "catalog_definition_sha256",
            ],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
                "reference_catalog_snapshots.definition_sha256",
            ],
            name="fk_reference_delivery_versions_catalog",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_delivery_versions_provider_layer_id",
        ),
        sa.UniqueConstraint(
            "source_id",
            "id",
            name="uq_reference_delivery_versions_source_id",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "sequence_number",
            name="uq_reference_delivery_versions_sequence",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "manifest_sha256",
            name="uq_reference_delivery_versions_manifest",
        ),
        sa.UniqueConstraint(
            "sync_run_id",
            name="uq_reference_delivery_versions_sync_run",
        ),
    )
    op.create_index(
        "ix_reference_delivery_versions_source_history",
        "reference_delivery_versions",
        ["source_id", "id"],
    )
    op.create_index(
        "ix_reference_delivery_versions_catalog_snapshot",
        "reference_delivery_versions",
        ["catalog_snapshot_id"],
    )

    op.create_table(
        "reference_delivery_version_artifacts",
        sa.Column("source_id", sa.BigInteger(), primary_key=True),
        sa.Column("version_id", sa.BigInteger(), primary_key=True),
        sa.Column("artifact_id", sa.BigInteger(), primary_key=True),
        sa.Column("role", sa.String(length=20), primary_key=True),
        _created_at(),
        sa.CheckConstraint(
            "role in ('input', 'style', 'metadata')",
            name="ck_reference_delivery_version_artifacts_role",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "version_id"],
            [
                "reference_delivery_versions.source_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_delivery_version_artifacts_source_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_delivery_version_artifacts_source_artifact",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_reference_delivery_version_artifacts_artifact",
        "reference_delivery_version_artifacts",
        ["artifact_id", "version_id"],
    )

    op.create_table(
        "reference_delivery_assets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("asset_key", sa.String(length=255), nullable=False),
        sa.Column("asset_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata_json",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        _created_at(),
        sa.CheckConstraint(
            "asset_kind in ('vector_table', 'raster_cog', 'tile_archive', "
            "'tile_prefix', 'style_sld', 'legend', 'metadata')",
            name="ck_reference_delivery_assets_kind",
        ),
        sa.CheckConstraint(
            "storage_backend in ('postgres', 'filesystem', 's3')",
            name="ck_reference_delivery_assets_storage_backend",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' and "
            "(size_bytes is null or size_bytes >= 0) and "
            "(storage_backend = 'postgres' or size_bytes is not null)",
            name="ck_reference_delivery_assets_content",
        ),
        sa.CheckConstraint(
            "btrim(asset_key) <> '' and btrim(storage_key) <> '' and "
            "btrim(media_type) <> ''",
            name="ck_reference_delivery_assets_required_text",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["reference_delivery_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "version_id",
            "asset_key",
            name="uq_reference_delivery_assets_version_key",
        ),
    )
    op.create_index(
        "uq_reference_delivery_assets_primary",
        "reference_delivery_assets",
        ["version_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_index(
        "ix_reference_delivery_assets_storage",
        "reference_delivery_assets",
        ["storage_backend", "storage_key"],
    )

    op.create_table(
        "reference_delivery_promotions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("from_version_id", sa.BigInteger(), nullable=True),
        sa.Column("to_version_id", sa.BigInteger(), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_event_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "previous_event_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "action in ('promote', 'rollback', 'deactivate')",
            name="ck_reference_delivery_promotions_action",
        ),
        sa.CheckConstraint(
            "(action = 'promote' and to_version_id is not null) or "
            "(action = 'rollback' and from_version_id is not null and "
            "to_version_id is not null) or "
            "(action = 'deactivate' and from_version_id is not null and "
            "to_version_id is null)",
            name="ck_reference_delivery_promotions_shape",
        ),
        sa.CheckConstraint(
            "from_version_id is null or to_version_id is null or "
            "from_version_id <> to_version_id",
            name="ck_reference_delivery_promotions_changes_version",
        ),
        sa.CheckConstraint(
            "(sequence_number = 1 and previous_event_id is null and "
            "previous_event_sha256 is null) or "
            "(sequence_number > 1 and previous_event_id is not null and "
            "previous_event_sha256 is not null)",
            name="ck_reference_delivery_promotions_chain",
        ),
        sa.CheckConstraint(
            "event_sha256 ~ '^[0-9a-f]{64}$' and "
            "(previous_event_sha256 is null or "
            "previous_event_sha256 ~ '^[0-9a-f]{64}$') and "
            "btrim(reason) <> ''",
            name="ck_reference_delivery_promotions_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_delivery_promotions_provider_layer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "from_version_id"],
            [
                "reference_delivery_versions.provider_key",
                "reference_delivery_versions.layer_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_delivery_promotions_from_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "to_version_id"],
            [
                "reference_delivery_versions.provider_key",
                "reference_delivery_versions.layer_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_delivery_promotions_to_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reference_sync_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "previous_event_id",
                "previous_event_sha256",
            ],
            [
                "reference_delivery_promotions.provider_key",
                "reference_delivery_promotions.layer_id",
                "reference_delivery_promotions.id",
                "reference_delivery_promotions.event_sha256",
            ],
            name="fk_reference_delivery_promotions_previous",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "event_sha256",
            name="uq_reference_delivery_promotions_hash",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "sequence_number",
            name="uq_reference_delivery_promotions_sequence",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_delivery_promotions_provider_layer_id",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            "event_sha256",
            name="uq_reference_delivery_promotions_chain_target",
        ),
    )
    op.create_index(
        "ix_reference_delivery_promotions_current_lookup",
        "reference_delivery_promotions",
        ["provider_key", "layer_id", "sequence_number"],
    )
    op.create_index(
        "ix_reference_delivery_promotions_from_version",
        "reference_delivery_promotions",
        ["provider_key", "layer_id", "from_version_id"],
    )
    op.create_index(
        "ix_reference_delivery_promotions_to_version",
        "reference_delivery_promotions",
        ["provider_key", "layer_id", "to_version_id"],
    )
    op.create_index(
        "ix_reference_delivery_promotions_run",
        "reference_delivery_promotions",
        ["run_id"],
    )
    op.create_index(
        "ix_reference_delivery_promotions_actor",
        "reference_delivery_promotions",
        ["actor_id"],
    )
    op.create_index(
        "uq_reference_delivery_promotions_genesis",
        "reference_delivery_promotions",
        ["provider_key", "layer_id"],
        unique=True,
        postgresql_where=sa.text("previous_event_id is null"),
    )
    op.create_index(
        "uq_reference_delivery_promotions_successor",
        "reference_delivery_promotions",
        ["provider_key", "layer_id", "previous_event_id"],
        unique=True,
        postgresql_where=sa.text("previous_event_id is not null"),
    )

    op.create_table(
        "reference_layer_delivery_state",
        sa.Column("provider_key", sa.String(length=64), primary_key=True),
        sa.Column("layer_id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("active_version_id", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("last_promotion_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'disabled')",
            name="ck_reference_layer_delivery_state_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' and active_version_id is not null) or "
            "(status = 'disabled' and active_version_id is null)",
            name="ck_reference_layer_delivery_state_version",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_reference_layer_delivery_state_generation",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_delivery_state_provider_layer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "active_version_id"],
            [
                "reference_delivery_versions.provider_key",
                "reference_delivery_versions.layer_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_layer_delivery_state_active_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "last_promotion_id"],
            [
                "reference_delivery_promotions.provider_key",
                "reference_delivery_promotions.layer_id",
                "reference_delivery_promotions.id",
            ],
            name="fk_reference_layer_delivery_state_last_promotion",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_reference_layer_delivery_state_active_version",
        "reference_layer_delivery_state",
        ["active_version_id"],
    )
    op.create_index(
        "ix_reference_layer_delivery_state_last_promotion",
        "reference_layer_delivery_state",
        ["last_promotion_id"],
    )

    for table_name, function_name, trigger_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE FUNCTION {function_name}()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'reference mirror records are immutable'
                USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION {function_name}()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE " + ", ".join(MIRROR_TABLES) + " IN ACCESS EXCLUSIVE MODE"
    )
    total_rows = bind.execute(
        sa.text(
            "SELECT "
            + " + ".join(
                f"(SELECT count(*) FROM {table_name})"
                for table_name in MIRROR_TABLES
            )
        )
    ).scalar_one()
    if int(total_rows):
        raise RuntimeError(
            "cannot downgrade: versioned reference mirror data exists "
            f"(rows={total_rows})"
        )

    for table_name, function_name, trigger_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER {trigger_name} ON {table_name}")
        op.execute(f"DROP FUNCTION {function_name}()")

    op.drop_index(
        "ix_reference_layer_delivery_state_last_promotion",
        table_name="reference_layer_delivery_state",
    )
    op.drop_index(
        "ix_reference_layer_delivery_state_active_version",
        table_name="reference_layer_delivery_state",
    )
    op.drop_table("reference_layer_delivery_state")

    for index_name in (
        "uq_reference_delivery_promotions_successor",
        "uq_reference_delivery_promotions_genesis",
        "ix_reference_delivery_promotions_actor",
        "ix_reference_delivery_promotions_run",
        "ix_reference_delivery_promotions_to_version",
        "ix_reference_delivery_promotions_from_version",
        "ix_reference_delivery_promotions_current_lookup",
    ):
        op.drop_index(index_name, table_name="reference_delivery_promotions")
    op.drop_table("reference_delivery_promotions")

    op.drop_index(
        "ix_reference_delivery_assets_storage",
        table_name="reference_delivery_assets",
    )
    op.drop_index(
        "uq_reference_delivery_assets_primary",
        table_name="reference_delivery_assets",
    )
    op.drop_table("reference_delivery_assets")

    op.drop_index(
        "ix_reference_delivery_version_artifacts_artifact",
        table_name="reference_delivery_version_artifacts",
    )
    op.drop_table("reference_delivery_version_artifacts")

    op.drop_index(
        "ix_reference_delivery_versions_catalog_snapshot",
        table_name="reference_delivery_versions",
    )
    op.drop_index(
        "ix_reference_delivery_versions_source_history",
        table_name="reference_delivery_versions",
    )
    op.drop_table("reference_delivery_versions")

    op.drop_index(
        "ix_reference_sync_run_artifacts_artifact",
        table_name="reference_sync_run_artifacts",
    )
    op.drop_table("reference_sync_run_artifacts")

    op.drop_index(
        "ix_reference_source_artifacts_source_history",
        table_name="reference_source_artifacts",
    )
    op.drop_table("reference_source_artifacts")

    for index_name in (
        "ix_reference_sync_runs_requested_by",
        "ix_reference_sync_runs_source_history",
        "ix_reference_sync_runs_running_lease",
        "ix_reference_sync_runs_queued",
        "uq_reference_sync_runs_open_source",
    ):
        op.drop_index(index_name, table_name="reference_sync_runs")
    op.drop_table("reference_sync_runs")

    for index_name in (
        "ix_reference_layer_sources_layer_priority",
        "ix_reference_layer_sources_due",
        "uq_reference_layer_sources_primary",
    ):
        op.drop_index(index_name, table_name="reference_layer_sources")
    op.drop_table("reference_layer_sources")
