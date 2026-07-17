"""create municipal maintenance orders

Revision ID: 20260715_0023
Revises: 20260715_0022
Create Date: 2026-07-15 00:23:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260715_0023"
down_revision: Union[str, None] = "20260715_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_municipal_assets_id_org_municipality",
        "municipal_assets",
        ["id", "organization_id", "municipality_id"],
    )

    op.create_table(
        "maintenance_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("municipality_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "maintenance_type",
            sa.String(length=30),
            server_default="other",
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.String(length=20),
            server_default="normal",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="planned",
            nullable=False,
        ),
        sa.Column("scheduled_for", sa.Date(), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        sa.Column("assigned_to_id", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
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
            "btrim(title) <> ''",
            name="ck_maintenance_orders_title",
        ),
        sa.CheckConstraint(
            "maintenance_type in "
            "('preventive', 'corrective', 'inspection', 'cleaning', 'other')",
            name="ck_maintenance_orders_type",
        ),
        sa.CheckConstraint(
            "priority in ('low', 'normal', 'high', 'urgent')",
            name="ck_maintenance_orders_priority",
        ),
        sa.CheckConstraint(
            "status in "
            "('planned', 'scheduled', 'in_progress', 'completed', 'cancelled')",
            name="ck_maintenance_orders_status",
        ),
        sa.CheckConstraint(
            "estimated_minutes is null or estimated_minutes > 0",
            name="ck_maintenance_orders_estimated_minutes",
        ),
        sa.CheckConstraint(
            "status <> 'scheduled' or scheduled_for is not null",
            name="ck_maintenance_orders_scheduled_date",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "municipality_id"],
            ["organizations.id", "organizations.municipality_id"],
            name="fk_maintenance_orders_organization_municipality",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"],
            ["municipalities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "organization_id", "municipality_id"],
            [
                "municipal_assets.id",
                "municipal_assets.organization_id",
                "municipal_assets.municipality_id",
            ],
            name="fk_maintenance_orders_asset_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name="uq_maintenance_orders_id_org",
        ),
    )
    op.create_index(
        "ix_maintenance_orders_org_status_scheduled",
        "maintenance_orders",
        ["organization_id", "status", "scheduled_for", "id"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_orders_asset_status",
        "maintenance_orders",
        ["asset_id", "status", "scheduled_for", "id"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_orders_assigned_status_scheduled",
        "maintenance_orders",
        ["assigned_to_id", "status", "scheduled_for", "id"],
        unique=False,
    )
    for column_name in (
        "municipality_id",
        "created_by_id",
        "updated_by_id",
    ):
        op.create_index(
            op.f(f"ix_maintenance_orders_{column_name}"),
            "maintenance_orders",
            [column_name],
            unique=False,
        )

    op.create_table(
        "maintenance_order_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=True),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type in ('created', 'updated', 'transition')",
            name="ck_maintenance_order_events_type",
        ),
        sa.CheckConstraint(
            "from_status is null or from_status in "
            "('planned', 'scheduled', 'in_progress', 'completed', 'cancelled')",
            name="ck_maintenance_order_events_from_status",
        ),
        sa.CheckConstraint(
            "to_status is null or to_status in "
            "('planned', 'scheduled', 'in_progress', 'completed', 'cancelled')",
            name="ck_maintenance_order_events_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["maintenance_orders.id", "maintenance_orders.organization_id"],
            name="fk_maintenance_order_events_order_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_maintenance_order_events_order_id",
        "maintenance_order_events",
        ["order_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_order_events_org_created",
        "maintenance_order_events",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_maintenance_order_events_actor_id"),
        "maintenance_order_events",
        ["actor_id"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION prevent_maintenance_order_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'maintenance order events are immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_maintenance_order_events_immutable
        BEFORE UPDATE OR DELETE ON maintenance_order_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_maintenance_order_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_maintenance_order_events_immutable "
        "ON maintenance_order_events"
    )
    op.drop_index(
        op.f("ix_maintenance_order_events_actor_id"),
        table_name="maintenance_order_events",
    )
    op.drop_index(
        "ix_maintenance_order_events_org_created",
        table_name="maintenance_order_events",
    )
    op.drop_index(
        "ix_maintenance_order_events_order_id",
        table_name="maintenance_order_events",
    )
    op.drop_table("maintenance_order_events")
    op.execute("DROP FUNCTION prevent_maintenance_order_event_mutation()")

    for column_name in reversed(
        (
            "municipality_id",
            "created_by_id",
            "updated_by_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_maintenance_orders_{column_name}"),
            table_name="maintenance_orders",
        )
    op.drop_index(
        "ix_maintenance_orders_assigned_status_scheduled",
        table_name="maintenance_orders",
    )
    op.drop_index(
        "ix_maintenance_orders_asset_status",
        table_name="maintenance_orders",
    )
    op.drop_index(
        "ix_maintenance_orders_org_status_scheduled",
        table_name="maintenance_orders",
    )
    op.drop_table("maintenance_orders")
    op.drop_constraint(
        "uq_municipal_assets_id_org_municipality",
        "municipal_assets",
        type_="unique",
    )
