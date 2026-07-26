"""allow resource-free, package-bound adapted styles

Revision ID: 20260726_0043
Revises: 20260726_0042
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260726_0043"
down_revision: str | None = "20260726_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLAN_ITEMS = "reference_style_parity_plan_items"
DELIVERY_PARITIES = "reference_delivery_style_parities"
PLAN_CONSTRAINT = "ck_reference_style_parity_plan_items_artifacts"
DELIVERY_CONSTRAINT = "ck_reference_delivery_style_parities_evidence"


def upgrade() -> None:
    _lock_tables()
    op.drop_constraint(
        PLAN_CONSTRAINT,
        PLAN_ITEMS,
        type_="check",
    )
    op.create_check_constraint(
        PLAN_CONSTRAINT,
        PLAN_ITEMS,
        "(parity_kind = 'exact' and "
        "source_style_artifact_id is not null and "
        "source_package_artifact_id is null and resource_count = 0) or "
        "(parity_kind = 'adapted' and "
        "source_style_artifact_id is not null and "
        "source_package_artifact_id is not null and resource_count >= 0) "
        "or (parity_kind in ('baked', 'missing') and "
        "source_style_artifact_id is null and "
        "source_package_artifact_id is null and resource_count = 0)",
    )
    op.drop_constraint(
        DELIVERY_CONSTRAINT,
        DELIVERY_PARITIES,
        type_="check",
    )
    op.create_check_constraint(
        DELIVERY_CONSTRAINT,
        DELIVERY_PARITIES,
        "resource_count >= 0 and "
        "((parity_kind = 'adapted' and resource_count >= 0) or "
        "(parity_kind in ('exact', 'baked') and resource_count = 0)) and "
        "evidence_sha256 ~ '^[0-9a-f]{64}$' and "
        "octet_length(evidence_json::text) <= 4194304",
    )


def downgrade() -> None:
    _lock_tables()
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM {PLAN_ITEMS}
                WHERE parity_kind = 'adapted' AND resource_count = 0
            ) OR EXISTS (
                SELECT 1
                FROM {DELIVERY_PARITIES}
                WHERE parity_kind = 'adapted' AND resource_count = 0
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: resource-free adapted style evidence exists';
            END IF;
        END $$;
        """
    )
    op.drop_constraint(
        PLAN_CONSTRAINT,
        PLAN_ITEMS,
        type_="check",
    )
    op.create_check_constraint(
        PLAN_CONSTRAINT,
        PLAN_ITEMS,
        "(parity_kind = 'exact' and "
        "source_style_artifact_id is not null and "
        "source_package_artifact_id is null and resource_count = 0) or "
        "(parity_kind = 'adapted' and "
        "source_style_artifact_id is not null and "
        "source_package_artifact_id is not null and resource_count > 0) "
        "or (parity_kind in ('baked', 'missing') and "
        "source_style_artifact_id is null and "
        "source_package_artifact_id is null and resource_count = 0)",
    )
    op.drop_constraint(
        DELIVERY_CONSTRAINT,
        DELIVERY_PARITIES,
        type_="check",
    )
    op.create_check_constraint(
        DELIVERY_CONSTRAINT,
        DELIVERY_PARITIES,
        "resource_count >= 0 and "
        "((parity_kind = 'adapted' and resource_count > 0) or "
        "(parity_kind in ('exact', 'baked') and resource_count = 0)) and "
        "evidence_sha256 ~ '^[0-9a-f]{64}$' and "
        "octet_length(evidence_json::text) <= 4194304",
    )


def _lock_tables() -> None:
    op.execute(
        f"LOCK TABLE {PLAN_ITEMS}, {DELIVERY_PARITIES} "
        "IN ACCESS EXCLUSIVE MODE"
    )
