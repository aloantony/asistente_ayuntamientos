"""remove only the synthetic mirror-strategy migration placeholders

Revision ID: 20260727_0046
Revises: 20260726_0045
Create Date: 2026-07-27

Revision 0040 deliberately inserted blocked rows so an existing deployment
could not serve a layer before deriving a reviewed strategy.  Those synthetic
rows use the same immutable table and snapshot uniqueness key as the real
strategy that the bootstrap subsequently derives, so leaving them in place
prevents that bootstrap from ever reconciling the snapshot.

This successor removes only the complete, exact 0040 placeholder fingerprint.
It takes an exclusive table lock, verifies the expected immutable trigger,
rejects altered placeholders or unexpected dependencies, and disables that
single trigger only for the bounded delete.  Real strategy evidence is never
rewritten or inferred.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0046"
down_revision: str | None = "20260726_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STRATEGY_TABLE = "reference_layer_mirror_strategies"
_DEPENDENCY_TABLE = "reference_layer_mirror_strategy_dependencies"
_IMMUTABLE_TRIGGER = "trg_reference_layer_mirror_strategy_immutable"
_IMMUTABLE_FUNCTION = "prevent_reference_layer_mirror_strategy_mutation"
_PLACEHOLDER_REASON_CODE = "migration_backfill_required"
_PLACEHOLDER_REASON = "strategy matrix must be reconciled after migration"
_EMPTY_JSON_SHA256 = (
    "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
)
_EXPECTED_TRIGGER_TYPE = 27  # ROW | BEFORE | DELETE | UPDATE

_PLACEHOLDER_PREDICATE = """
strategy.strategy = 'blocked'
AND strategy.source_id IS NULL
AND strategy.strategy_reason_code = :reason_code
AND strategy.strategy_reason = :reason
AND strategy.evidence_json::jsonb = '{}'::jsonb
AND strategy.evidence_sha256 = :empty_json_sha256
AND strategy.generation = 1
AND strategy.validated_at = strategy.created_at
AND EXISTS (
    SELECT 1
    FROM reference_catalog_snapshots AS snapshot
    WHERE snapshot.provider_key = strategy.provider_key
      AND snapshot.id = strategy.catalog_snapshot_id
      AND snapshot.definition_sha256 =
          strategy.catalog_definition_sha256
)
"""


def upgrade() -> None:
    connection = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"LOCK TABLE {_STRATEGY_TABLE}, {_DEPENDENCY_TABLE} "
        "IN ACCESS EXCLUSIVE MODE"
    )

    _require_expected_immutable_trigger(connection)
    parameters = _placeholder_parameters()

    altered_count = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {_STRATEGY_TABLE} AS strategy
            WHERE strategy.strategy_reason_code = :reason_code
              AND NOT ({_PLACEHOLDER_PREDICATE})
            """
        ),
        parameters,
    ).scalar_one()
    if int(altered_count):
        raise RuntimeError(
            "cannot reconcile mirror strategy placeholders: "
            "migration_backfill_required rows do not match the exact "
            f"0040 fingerprint (rows={altered_count})"
        )

    dependency_count = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {_DEPENDENCY_TABLE} AS dependency
            JOIN {_STRATEGY_TABLE} AS strategy
              ON strategy.provider_key = dependency.provider_key
             AND strategy.id = dependency.strategy_id
            WHERE {_PLACEHOLDER_PREDICATE}
            """
        ),
        parameters,
    ).scalar_one()
    if int(dependency_count):
        raise RuntimeError(
            "cannot reconcile mirror strategy placeholders: "
            "synthetic 0040 rows have unexpected dependencies "
            f"(rows={dependency_count})"
        )

    placeholder_count = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {_STRATEGY_TABLE} AS strategy
            WHERE {_PLACEHOLDER_PREDICATE}
            """
        ),
        parameters,
    ).scalar_one()

    op.execute(
        f"ALTER TABLE {_STRATEGY_TABLE} "
        f"DISABLE TRIGGER {_IMMUTABLE_TRIGGER}"
    )
    deleted = connection.execute(
        sa.text(
            f"""
            DELETE FROM {_STRATEGY_TABLE} AS strategy
            WHERE {_PLACEHOLDER_PREDICATE}
            """
        ),
        parameters,
    ).rowcount
    op.execute(
        f"ALTER TABLE {_STRATEGY_TABLE} "
        f"ENABLE TRIGGER {_IMMUTABLE_TRIGGER}"
    )

    if deleted != int(placeholder_count):
        raise RuntimeError(
            "mirror strategy placeholder reconciliation changed an "
            "unexpected number of rows "
            f"(expected={placeholder_count}, deleted={deleted})"
        )
    _require_expected_immutable_trigger(connection)

    remaining_count = connection.execute(
        sa.text(
            f"""
            SELECT count(*)
            FROM {_STRATEGY_TABLE}
            WHERE strategy_reason_code = :reason_code
            """
        ),
        {"reason_code": _PLACEHOLDER_REASON_CODE},
    ).scalar_one()
    if int(remaining_count):
        raise RuntimeError(
            "mirror strategy placeholder reconciliation left synthetic "
            f"rows behind (rows={remaining_count})"
        )


def downgrade() -> None:
    # The removed rows were synthetic blockers rather than reviewed evidence.
    # Recreating them would require inventing their original identities and
    # timestamps and would reintroduce the bootstrap deadlock.  A downgrade is
    # therefore deliberately non-destructive and leaves real strategies and
    # the reconciled absence of placeholders intact.
    pass


def _placeholder_parameters() -> dict[str, str]:
    return {
        "reason_code": _PLACEHOLDER_REASON_CODE,
        "reason": _PLACEHOLDER_REASON,
        "empty_json_sha256": _EMPTY_JSON_SHA256,
    }


def _require_expected_immutable_trigger(
    connection: sa.engine.Connection,
) -> None:
    trigger = connection.execute(
        sa.text(
            """
            SELECT
                trigger.tgenabled,
                trigger.tgisinternal,
                trigger.tgtype,
                procedure.proname,
                procedure.prorettype = 'trigger'::regtype AS returns_trigger,
                pg_get_function_identity_arguments(procedure.oid) AS arguments,
                procedure_namespace.oid = table_namespace.oid AS same_schema
            FROM pg_trigger AS trigger
            JOIN pg_class AS target
              ON target.oid = trigger.tgrelid
            JOIN pg_namespace AS table_namespace
              ON table_namespace.oid = target.relnamespace
            JOIN pg_proc AS procedure
              ON procedure.oid = trigger.tgfoid
            JOIN pg_namespace AS procedure_namespace
              ON procedure_namespace.oid = procedure.pronamespace
            WHERE trigger.tgname = :trigger_name
              AND trigger.tgrelid = CAST(:table_name AS regclass)
            """
        ),
        {
            "trigger_name": _IMMUTABLE_TRIGGER,
            "table_name": _STRATEGY_TABLE,
        },
    ).mappings().one_or_none()
    if (
        trigger is None
        or trigger["tgenabled"] != "O"
        or trigger["tgisinternal"] is not False
        or int(trigger["tgtype"]) != _EXPECTED_TRIGGER_TYPE
        or trigger["proname"] != _IMMUTABLE_FUNCTION
        or trigger["returns_trigger"] is not True
        or trigger["arguments"] != ""
        or trigger["same_schema"] is not True
    ):
        raise RuntimeError(
            "cannot reconcile mirror strategy placeholders: the expected "
            "enabled immutable strategy trigger is missing or altered"
        )
