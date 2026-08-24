"""harden reference mirror lifecycle invariants

Revision ID: 20260723_0036
Revises: 20260723_0035
Create Date: 2026-07-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260723_0036"
down_revision: str | None = "20260723_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_TABLES = (
    "reference_source_artifacts",
    "reference_sync_run_artifacts",
    "reference_delivery_versions",
    "reference_delivery_version_artifacts",
    "reference_delivery_assets",
    "reference_delivery_promotions",
)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    inconsistent = bind.execute(
        sa.text(
            """
            WITH heads AS (
              SELECT DISTINCT ON (provider_key, layer_id)
                provider_key, layer_id, id, sequence_number, action,
                to_version_id
              FROM reference_delivery_promotions
              ORDER BY provider_key, layer_id, sequence_number DESC
            )
            SELECT count(*)
            FROM heads
            FULL OUTER JOIN reference_layer_delivery_state AS state
              USING (provider_key, layer_id)
            WHERE heads.id IS NULL
               OR state.last_promotion_id IS NULL
               OR state.last_promotion_id <> heads.id
               OR state.generation <> heads.sequence_number
               OR (
                    heads.action = 'deactivate'
                    AND (
                      state.status <> 'disabled'
                      OR state.active_version_id IS NOT NULL
                    )
                  )
               OR (
                    heads.action <> 'deactivate'
                    AND (
                      state.status <> 'active'
                      OR state.active_version_id IS DISTINCT FROM heads.to_version_id
                    )
                  )
            """
        )
    ).scalar_one()
    if int(inconsistent):
        raise RuntimeError(
            "cannot harden reference mirror: delivery state and promotion "
            f"heads are inconsistent (rows={inconsistent})"
        )

    op.create_check_constraint(
        "ck_reference_sync_runs_observed_bounds",
        "reference_sync_runs",
        "(observed_etag is null or length(observed_etag) <= 4096) and "
        "(observed_version is null or length(observed_version) <= 2048) and "
        "(error_summary is null or length(error_summary) <= 4096) and "
        "octet_length(stats_json::text) <= 1048576",
    )
    op.create_check_constraint(
        "ck_reference_source_artifacts_metadata_bounds",
        "reference_source_artifacts",
        "(source_version is null or length(source_version) <= 2048) and "
        "(upstream_etag is null or length(upstream_etag) <= 4096) and "
        "octet_length(metadata_json::text) <= 4194304",
    )
    op.create_check_constraint(
        "ck_reference_delivery_versions_source_version_bounds",
        "reference_delivery_versions",
        "source_version is null or length(source_version) <= 2048",
    )
    op.create_check_constraint(
        "ck_reference_delivery_assets_metadata_bounds",
        "reference_delivery_assets",
        "octet_length(metadata_json::text) <= 4194304",
    )

    op.drop_constraint(
        "ck_reference_delivery_promotions_shape",
        "reference_delivery_promotions",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_delivery_promotions_action",
        "reference_delivery_promotions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reference_delivery_promotions_action",
        "reference_delivery_promotions",
        "action in ('promote', 'rollback', 'deactivate', 'reactivate')",
    )
    op.create_check_constraint(
        "ck_reference_delivery_promotions_shape",
        "reference_delivery_promotions",
        "(action = 'promote' and to_version_id is not null) or "
        "(action = 'rollback' and from_version_id is not null and "
        "to_version_id is not null) or "
        "(action = 'deactivate' and from_version_id is not null and "
        "to_version_id is null) or "
        "(action = 'reactivate' and from_version_id is null and "
        "to_version_id is not null)",
    )

    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_truncate_immutable
            BEFORE TRUNCATE ON {table_name}
            FOR EACH STATEMENT
            EXECUTE FUNCTION prevent_{_singular(table_name)}_mutation()
            """
        )

    op.execute(
        """
        CREATE FUNCTION validate_reference_delivery_state_head()
        RETURNS trigger AS $$
        DECLARE
          head record;
        BEGIN
          SELECT id, sequence_number, action, to_version_id
            INTO head
          FROM reference_delivery_promotions
          WHERE provider_key = NEW.provider_key
            AND layer_id = NEW.layer_id
          ORDER BY sequence_number DESC
          LIMIT 1;

          IF NOT FOUND
             OR NEW.last_promotion_id <> head.id
             OR NEW.generation <> head.sequence_number
             OR (
                  head.action = 'deactivate'
                  AND (
                    NEW.status <> 'disabled'
                    OR NEW.active_version_id IS NOT NULL
                  )
                )
             OR (
                  head.action <> 'deactivate'
                  AND (
                    NEW.status <> 'active'
                    OR NEW.active_version_id IS DISTINCT FROM head.to_version_id
                  )
                ) THEN
            RAISE EXCEPTION 'reference delivery state must match its promotion head'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_delivery_state_head
        BEFORE INSERT OR UPDATE ON reference_layer_delivery_state
        FOR EACH ROW
        EXECUTE FUNCTION validate_reference_delivery_state_head()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_reference_promotion_state()
        RETURNS trigger AS $$
        DECLARE
          projected record;
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM reference_delivery_promotions
            WHERE provider_key = NEW.provider_key
              AND layer_id = NEW.layer_id
              AND sequence_number > NEW.sequence_number
          ) THEN
            RETURN NEW;
          END IF;

          SELECT status, active_version_id, generation, last_promotion_id
            INTO projected
          FROM reference_layer_delivery_state
          WHERE provider_key = NEW.provider_key
            AND layer_id = NEW.layer_id;

          IF NOT FOUND
             OR projected.last_promotion_id <> NEW.id
             OR projected.generation <> NEW.sequence_number
             OR (
                  NEW.action = 'deactivate'
                  AND (
                    projected.status <> 'disabled'
                    OR projected.active_version_id IS NOT NULL
                  )
                )
             OR (
                  NEW.action <> 'deactivate'
                  AND (
                    projected.status <> 'active'
                    OR projected.active_version_id IS DISTINCT FROM NEW.to_version_id
                  )
                ) THEN
            RAISE EXCEPTION 'promotion head must be projected by delivery state'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_reference_promotion_state
        AFTER INSERT ON reference_delivery_promotions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION validate_reference_promotion_state()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    reactivate_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM reference_delivery_promotions "
            "WHERE action = 'reactivate'"
        )
    ).scalar_one()
    if int(reactivate_count):
        raise RuntimeError(
            "cannot downgrade: reference delivery reactivation history exists "
            f"(rows={reactivate_count})"
        )

    op.execute(
        "DROP TRIGGER trg_reference_promotion_state "
        "ON reference_delivery_promotions"
    )
    op.execute("DROP FUNCTION validate_reference_promotion_state()")
    op.execute(
        "DROP TRIGGER trg_reference_delivery_state_head "
        "ON reference_layer_delivery_state"
    )
    op.execute("DROP FUNCTION validate_reference_delivery_state_head()")

    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(
            f"DROP TRIGGER trg_{table_name}_truncate_immutable "
            f"ON {table_name}"
        )

    op.drop_constraint(
        "ck_reference_delivery_promotions_shape",
        "reference_delivery_promotions",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_delivery_promotions_action",
        "reference_delivery_promotions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reference_delivery_promotions_action",
        "reference_delivery_promotions",
        "action in ('promote', 'rollback', 'deactivate')",
    )
    op.create_check_constraint(
        "ck_reference_delivery_promotions_shape",
        "reference_delivery_promotions",
        "(action = 'promote' and to_version_id is not null) or "
        "(action = 'rollback' and from_version_id is not null and "
        "to_version_id is not null) or "
        "(action = 'deactivate' and from_version_id is not null and "
        "to_version_id is null)",
    )

    op.drop_constraint(
        "ck_reference_delivery_assets_metadata_bounds",
        "reference_delivery_assets",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_delivery_versions_source_version_bounds",
        "reference_delivery_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_source_artifacts_metadata_bounds",
        "reference_source_artifacts",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_sync_runs_observed_bounds",
        "reference_sync_runs",
        type_="check",
    )


def _singular(table_name: str) -> str:
    return {
        "reference_source_artifacts": "reference_source_artifact",
        "reference_sync_run_artifacts": "reference_sync_run_artifact",
        "reference_delivery_versions": "reference_delivery_version",
        "reference_delivery_version_artifacts": (
            "reference_delivery_version_artifact"
        ),
        "reference_delivery_assets": "reference_delivery_asset",
        "reference_delivery_promotions": "reference_delivery_promotion",
    }[table_name]
