"""create immutable local style parity matrix

Revision ID: 20260726_0041
Revises: 20260726_0040
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0041"
down_revision: str | None = "20260726_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PARITY_TABLES = (
    "reference_delivery_style_resources",
    "reference_delivery_style_parities",
    "reference_style_parity_plan_resources",
    "reference_style_parity_plan_items",
    "reference_style_parity_plans",
)

IMMUTABLE_TABLES = (
    (
        "reference_style_parity_plans",
        "prevent_reference_style_parity_plan_mutation",
        "trg_reference_style_parity_plans_immutable",
        "trg_reference_style_parity_plans_truncate_immutable",
    ),
    (
        "reference_style_parity_plan_items",
        "prevent_reference_style_parity_plan_item_mutation",
        "trg_reference_style_parity_plan_items_immutable",
        "trg_reference_style_parity_plan_items_truncate_immutable",
    ),
    (
        "reference_style_parity_plan_resources",
        "prevent_reference_style_parity_plan_resource_mutation",
        "trg_reference_style_parity_plan_resources_immutable",
        "trg_reference_style_parity_plan_resources_truncate_immutable",
    ),
    (
        "reference_delivery_style_parities",
        "prevent_reference_delivery_style_parity_mutation",
        "trg_reference_delivery_style_parities_immutable",
        "trg_reference_delivery_style_parities_truncate_immutable",
    ),
    (
        "reference_delivery_style_resources",
        "prevent_reference_delivery_style_resource_mutation",
        "trg_reference_delivery_style_resources_immutable",
        "trg_reference_delivery_style_resources_truncate_immutable",
    ),
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _replace_mirror_artifact_checks(*, expanded: bool) -> None:
    op.drop_constraint(
        "ck_reference_source_artifacts_kind",
        "reference_source_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_sync_run_artifacts_role",
        "reference_sync_run_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_delivery_version_artifacts_role",
        "reference_delivery_version_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_delivery_assets_kind",
        "reference_delivery_assets",
        type_="check",
    )
    if expanded:
        artifact_kinds = (
            "artifact_kind in ('capabilities', 'manifest', 'dataset', "
            "'style', 'style_package', 'style_resource', 'metadata', "
            "'tile_archive')"
        )
        run_roles = (
            "role in ('observation', 'input', 'style', 'style_package', "
            "'style_resource', 'metadata')"
        )
        delivery_roles = (
            "role in ('input', 'style', 'style_package', "
            "'style_resource', 'metadata')"
        )
        asset_kinds = (
            "asset_kind in ('vector_table', 'raster_cog', 'tile_archive', "
            "'tile_prefix', 'style_sld', 'style_package', 'legend', "
            "'metadata')"
        )
    else:
        artifact_kinds = (
            "artifact_kind in ('capabilities', 'manifest', 'dataset', "
            "'style', 'metadata', 'tile_archive')"
        )
        run_roles = "role in ('observation', 'input', 'style', 'metadata')"
        delivery_roles = "role in ('input', 'style', 'metadata')"
        asset_kinds = (
            "asset_kind in ('vector_table', 'raster_cog', 'tile_archive', "
            "'tile_prefix', 'style_sld', 'legend', 'metadata')"
        )
    op.create_check_constraint(
        "ck_reference_source_artifacts_kind",
        "reference_source_artifacts",
        artifact_kinds,
    )
    op.create_check_constraint(
        "ck_reference_sync_run_artifacts_role",
        "reference_sync_run_artifacts",
        run_roles,
    )
    op.create_check_constraint(
        "ck_reference_delivery_version_artifacts_role",
        "reference_delivery_version_artifacts",
        delivery_roles,
    )
    op.create_check_constraint(
        "ck_reference_delivery_assets_kind",
        "reference_delivery_assets",
        asset_kinds,
    )


def upgrade() -> None:
    _replace_mirror_artifact_checks(expanded=True)

    op.create_table(
        "reference_style_parity_plans",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Integer(), nullable=False),
        sa.Column(
            "catalog_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=False),
        sa.Column("delivery_kind", sa.String(length=16), nullable=False),
        sa.Column("required_style_count", sa.Integer(), nullable=False),
        sa.Column("missing_style_count", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "delivery_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_style_parity_plans_kind",
        ),
        sa.CheckConstraint(
            "required_style_count > 0 and missing_style_count >= 0 and "
            "missing_style_count <= required_style_count and "
            "complete = (missing_style_count = 0)",
            name="ck_reference_style_parity_plans_counts",
        ),
        sa.CheckConstraint(
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_style_parity_plans_hashes",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and "
            "octet_length(evidence_json::text) <= 4194304",
            name="ck_reference_style_parity_plans_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_style_parity_plans_layer_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "sync_run_id"],
            ["reference_sync_runs.source_id", "reference_sync_runs.id"],
            name="fk_reference_style_parity_plans_source_run",
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
            name="fk_reference_style_parity_plans_catalog",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_style_parity_plans"),
        ),
        sa.UniqueConstraint(
            "source_id",
            "sync_run_id",
            name="uq_reference_style_parity_plans_run",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_style_parity_plans_layer_id",
        ),
    )
    op.create_index(
        "ix_reference_style_parity_plans_snapshot",
        "reference_style_parity_plans",
        ["provider_key", "catalog_snapshot_id", "layer_id"],
    )

    op.create_table(
        "reference_style_parity_plan_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("style_id", sa.Integer(), nullable=True),
        sa.Column("style_source_key", sa.String(length=255), nullable=False),
        sa.Column("remote_name", sa.String(length=255), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("parity_kind", sa.String(length=16), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column(
            "source_style_artifact_id",
            sa.BigInteger(),
            nullable=True,
        ),
        sa.Column(
            "source_package_artifact_id",
            sa.BigInteger(),
            nullable=True,
        ),
        sa.Column("resource_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "parity_kind in ('exact', 'adapted', 'baked', 'missing')",
            name="ck_reference_style_parity_plan_items_kind",
        ),
        sa.CheckConstraint(
            "btrim(style_source_key) <> '' and btrim(remote_name) <> '' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$' and resource_count >= 0 and "
            "octet_length(evidence_json::text) <= 4194304",
            name="ck_reference_style_parity_plan_items_evidence",
        ),
        sa.CheckConstraint(
            "(parity_kind = 'missing' and not verified and "
            "reason_code is not null and btrim(reason_code) <> '') or "
            "(parity_kind <> 'missing' and verified and reason_code is null)",
            name="ck_reference_style_parity_plan_items_verification",
        ),
        sa.CheckConstraint(
            "(parity_kind = 'exact' and "
            "source_style_artifact_id is not null and "
            "source_package_artifact_id is null and resource_count = 0) or "
            "(parity_kind = 'adapted' and "
            "source_style_artifact_id is not null and "
            "source_package_artifact_id is not null and resource_count > 0) "
            "or (parity_kind in ('baked', 'missing') and "
            "source_style_artifact_id is null and "
            "source_package_artifact_id is null and resource_count = 0)",
            name="ck_reference_style_parity_plan_items_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["reference_style_parity_plans.id"],
            name="fk_reference_style_parity_plan_items_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["style_id"],
            ["reference_layer_styles.id"],
            name="fk_reference_style_parity_plan_items_style",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "source_style_artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_style_parity_plan_items_style_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "source_package_artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_style_parity_plan_items_package_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_style_parity_plan_items"),
        ),
        sa.UniqueConstraint(
            "plan_id",
            "style_source_key",
            name="uq_reference_style_parity_plan_items_identity",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "id",
            name="uq_reference_style_parity_plan_items_plan_id",
        ),
    )
    op.create_index(
        "ix_reference_style_parity_plan_items_status",
        "reference_style_parity_plan_items",
        ["plan_id", "parity_kind", "id"],
    )

    op.create_table(
        "reference_style_parity_plan_resources",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("plan_item_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("original_href", sa.Text(), nullable=False),
        sa.Column("resolved_url", sa.Text(), nullable=False),
        sa.Column("local_path", sa.String(length=96), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "resolved_url like 'https://%' and "
            "btrim(original_href) <> '' and "
            "local_path ~ '^resources/[0-9a-f]{64}\\.[a-z0-9]{1,8}$' and "
            "sha256 ~ '^[0-9a-f]{64}$' and btrim(media_type) <> '' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_style_parity_plan_resources_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["plan_item_id"],
            ["reference_style_parity_plan_items.id"],
            name="fk_reference_style_parity_plan_resources_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_style_parity_plan_resources_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_style_parity_plan_resources"),
        ),
        sa.UniqueConstraint(
            "plan_item_id",
            "original_href",
            name="uq_reference_style_parity_plan_resources_href",
        ),
        sa.UniqueConstraint(
            "plan_item_id",
            "id",
            name="uq_reference_style_parity_plan_resources_item_id",
        ),
    )
    op.create_index(
        "ix_reference_style_parity_plan_resources_artifact",
        "reference_style_parity_plan_resources",
        ["artifact_id", "plan_item_id"],
    )

    op.create_table(
        "reference_delivery_style_parities",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_item_id", sa.BigInteger(), nullable=False),
        sa.Column("parity_kind", sa.String(length=16), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("delivery_asset_id", sa.BigInteger(), nullable=False),
        sa.Column("resource_count", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "parity_kind in ('exact', 'adapted', 'baked') and verified",
            name="ck_reference_delivery_style_parities_verified",
        ),
        sa.CheckConstraint(
            "resource_count >= 0 and "
            "((parity_kind = 'adapted' and resource_count > 0) or "
            "(parity_kind in ('exact', 'baked') and resource_count = 0)) and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$' and "
            "octet_length(evidence_json::text) <= 4194304",
            name="ck_reference_delivery_style_parities_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["reference_delivery_versions.id"],
            name="fk_reference_delivery_style_parities_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_item_id"],
            ["reference_style_parity_plan_items.id"],
            name="fk_reference_delivery_style_parities_plan_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_asset_id"],
            ["reference_delivery_assets.id"],
            name="fk_reference_delivery_style_parities_asset",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_delivery_style_parities"),
        ),
        sa.UniqueConstraint(
            "version_id",
            "plan_item_id",
            name="uq_reference_delivery_style_parities_item",
        ),
        sa.UniqueConstraint(
            "version_id",
            "id",
            name="uq_reference_delivery_style_parities_version_id",
        ),
    )
    op.create_index(
        "ix_reference_delivery_style_parities_version",
        "reference_delivery_style_parities",
        ["version_id", "parity_kind", "id"],
    )

    op.create_table(
        "reference_delivery_style_resources",
        sa.Column("delivery_parity_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_resource_id", sa.BigInteger(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["delivery_parity_id"],
            ["reference_delivery_style_parities.id"],
            name="fk_reference_delivery_style_resources_parity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_resource_id"],
            ["reference_style_parity_plan_resources.id"],
            name="fk_reference_delivery_style_resources_plan_resource",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "delivery_parity_id",
            "plan_resource_id",
            name=op.f("pk_reference_delivery_style_resources"),
        ),
    )

    _create_validation_triggers()
    _create_immutable_triggers()
    _backfill_legacy_delivery_versions()


def _create_validation_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION validate_reference_style_parity_plan_item()
        RETURNS trigger AS $func$
        DECLARE
            plan_row reference_style_parity_plans%ROWTYPE;
            style_row reference_layer_styles%ROWTYPE;
            artifact_kind_value text;
        BEGIN
            SELECT * INTO plan_row
            FROM reference_style_parity_plans
            WHERE id = NEW.plan_id;
            IF plan_row.id IS NULL OR NEW.source_id <> plan_row.source_id THEN
                RAISE EXCEPTION 'style parity item has an invalid plan source'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.style_id IS NOT NULL THEN
                SELECT * INTO style_row
                FROM reference_layer_styles
                WHERE id = NEW.style_id;
                IF style_row.id IS NULL
                   OR style_row.provider_key <> plan_row.provider_key
                   OR style_row.layer_id <> plan_row.layer_id
                   OR style_row.last_seen_snapshot_id
                      <> plan_row.catalog_snapshot_id
                   OR style_row.source_key <> NEW.style_source_key
                   OR style_row.remote_name <> NEW.remote_name THEN
                    RAISE EXCEPTION 'style parity item catalog identity differs'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.style_source_key <> '__implicit_default__' THEN
                RAISE EXCEPTION 'implicit style parity identity is invalid'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.source_style_artifact_id IS NOT NULL THEN
                SELECT artifact_kind INTO artifact_kind_value
                FROM reference_source_artifacts
                WHERE source_id = NEW.source_id
                  AND id = NEW.source_style_artifact_id;
                IF artifact_kind_value <> 'style' THEN
                    RAISE EXCEPTION 'style parity SLD artifact is invalid'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            IF NEW.source_package_artifact_id IS NOT NULL THEN
                SELECT artifact_kind INTO artifact_kind_value
                FROM reference_source_artifacts
                WHERE source_id = NEW.source_id
                  AND id = NEW.source_package_artifact_id;
                IF artifact_kind_value <> 'style_package' THEN
                    RAISE EXCEPTION 'style parity package artifact is invalid'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_style_parity_plan_items_validate
        BEFORE INSERT ON reference_style_parity_plan_items
        FOR EACH ROW EXECUTE FUNCTION
            validate_reference_style_parity_plan_item()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_reference_style_parity_plan_resource()
        RETURNS trigger AS $func$
        DECLARE
            item_row reference_style_parity_plan_items%ROWTYPE;
            artifact_row reference_source_artifacts%ROWTYPE;
        BEGIN
            SELECT * INTO item_row
            FROM reference_style_parity_plan_items
            WHERE id = NEW.plan_item_id;
            SELECT * INTO artifact_row
            FROM reference_source_artifacts
            WHERE source_id = NEW.source_id AND id = NEW.artifact_id;
            IF item_row.id IS NULL OR item_row.parity_kind <> 'adapted'
               OR item_row.source_id <> NEW.source_id
               OR artifact_row.id IS NULL
               OR artifact_row.artifact_kind <> 'style_resource'
               OR artifact_row.sha256 <> NEW.sha256
               OR artifact_row.media_type <> NEW.media_type THEN
                RAISE EXCEPTION 'style parity resource evidence is invalid'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_style_parity_plan_resources_validate
        BEFORE INSERT ON reference_style_parity_plan_resources
        FOR EACH ROW EXECUTE FUNCTION
            validate_reference_style_parity_plan_resource()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_reference_delivery_style_parity()
        RETURNS trigger AS $func$
        DECLARE
            version_row reference_delivery_versions%ROWTYPE;
            item_row reference_style_parity_plan_items%ROWTYPE;
            plan_row reference_style_parity_plans%ROWTYPE;
            asset_row reference_delivery_assets%ROWTYPE;
            actual_item_count integer;
            actual_missing_count integer;
            expected_asset_kind text;
        BEGIN
            SELECT * INTO version_row
            FROM reference_delivery_versions WHERE id = NEW.version_id;
            SELECT * INTO item_row
            FROM reference_style_parity_plan_items
            WHERE id = NEW.plan_item_id;
            SELECT * INTO plan_row
            FROM reference_style_parity_plans WHERE id = item_row.plan_id;
            SELECT * INTO asset_row
            FROM reference_delivery_assets WHERE id = NEW.delivery_asset_id;
            SELECT count(*),
                   count(*) FILTER (WHERE parity_kind = 'missing')
            INTO actual_item_count, actual_missing_count
            FROM reference_style_parity_plan_items
            WHERE plan_id = plan_row.id;
            expected_asset_kind := CASE NEW.parity_kind
                WHEN 'exact' THEN 'style_sld'
                WHEN 'adapted' THEN 'style_package'
                WHEN 'baked' THEN 'tile_archive'
            END;
            IF version_row.id IS NULL OR item_row.id IS NULL
               OR plan_row.id IS NULL OR asset_row.id IS NULL
               OR NOT plan_row.complete
               OR actual_item_count <> plan_row.required_style_count
               OR actual_missing_count <> plan_row.missing_style_count
               OR item_row.parity_kind <> NEW.parity_kind
               OR NOT item_row.verified
               OR version_row.provider_key <> plan_row.provider_key
               OR version_row.layer_id <> plan_row.layer_id
               OR version_row.source_id <> plan_row.source_id
               OR version_row.sync_run_id <> plan_row.sync_run_id
               OR version_row.catalog_snapshot_id
                  <> plan_row.catalog_snapshot_id
               OR version_row.catalog_definition_sha256
                  <> plan_row.catalog_definition_sha256
               OR version_row.delivery_kind <> plan_row.delivery_kind
               OR asset_row.version_id <> version_row.id
               OR asset_row.asset_kind <> expected_asset_kind THEN
                RAISE EXCEPTION 'delivery style parity evidence is invalid'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_delivery_style_parities_validate
        BEFORE INSERT ON reference_delivery_style_parities
        FOR EACH ROW EXECUTE FUNCTION
            validate_reference_delivery_style_parity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_reference_delivery_style_resource()
        RETURNS trigger AS $func$
        DECLARE
            parity_item_id bigint;
            parity_kind_value text;
            resource_item_id bigint;
        BEGIN
            SELECT plan_item_id, parity_kind
            INTO parity_item_id, parity_kind_value
            FROM reference_delivery_style_parities
            WHERE id = NEW.delivery_parity_id;
            SELECT plan_item_id INTO resource_item_id
            FROM reference_style_parity_plan_resources
            WHERE id = NEW.plan_resource_id;
            IF parity_item_id IS NULL OR resource_item_id IS NULL
               OR parity_kind_value <> 'adapted'
               OR parity_item_id <> resource_item_id THEN
                RAISE EXCEPTION 'delivery style resource link is invalid'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_delivery_style_resources_validate
        BEFORE INSERT ON reference_delivery_style_resources
        FOR EACH ROW EXECUTE FUNCTION
            validate_reference_delivery_style_resource()
        """
    )


