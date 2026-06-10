"""add organizations tenancy

Revision ID: 20260610_0003
Revises: 20260609_0002
Create Date: 2026-06-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260610_0003"
down_revision: Union[str, None] = "20260609_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('active', 'paused', 'archived')",
            name="ck_organizations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "organization_users",
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )
    op.create_index(
        op.f("ix_organization_users_user_id"),
        "organization_users",
        ["user_id"],
        unique=False,
    )

    op.add_column(
        "groups",
        sa.Column("organization_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_groups_organization_id"),
        "groups",
        ["organization_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_groups_organization_id_organizations",
        "groups",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "projects",
        sa.Column("organization_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_projects_organization_id"),
        "projects",
        ["organization_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_projects_organization_id_organizations",
        "projects",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    bind = op.get_bind()
    default_organization_id = bind.execute(
        sa.text(
            """
            insert into organizations (name, description, status)
            values (
                'Default organization',
                'Automatically created for existing data during tenancy migration',
                'active'
            )
            returning id
            """
        )
    ).scalar_one()

    bind.execute(
        sa.text(
            """
            insert into organization_users (organization_id, user_id)
            select :organization_id, users.id
            from users
            """
        ),
        {"organization_id": default_organization_id},
    )
    bind.execute(
        sa.text(
            """
            update groups
            set organization_id = :organization_id
            where organization_id is null
            """
        ),
        {"organization_id": default_organization_id},
    )
    bind.execute(
        sa.text(
            """
            update projects
            set organization_id = :organization_id
            where organization_id is null
            """
        ),
        {"organization_id": default_organization_id},
    )

    op.alter_column("groups", "organization_id", nullable=False)
    op.alter_column("projects", "organization_id", nullable=False)


def downgrade() -> None:
    op.drop_constraint(
        "fk_projects_organization_id_organizations",
        "projects",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_projects_organization_id"), table_name="projects")
    op.drop_column("projects", "organization_id")

    op.drop_constraint(
        "fk_groups_organization_id_organizations",
        "groups",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_groups_organization_id"), table_name="groups")
    op.drop_column("groups", "organization_id")

    op.drop_index(op.f("ix_organization_users_user_id"), table_name="organization_users")
    op.drop_table("organization_users")
    op.drop_table("organizations")
