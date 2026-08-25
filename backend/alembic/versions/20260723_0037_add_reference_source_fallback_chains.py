"""add sequential reference-source fallback chains

Revision ID: 20260723_0037
Revises: 20260723_0036
Create Date: 2026-07-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260723_0037"
down_revision: str | None = "20260723_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE reference_sync_runs, reference_layer_sources "
        "IN SHARE ROW EXCLUSIVE MODE"
    )

    conflicting_layers = bind.execute(
        sa.text(
            """
            SELECT count(*)
            FROM (
              SELECT source.provider_key, source.layer_id
              FROM reference_sync_runs AS run
              JOIN reference_layer_sources AS source
                ON source.id = run.source_id
              WHERE run.status IN ('queued', 'running')
              GROUP BY source.provider_key, source.layer_id
              HAVING count(*) > 1
            ) AS conflicts
            """
        )
    ).scalar_one()
    if int(conflicting_layers):
        raise RuntimeError(
            "cannot add reference fallback chains: multiple open sync runs "
            "exist for one or more layers; finish or cancel them first "
            f"(layers={conflicting_layers})"
        )

    op.add_column(
        "reference_sync_runs",
        sa.Column("provider_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reference_sync_runs",
        sa.Column("layer_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "reference_sync_runs",
        sa.Column("parent_run_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "reference_sync_runs",
        sa.Column(
            "fallback_depth",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE reference_sync_runs AS run
        SET provider_key = source.provider_key,
            layer_id = source.layer_id
        FROM reference_layer_sources AS source
        WHERE source.id = run.source_id
        """
    )
    op.alter_column(
        "reference_sync_runs",
        "provider_key",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "reference_sync_runs",
        "layer_id",
        existing_type=sa.Integer(),
        nullable=False,
    )

    op.drop_constraint(
        "ck_reference_sync_runs_identity",
        "reference_sync_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reference_sync_runs_identity",
        "reference_sync_runs",
        "source_definition_sha256 ~ '^[0-9a-f]{64}$' and attempt_no > 0 "
        "and expected_active_generation >= 0 and fallback_depth >= 0",
    )
    op.create_check_constraint(
        "ck_reference_sync_runs_fallback_chain",
        "reference_sync_runs",
        "(parent_run_id is null and fallback_depth = 0) or "
        "(parent_run_id is not null and fallback_depth > 0 and "
        "trigger_kind = 'retry')",
    )
    op.create_check_constraint(
        "ck_reference_sync_runs_parent_not_self",
        "reference_sync_runs",
        "parent_run_id is null or parent_run_id <> id",
    )
    op.create_foreign_key(
        "fk_reference_sync_runs_layer_source",
        "reference_sync_runs",
        "reference_layer_sources",
        ["provider_key", "layer_id", "source_id"],
        ["provider_key", "layer_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_reference_sync_runs_layer_id",
        "reference_sync_runs",
        ["provider_key", "layer_id", "id"],
    )
    op.create_foreign_key(
        "fk_reference_sync_runs_parent",
        "reference_sync_runs",
        "reference_sync_runs",
        ["provider_key", "layer_id", "parent_run_id"],
        ["provider_key", "layer_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_reference_sync_runs_open_layer",
        "reference_sync_runs",
        ["provider_key", "layer_id"],
        unique=True,
        postgresql_where=sa.text("status in ('queued', 'running')"),
    )
    op.create_index(
        "uq_reference_sync_runs_fallback_child",
        "reference_sync_runs",
        ["parent_run_id"],
        unique=True,
        postgresql_where=sa.text("parent_run_id is not null"),
    )
    op.create_index(
        "ix_reference_sync_runs_layer_history",
        "reference_sync_runs",
        ["provider_key", "layer_id", "id"],
    )

    op.execute(
        "UPDATE reference_layer_sources SET is_primary = false "
        "WHERE is_primary"
    )
    op.execute(
        """
        WITH preferred AS (
          SELECT DISTINCT ON (provider_key, layer_id)
            id
          FROM reference_layer_sources
          WHERE enabled AND source_key LIKE 'auto:%'
          ORDER BY provider_key, layer_id, priority, source_key, id
        )
        UPDATE reference_layer_sources AS source
        SET is_primary = true
        FROM preferred
        WHERE source.id = preferred.id
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE reference_sync_runs, reference_layer_sources "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    fallback_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM reference_sync_runs "
            "WHERE parent_run_id IS NOT NULL"
        )
    ).scalar_one()
    if int(fallback_count):
        raise RuntimeError(
            "cannot downgrade: reference fallback chain history exists "
            f"(rows={fallback_count})"
        )

    op.drop_index(
        "ix_reference_sync_runs_layer_history",
        table_name="reference_sync_runs",
    )
    op.drop_index(
        "uq_reference_sync_runs_fallback_child",
        table_name="reference_sync_runs",
    )
    op.drop_index(
        "uq_reference_sync_runs_open_layer",
        table_name="reference_sync_runs",
    )
    op.drop_constraint(
        "fk_reference_sync_runs_parent",
        "reference_sync_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reference_sync_runs_layer_source",
        "reference_sync_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_reference_sync_runs_layer_id",
        "reference_sync_runs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_reference_sync_runs_parent_not_self",
        "reference_sync_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_sync_runs_fallback_chain",
        "reference_sync_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_reference_sync_runs_identity",
        "reference_sync_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reference_sync_runs_identity",
        "reference_sync_runs",
        "source_definition_sha256 ~ '^[0-9a-f]{64}$' and attempt_no > 0 "
        "and expected_active_generation >= 0",
    )
    op.drop_column("reference_sync_runs", "fallback_depth")
    op.drop_column("reference_sync_runs", "parent_run_id")
    op.drop_column("reference_sync_runs", "layer_id")
    op.drop_column("reference_sync_runs", "provider_key")
