"""create the durable SIUR mirror strategy matrix

Revision ID: 20260726_0040
Revises: 20260726_0039
Create Date: 2026-07-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0040"
down_revision: str | None = "20260726_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_layer_mirror_strategies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("catalog_definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("strategy_reason_code", sa.String(length=64), nullable=False),
        sa.Column("strategy_reason", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "strategy in ('vector', 'raster', 'tiles', 'composition', 'blocked')",
            name="ck_reference_layer_mirror_strategies_strategy",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(strategy_reason_code) <> ''",
            name="ck_reference_layer_mirror_strategies_reason_code",
        ),
        sa.CheckConstraint(
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$' and generation > 0 and "
            "length(strategy_reason) <= 4096",
            name="ck_reference_layer_mirror_strategies_identity",
        ),
        sa.CheckConstraint(
            "(strategy in ('vector', 'raster', 'tiles') and source_id is not null) "
            "or (strategy in ('composition', 'blocked') and source_id is null)",
            name="ck_reference_layer_mirror_strategies_source_shape",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_mirror_strategies_provider_layer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "catalog_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_layer_mirror_strategies_provider_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["reference_layer_sources.id"],
            name="fk_reference_layer_mirror_strategies_provider_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_layer_mirror_strategies"),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "catalog_snapshot_id",
            name="uq_reference_layer_mirror_strategies_snapshot_layer",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_layer_mirror_strategies_provider_id",
        ),
    )
    op.create_index(
        "ix_reference_layer_mirror_strategies_current",
        "reference_layer_mirror_strategies",
        ["provider_key", "catalog_snapshot_id", "layer_id"],
    )

    op.create_table(
        "reference_layer_mirror_strategy_dependencies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_layer_id", sa.Integer(), nullable=False),
        sa.Column("dependency_layer_id", sa.Integer(), nullable=False),
        sa.Column("dependency_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dependency_order >= 0 and dependency_layer_id <> strategy_layer_id",
            name="ck_reference_layer_mirror_strategy_dependencies_shape",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "strategy_id"],
            [
                "reference_layer_mirror_strategies.provider_key",
                "reference_layer_mirror_strategies.id",
            ],
            name="fk_reference_layer_mirror_strategy_dependencies_strategy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "dependency_layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_mirror_strategy_dependencies_layer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_layer_mirror_strategy_dependencies"),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "strategy_id",
            "dependency_layer_id",
            name="uq_reference_layer_mirror_strategy_dependencies_item",
        ),
    )
    op.create_index(
        "ix_reference_layer_mirror_strategy_dependencies_order",
        "reference_layer_mirror_strategy_dependencies",
        ["strategy_id", "dependency_order"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_reference_layer_mirror_strategy_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'reference mirror strategy evidence is immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_layer_mirror_strategy_immutable
        BEFORE UPDATE OR DELETE ON reference_layer_mirror_strategies
        FOR EACH ROW EXECUTE FUNCTION
            prevent_reference_layer_mirror_strategy_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_layer_mirror_strategy_truncate_immutable
        BEFORE TRUNCATE ON reference_layer_mirror_strategies
        FOR EACH STATEMENT EXECUTE FUNCTION
            prevent_reference_layer_mirror_strategy_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_reference_layer_mirror_strategy_dependency()
        RETURNS trigger AS $func$
        DECLARE
            strategy_layer integer;
            strategy_kind text;
            dependency_kind text;
        BEGIN
            SELECT layer_id, strategy INTO strategy_layer, strategy_kind
            FROM reference_layer_mirror_strategies
            WHERE provider_key = NEW.provider_key AND id = NEW.strategy_id;
            IF strategy_layer IS NULL OR strategy_layer <> NEW.strategy_layer_id
                OR strategy_kind <> 'composition' THEN
                RAISE EXCEPTION 'strategy dependency does not belong to a composition'
                    USING ERRCODE = '23514';
            END IF;
            SELECT strategy INTO dependency_kind
            FROM reference_layer_mirror_strategies s
            WHERE s.provider_key = NEW.provider_key
              AND s.layer_id = NEW.dependency_layer_id
              AND s.catalog_snapshot_id = (
                  SELECT catalog_snapshot_id
                  FROM reference_layer_mirror_strategies
                  WHERE provider_key = NEW.provider_key AND id = NEW.strategy_id
              );
            IF dependency_kind IS NULL OR dependency_kind = 'blocked' THEN
                RAISE EXCEPTION 'composition dependency has no usable strategy'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                WITH RECURSIVE walk(layer_id, path) AS (
                    SELECT NEW.dependency_layer_id,
                           ARRAY[NEW.strategy_layer_id, NEW.dependency_layer_id]
                    UNION ALL
                    SELECT d.dependency_layer_id,
                           w.path || d.dependency_layer_id
                    FROM walk w
                    JOIN reference_layer_mirror_strategies s
                      ON s.provider_key = NEW.provider_key
                     AND s.layer_id = w.layer_id
                     AND s.catalog_snapshot_id = (
                         SELECT catalog_snapshot_id
                         FROM reference_layer_mirror_strategies
                         WHERE provider_key = NEW.provider_key
                           AND id = NEW.strategy_id
                     )
                    JOIN reference_layer_mirror_strategy_dependencies d
                      ON d.provider_key = s.provider_key
                     AND d.strategy_id = s.id
                    WHERE NOT d.dependency_layer_id = ANY(w.path)
                )
                SELECT 1 FROM walk WHERE layer_id = NEW.strategy_layer_id
            ) THEN
                RAISE EXCEPTION 'composition dependency cycle detected'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_layer_mirror_strategy_dependency_validate
        AFTER INSERT OR UPDATE ON reference_layer_mirror_strategy_dependencies
        FOR EACH ROW EXECUTE FUNCTION
            validate_reference_layer_mirror_strategy_dependency()
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_reference_layer_mirror_strategy_dependency_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'reference mirror strategy dependencies are immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_layer_mirror_strategy_dependency_immutable
        BEFORE UPDATE OR DELETE ON reference_layer_mirror_strategy_dependencies
        FOR EACH ROW EXECUTE FUNCTION
            prevent_reference_layer_mirror_strategy_dependency_mutation()
        """
    )

    # Existing deployments must fail closed until the next reviewed bootstrap
    # derives a real strategy. The row is immutable and deliberately marked as
    # a migration backfill rather than inferred as a safe delivery.
    op.execute(
        """
        INSERT INTO reference_layer_mirror_strategies (
            provider_key, layer_id, catalog_snapshot_id,
            catalog_definition_sha256, strategy, source_id,
            strategy_reason_code, strategy_reason, evidence_json,
            evidence_sha256, generation, validated_at
        )
        SELECT l.provider_key, l.id, l.last_seen_snapshot_id,
               s.definition_sha256, 'blocked', NULL,
               'migration_backfill_required',
               'strategy matrix must be reconciled after migration',
               '{}'::json,
               '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
               1, now()
        FROM reference_layers l
        JOIN reference_catalog_snapshots s
          ON s.provider_key = l.provider_key AND s.id = l.last_seen_snapshot_id
        WHERE l.node_type = 'layer'
          AND NOT EXISTS (
              SELECT 1 FROM reference_layer_mirror_strategies existing
              WHERE existing.provider_key = l.provider_key
                AND existing.layer_id = l.id
                AND existing.catalog_snapshot_id = l.last_seen_snapshot_id
          )
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_reference_layer_mirror_strategy_dependency_immutable "
        "ON reference_layer_mirror_strategy_dependencies"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_reference_layer_mirror_strategy_dependency_validate "
        "ON reference_layer_mirror_strategy_dependencies"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_reference_layer_mirror_strategy_truncate_immutable "
        "ON reference_layer_mirror_strategies"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_reference_layer_mirror_strategy_immutable "
        "ON reference_layer_mirror_strategies"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "prevent_reference_layer_mirror_strategy_dependency_mutation()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS validate_reference_layer_mirror_strategy_dependency()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS prevent_reference_layer_mirror_strategy_mutation()"
    )
    op.drop_index(
        "ix_reference_layer_mirror_strategy_dependencies_order",
        table_name="reference_layer_mirror_strategy_dependencies",
    )
    op.drop_table("reference_layer_mirror_strategy_dependencies")
    op.drop_index(
        "ix_reference_layer_mirror_strategies_current",
        table_name="reference_layer_mirror_strategies",
    )
    op.drop_table("reference_layer_mirror_strategies")