def _create_immutable_triggers() -> None:
    for table, function, row_trigger, truncate_trigger in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE FUNCTION {function}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'reference style parity evidence is immutable'
                    USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {row_trigger}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function}()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {truncate_trigger}
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION {function}()
            """
        )


def _backfill_legacy_delivery_versions() -> None:
    empty_object_sha256 = (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    op.execute(
        f"""
        INSERT INTO reference_style_parity_plans (
            provider_key,
            layer_id,
            catalog_snapshot_id,
            catalog_definition_sha256,
            source_id,
            sync_run_id,
            delivery_kind,
            required_style_count,
            missing_style_count,
            complete,
            evidence_json,
            evidence_sha256,
            created_at
        )
        SELECT
            version.provider_key,
            version.layer_id,
            version.catalog_snapshot_id,
            version.catalog_definition_sha256,
            version.source_id,
            version.sync_run_id,
            version.delivery_kind,
            1,
            1,
            false,
            '{{}}'::json,
            '{empty_object_sha256}',
            version.created_at
        FROM reference_delivery_versions AS version
        ON CONFLICT (source_id, sync_run_id) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO reference_style_parity_plan_items (
            plan_id,
            source_id,
            style_id,
            style_source_key,
            remote_name,
            is_default,
            parity_kind,
            verified,
            source_style_artifact_id,
            source_package_artifact_id,
            resource_count,
            reason_code,
            evidence_json,
            evidence_sha256,
            created_at
        )
        SELECT
            plan.id,
            plan.source_id,
            NULL,
            '__implicit_default__',
            '__implicit_default__',
            true,
            'missing',
            false,
            NULL,
            NULL,
            0,
            'migration_backfill_required',
            '{{}}'::json,
            '{empty_object_sha256}',
            plan.created_at
        FROM reference_style_parity_plans AS plan
        WHERE NOT EXISTS (
            SELECT 1
            FROM reference_style_parity_plan_items AS item
            WHERE item.plan_id = plan.id
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM reference_style_parity_plans
            ) OR EXISTS (
                SELECT 1 FROM reference_source_artifacts
                WHERE artifact_kind IN ('style_package', 'style_resource')
            ) OR EXISTS (
                SELECT 1 FROM reference_sync_run_artifacts
                WHERE role IN ('style_package', 'style_resource')
            ) OR EXISTS (
                SELECT 1 FROM reference_delivery_version_artifacts
                WHERE role IN ('style_package', 'style_resource')
            ) OR EXISTS (
                SELECT 1 FROM reference_delivery_assets
                WHERE asset_kind = 'style_package'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: immutable style parity evidence exists';
            END IF;
        END $$;
        """
    )

    for table, function, row_trigger, truncate_trigger in reversed(
        IMMUTABLE_TABLES
    ):
        op.execute(f"DROP TRIGGER {truncate_trigger} ON {table}")
        op.execute(f"DROP TRIGGER {row_trigger} ON {table}")
        op.execute(f"DROP FUNCTION {function}()")

    for table, trigger, function in (
        (
            "reference_delivery_style_resources",
            "trg_reference_delivery_style_resources_validate",
            "validate_reference_delivery_style_resource",
        ),
        (
            "reference_delivery_style_parities",
            "trg_reference_delivery_style_parities_validate",
            "validate_reference_delivery_style_parity",
        ),
        (
            "reference_style_parity_plan_resources",
            "trg_reference_style_parity_plan_resources_validate",
            "validate_reference_style_parity_plan_resource",
        ),
        (
            "reference_style_parity_plan_items",
            "trg_reference_style_parity_plan_items_validate",
            "validate_reference_style_parity_plan_item",
        ),
    ):
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
        op.execute(f"DROP FUNCTION {function}()")

    for table in PARITY_TABLES:
        op.drop_table(table)

    _replace_mirror_artifact_checks(expanded=False)
