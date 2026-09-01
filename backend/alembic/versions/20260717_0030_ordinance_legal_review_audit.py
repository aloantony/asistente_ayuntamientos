"""add explicit human legal-review audit to ordinances

Revision ID: 20260717_0030
Revises: 20260717_0029
Create Date: 2026-07-17

Technical corpus approval and human legal review are intentionally separate.
Historical rows default to pending legal review unless a current human review
report proves an approval or rejection with a review timestamp.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0030"
down_revision: str | None = "20260717_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ordinances",
        sa.Column(
            "legal_review_status",
            sa.String(length=30),
            server_default=sa.text("'pending_review'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ordinances",
        sa.Column("legal_reviewed_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ordinances",
        sa.Column(
            "legal_reviewed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_ordinances_legal_reviewed_by_id_users",
        "ordinances",
        "users",
        ["legal_reviewed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ordinances_legal_review_status",
        "ordinances",
        ["legal_review_status"],
        unique=False,
    )
    op.create_index(
        "ix_ordinances_legal_reviewed_by_id",
        "ordinances",
        ["legal_reviewed_by_id"],
        unique=False,
    )

    # Only a still-current human report can backfill human review. Agent reports
    # and the Sprint 0 technical bootstrap deliberately remain pending.
    op.execute(
        """
        WITH latest_human_review AS (
            SELECT DISTINCT ON (r.ordinance_id)
                r.ordinance_id,
                r.status,
                r.reviewed_by_id,
                r.reviewed_at
            FROM ordinance_review_reports r
            WHERE r.status IN ('human_approved', 'human_rejected')
              AND r.reviewed_at IS NOT NULL
            ORDER BY r.ordinance_id, r.reviewed_at DESC, r.id DESC
        )
        UPDATE ordinances o
        SET
            legal_review_status = latest_human_review.status,
            legal_reviewed_by_id = latest_human_review.reviewed_by_id,
            legal_reviewed_at = latest_human_review.reviewed_at
        FROM latest_human_review
        WHERE latest_human_review.ordinance_id = o.id
          AND (
              (
                  latest_human_review.status = 'human_approved'
                  AND o.curation_status = 'approved'
              ) OR (
                  latest_human_review.status = 'human_rejected'
                  AND o.curation_status = 'rejected'
              )
          )
        """
    )

    op.create_check_constraint(
        "ck_ordinances_legal_review_status",
        "ordinances",
        "legal_review_status IN ("
        "'pending_review', 'human_approved', 'human_rejected'"
        ")",
    )
    op.create_check_constraint(
        "ck_ordinances_legal_review_audit",
        "ordinances",
        "("
        "legal_review_status = 'pending_review' "
        "AND legal_reviewed_by_id IS NULL "
        "AND legal_reviewed_at IS NULL"
        ") OR ("
        "legal_review_status = 'human_approved' "
        "AND curation_status = 'approved' "
        "AND legal_reviewed_at IS NOT NULL"
        ") OR ("
        "legal_review_status = 'human_rejected' "
        "AND curation_status = 'rejected' "
        "AND legal_reviewed_at IS NOT NULL"
        ")",
    )


def downgrade() -> None:
    has_human_audit = op.get_bind().execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM ordinances
                WHERE legal_review_status <> 'pending_review'
                   OR legal_reviewed_by_id IS NOT NULL
                   OR legal_reviewed_at IS NOT NULL
            )
            """
        )
    ).scalar_one()
    if has_human_audit:
        raise RuntimeError(
            "Cannot downgrade 20260717_0030 while human legal-review audit "
            "data exists; preserve or explicitly reconcile that audit first"
        )

    op.drop_constraint(
        "ck_ordinances_legal_review_audit",
        "ordinances",
        type_="check",
    )
    op.drop_constraint(
        "ck_ordinances_legal_review_status",
        "ordinances",
        type_="check",
    )
    op.drop_index(
        "ix_ordinances_legal_reviewed_by_id",
        table_name="ordinances",
    )
    op.drop_index(
        "ix_ordinances_legal_review_status",
        table_name="ordinances",
    )
    op.drop_constraint(
        "fk_ordinances_legal_reviewed_by_id_users",
        "ordinances",
        type_="foreignkey",
    )
    op.drop_column("ordinances", "legal_reviewed_at")
    op.drop_column("ordinances", "legal_reviewed_by_id")
    op.drop_column("ordinances", "legal_review_status")
