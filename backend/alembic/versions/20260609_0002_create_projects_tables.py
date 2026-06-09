"""create projects tables

Revision ID: 20260609_0002
Revises: 20260608_0001
Create Date: 2026-06-09 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260609_0002"
down_revision: Union[str, None] = "20260608_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('active', 'paused', 'completed', 'archived')",
            name="ck_projects_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "project_users",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "user_id"),
    )
    op.create_index(
        op.f("ix_project_users_user_id"),
        "project_users",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "project_groups",
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "group_id"),
    )
    op.create_index(
        op.f("ix_project_groups_group_id"),
        "project_groups",
        ["group_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_project_groups_group_id"), table_name="project_groups")
    op.drop_table("project_groups")
    op.drop_index(op.f("ix_project_users_user_id"), table_name="project_users")
    op.drop_table("project_users")
    op.drop_table("projects")
