"""create government corporation and municipal staff

Revision ID: 20260803_0034
Revises: 20260717_0033
Create Date: 2026-08-03 00:34:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260803_0034"
down_revision: Union[str, None] = "20260731_0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GOVERNMENT_LEVELS = "'alcaldia', 'tenencia', 'concejalia', 'secretaria'"
GOVERNMENT_MEMBER_STATUSES = "'active', 'archived'"
STAFF_POST_KINDS = "'post', 'container'"
STAFF_WORKER_STATUSES = "'active', 'vacation', 'leave', 'archived'"
STAFF_ABSENCE_TYPES = "'vacation', 'personal', 'sick_leave', 'other'"
STAFF_REPORT_TYPES = "'diary', 'report'"
STAFF_CONTRACT_TYPES = "'permanent', 'temporary', 'interim', 'external', 'other'"
STAFF_HISTORY_EVENT_TYPES = (
    "'created', 'updated', 'status_changed', 'post_changed', 'archived'"
)


def upgrade() -> None:
    op.create_table(
        "government_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=30), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role_title", sa.String(length=255), nullable=False),
        sa.Column("political_group", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("biography", sa.Text(), nullable=True),
        sa.Column("term_start_date", sa.Date(), nullable=True),
        sa.Column("term_end_date", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="active",
            nullable=False,
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            f"level in ({GOVERNMENT_LEVELS})",
            name="ck_government_members_level",
        ),
        sa.CheckConstraint(
            f"status in ({GOVERNMENT_MEMBER_STATUSES})",
            name="ck_government_members_status",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_government_members_sort_order",
        ),
        sa.CheckConstraint(
            "term_end_date is null"
            " or term_start_date is null"
            " or term_end_date >= term_start_date",
            name="ck_government_members_term_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_government_members_org_status_sort",
        "government_members",
        ["organization_id", "status", "sort_order", "id"],
        unique=False,
    )
    for column_name in ("created_by_id", "updated_by_id"):
        op.create_index(
            op.f(f"ix_government_members_{column_name}"),
            "government_members",
            [column_name],
            unique=False,
        )

    op.create_table(
        "staff_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column(
            "kind",
            sa.String(length=20),
            server_default="post",
            nullable=False,
        ),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
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
            f"kind in ({STAFF_POST_KINDS})",
            name="ck_staff_posts_kind",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_staff_posts_sort_order",
        ),
        sa.CheckConstraint(
            "parent_id is null or parent_id <> id",
            name="ck_staff_posts_parent_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_staff_posts_id_org",
        ),
    )
    # El padre se ata por (id, organization_id) para que un puesto no pueda
    # colgar de la plantilla de otro ayuntamiento; la clave compuesta que
    # referencia sólo existe una vez creada la tabla.
    op.create_foreign_key(
        "fk_staff_posts_parent_org",
        "staff_posts",
        "staff_posts",
        ["parent_id", "organization_id"],
        ["id", "organization_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_staff_posts_org_sort",
        "staff_posts",
        ["organization_id", "sort_order", "id"],
        unique=False,
    )

    op.create_table(
        "staff_workers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="active",
            nullable=False,
        ),
        sa.Column("schedule_summary", sa.String(length=255), nullable=True),
        sa.Column("schedule_days", sa.JSON(), nullable=False),
        sa.Column("weekly_hours", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("contract_type", sa.String(length=30), nullable=True),
        sa.Column("contract_start_date", sa.Date(), nullable=True),
        sa.Column("contract_end_date", sa.Date(), nullable=True),
        sa.Column("vacation_days_limit", sa.Integer(), nullable=True),
        sa.Column("personal_days_limit", sa.Integer(), nullable=True),
        sa.Column(
            "bills_invoices",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            f"status in ({STAFF_WORKER_STATUSES})",
            name="ck_staff_workers_status",
        ),
        sa.CheckConstraint(
            "contract_type is null or "
            f"contract_type in ({STAFF_CONTRACT_TYPES})",
            name="ck_staff_workers_contract_type",
        ),
        sa.CheckConstraint(
            "contract_end_date is null"
            " or contract_start_date is null"
            " or contract_end_date >= contract_start_date",
            name="ck_staff_workers_contract_range",
        ),
        sa.CheckConstraint(
            "vacation_days_limit is null or vacation_days_limit >= 0",
            name="ck_staff_workers_vacation_limit",
        ),
        sa.CheckConstraint(
            "personal_days_limit is null or personal_days_limit >= 0",
            name="ck_staff_workers_personal_limit",
        ),
        sa.CheckConstraint(
            "weekly_hours is null or weekly_hours >= 0",
            name="ck_staff_workers_weekly_hours",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["post_id", "organization_id"],
            ["staff_posts.id", "staff_posts.organization_id"],
            name="fk_staff_workers_post_org",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", name="uq_staff_workers_post"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_staff_workers_id_org",
        ),
    )
    op.create_index(
        "ix_staff_workers_org_status",
        "staff_workers",
        ["organization_id", "status", "id"],
        unique=False,
    )
    for column_name in ("created_by_id", "updated_by_id"):
        op.create_index(
            op.f(f"ix_staff_workers_{column_name}"),
            "staff_workers",
            [column_name],
            unique=False,
        )

    op.create_table(
        "staff_absences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("absence_type", sa.String(length=30), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
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
            f"absence_type in ({STAFF_ABSENCE_TYPES})",
            name="ck_staff_absences_type",
        ),
        sa.CheckConstraint(
            "end_date >= start_date",
            name="ck_staff_absences_range",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_absences_worker_org",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_staff_absences_worker_start",
        "staff_absences",
        ["worker_id", "start_date", "id"],
        unique=False,
    )

    op.create_table(
        "staff_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("report_type", sa.String(length=20), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("closing", sa.Text(), nullable=True),
        sa.Column("incident", sa.Text(), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=True),
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
            f"report_type in ({STAFF_REPORT_TYPES})",
            name="ck_staff_reports_type",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_reports_worker_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_staff_reports_worker_date",
        "staff_reports",
        ["worker_id", "report_date", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_staff_reports_author_id"),
        "staff_reports",
        ["author_id"],
        unique=False,
    )

    op.create_table(
        "staff_invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("concept", sa.String(length=255), nullable=False),
        sa.Column("hours", sa.Numeric(precision=7, scale=2), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
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
            "hours is null or hours >= 0",
            name="ck_staff_invoices_hours",
        ),
        sa.CheckConstraint(
            "amount is null or amount >= 0",
            name="ck_staff_invoices_amount",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_invoices_worker_org",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_staff_invoices_worker_date",
        "staff_invoices",
        ["worker_id", "issued_on", "id"],
        unique=False,
    )

    op.create_table(
        "staff_history_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=True),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"event_type in ({STAFF_HISTORY_EVENT_TYPES})",
            name="ck_staff_history_events_type",
        ),
        sa.CheckConstraint(
            f"from_status is null or from_status in ({STAFF_WORKER_STATUSES})",
            name="ck_staff_history_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status is null or to_status in ({STAFF_WORKER_STATUSES})",
            name="ck_staff_history_events_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "organization_id"],
            ["staff_workers.id", "staff_workers.organization_id"],
            name="fk_staff_history_events_worker_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_staff_history_events_worker",
        "staff_history_events",
        ["worker_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_staff_history_events_org_created",
        "staff_history_events",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_staff_history_events_actor_id"),
        "staff_history_events",
        ["actor_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_staff_history_events_actor_id"),
        table_name="staff_history_events",
    )
    op.drop_index(
        "ix_staff_history_events_org_created",
        table_name="staff_history_events",
    )
    op.drop_index(
        "ix_staff_history_events_worker",
        table_name="staff_history_events",
    )
    op.drop_table("staff_history_events")

    op.drop_index("ix_staff_invoices_worker_date", table_name="staff_invoices")
    op.drop_table("staff_invoices")

    op.drop_index(op.f("ix_staff_reports_author_id"), table_name="staff_reports")
    op.drop_index("ix_staff_reports_worker_date", table_name="staff_reports")
    op.drop_table("staff_reports")

    op.drop_index("ix_staff_absences_worker_start", table_name="staff_absences")
    op.drop_table("staff_absences")

    for column_name in reversed(("created_by_id", "updated_by_id")):
        op.drop_index(
            op.f(f"ix_staff_workers_{column_name}"),
            table_name="staff_workers",
        )
    op.drop_index("ix_staff_workers_org_status", table_name="staff_workers")
    op.drop_table("staff_workers")

    op.drop_index("ix_staff_posts_org_sort", table_name="staff_posts")
    op.drop_constraint("fk_staff_posts_parent_org", "staff_posts", type_="foreignkey")
    op.drop_table("staff_posts")

    for column_name in reversed(("created_by_id", "updated_by_id")):
        op.drop_index(
            op.f(f"ix_government_members_{column_name}"),
            table_name="government_members",
        )
    op.drop_index(
        "ix_government_members_org_status_sort",
        table_name="government_members",
    )
    op.drop_table("government_members")
