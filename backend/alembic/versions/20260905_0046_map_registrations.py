"""Persist idempotent map registration receipts without changing existing data."""
from alembic import op
import sqlalchemy as sa

revision = "20260905_0046"
down_revision = "20260904_0045"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "map_registrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("actor_id", "request_key", name="uq_map_registration_request"),
    )


def downgrade():
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE map_registrations IN ACCESS EXCLUSIVE MODE NOWAIT"))
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM map_registrations)")).scalar_one():
        raise RuntimeError("Cannot downgrade while map registration receipts exist; preserve replay protection")
    op.drop_table("map_registrations")
