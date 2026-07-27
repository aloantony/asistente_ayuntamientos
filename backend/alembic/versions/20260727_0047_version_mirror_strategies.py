"""allow immutable mirror-strategy evolution by complete generations

Revision ID: 20260727_0047
Revises: 20260727_0046
Create Date: 2026-07-27

Revision 0040 made every strategy row immutable, but its uniqueness key allowed
only one row per layer and snapshot.  This successor keeps every reviewed row
and changes the identity to provider/snapshot/generation/layer.  Composition
dependencies are accepted only when their target strategy belongs to the same
snapshot and generation.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0047"
down_revision: str | None = "20260727_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STRATEGY_TABLE = "reference_layer_mirror_strategies"
_DEPENDENCY_TABLE = "reference_layer_mirror_strategy_dependencies"
_OLD_UNIQUE = "uq_reference_layer_mirror_strategies_snapshot_layer"
_GENERATION_UNIQUE = (
    "uq_reference_layer_mirror_strategies_snapshot_generation_layer"
)
_CURRENT_INDEX = "ix_reference_layer_mirror_strategies_current"

_GENERATION_AWARE_DEPENDENCY_FUNCTION = """
CREATE OR REPLACE FUNCTION validate_reference_layer_mirror_strategy_dependency()
RETURNS trigger AS $func$
DECLARE
    strategy_layer integer;
    strategy_kind text;
    strategy_snapshot integer;
    strategy_generation bigint;
    dependency_kind text;
BEGIN
    SELECT layer_id, strategy, catalog_snapshot_id, generation
      INTO strategy_layer, strategy_kind, strategy_snapshot,
           strategy_generation
    FROM reference_layer_mirror_strategies
    WHERE provider_key = NEW.provider_key AND id = NEW.strategy_id;
    IF strategy_layer IS NULL OR strategy_layer <> NEW.strategy_layer_id
        OR strategy_kind <> 'composition' THEN
        RAISE EXCEPTION 'strategy dependency does not belong to a composition'
            USING ERRCODE = '23514';
    END IF;

    SELECT strategy INTO dependency_kind
    FROM reference_layer_mirror_strategies AS dependency
    WHERE dependency.provider_key = NEW.provider_key
      AND dependency.layer_id = NEW.dependency_layer_id
      AND dependency.catalog_snapshot_id = strategy_snapshot
      AND dependency.generation = strategy_generation;
    IF dependency_kind IS NULL OR dependency_kind = 'blocked' THEN
        RAISE EXCEPTION
            'composition dependency has no usable strategy in the same generation'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        WITH RECURSIVE walk(layer_id, path) AS (
            SELECT NEW.dependency_layer_id,
                   ARRAY[NEW.strategy_layer_id, NEW.dependency_layer_id]
            UNION ALL
            SELECT dependency.dependency_layer_id,
                   walk.path || dependency.dependency_layer_id
            FROM walk
            JOIN reference_layer_mirror_strategies AS strategy
              ON strategy.provider_key = NEW.provider_key
             AND strategy.layer_id = walk.layer_id
             AND strategy.catalog_snapshot_id = strategy_snapshot
             AND strategy.generation = strategy_generation
            JOIN reference_layer_mirror_strategy_dependencies AS dependency
              ON dependency.provider_key = strategy.provider_key
             AND dependency.strategy_id = strategy.id
            WHERE NOT dependency.dependency_layer_id = ANY(walk.path)
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

_GENERATION_ONE_DEPENDENCY_FUNCTION = """
CREATE OR REPLACE FUNCTION validate_reference_layer_mirror_strategy_dependency()
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


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"LOCK TABLE {_STRATEGY_TABLE}, {_DEPENDENCY_TABLE} "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.drop_constraint(_OLD_UNIQUE, _STRATEGY_TABLE, type_="unique")
    op.create_unique_constraint(
        _GENERATION_UNIQUE,
        _STRATEGY_TABLE,
        [
            "provider_key",
            "catalog_snapshot_id",
            "generation",
            "layer_id",
        ],
    )
    op.drop_index(_CURRENT_INDEX, table_name=_STRATEGY_TABLE)
    op.create_index(
        _CURRENT_INDEX,
        _STRATEGY_TABLE,
        [
            "provider_key",
            "catalog_snapshot_id",
            "generation",
            "layer_id",
        ],
    )
    op.execute(_GENERATION_AWARE_DEPENDENCY_FUNCTION)


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"LOCK TABLE {_STRATEGY_TABLE}, {_DEPENDENCY_TABLE} "
        "IN ACCESS EXCLUSIVE MODE"
    )
    later_generation_count = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {_STRATEGY_TABLE}
            WHERE generation > 1
            """
        )
    ).scalar_one()
    if int(later_generation_count):
        raise RuntimeError(
            "cannot downgrade mirror strategy generations while immutable "
            "evidence with generation > 1 exists "
            f"(rows={later_generation_count})"
        )

    op.drop_constraint(
        _GENERATION_UNIQUE,
        _STRATEGY_TABLE,
        type_="unique",
    )
    op.create_unique_constraint(
        _OLD_UNIQUE,
        _STRATEGY_TABLE,
        ["provider_key", "layer_id", "catalog_snapshot_id"],
    )
    op.drop_index(_CURRENT_INDEX, table_name=_STRATEGY_TABLE)
    op.create_index(
        _CURRENT_INDEX,
        _STRATEGY_TABLE,
        ["provider_key", "catalog_snapshot_id", "layer_id"],
    )
    op.execute(_GENERATION_ONE_DEPENDENCY_FUNCTION)
