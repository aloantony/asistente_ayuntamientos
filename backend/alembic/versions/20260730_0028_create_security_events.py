"""create the immutable security event log

Revision ID: 20260730_0028
Revises: 20260729_0027
Create Date: 2026-07-30 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260730_0028"
down_revision: Union[str, None] = "20260729_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IMMUTABLE_FUNCTION = "prevent_security_event_mutation"
IMMUTABLE_TRIGGER = "trg_security_events_immutable"


def upgrade() -> None:
    op.create_table(
        "security_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.String(length=320), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("client_ip", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("target_type", sa.String(length=50), nullable=True),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('success', 'failure', 'blocked')",
            name="ck_security_events_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_security_events_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_security_events_organization_id_organizations",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_events_user_id",
        "security_events",
        ["user_id"],
    )
    op.create_index(
        "ix_security_events_organization_id",
        "security_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_security_events_created_at",
        "security_events",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_security_events_type_created",
        "security_events",
        ["event_type", "created_at"],
    )

    # Inmutable en la base de datos: una traza que la aplicación pueda reescribir
    # o borrar no sirve como prueba ante un incidente. Mismo patrón que
    # maintenance_order_events. La purga por retención desactiva el trigger de
    # forma explícita (ops/purge_security_events.sh).
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {IMMUTABLE_FUNCTION}()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'security events are immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {IMMUTABLE_TRIGGER}
        BEFORE UPDATE OR DELETE ON security_events
        FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE_FUNCTION}();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {IMMUTABLE_TRIGGER} ON security_events")
    op.execute(f"DROP FUNCTION IF EXISTS {IMMUTABLE_FUNCTION}()")
    op.drop_index("ix_security_events_type_created", table_name="security_events")
    op.drop_index("ix_security_events_created_at", table_name="security_events")
    op.drop_index("ix_security_events_organization_id", table_name="security_events")
    op.drop_index("ix_security_events_user_id", table_name="security_events")
    op.drop_table("security_events")
