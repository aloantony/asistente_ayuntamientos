"""nest town hall items under epigraphs

Devuelve al Ayuntamiento el cuarto nivel del diseño: pestaña → epígrafe →
apartado → elemento. Hasta ahora los apartados colgaban directamente de la
pestaña, así que cada pestaña existente recibe un epígrafe que hereda su título
y adopta sus apartados. El `block_type` ya admitía `epigraph`, de modo que no
hace falta tocar la restricción. Ver ADR-054.

Revision ID: 20260901_0043
Revises: 20260806_0042
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260901_0043"
down_revision: str | None = "20260806_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()

    # Sólo las pestañas que tengan apartados colgando: una pestaña vacía no
    # necesita epígrafe, y creárselo la ensuciaría.
    sections = connection.execute(
        sa.text(
            """
            SELECT DISTINCT parent.id, parent.organization_id, parent.title,
                            parent.created_by_id, parent.updated_by_id
            FROM municipal_blocks AS parent
            JOIN municipal_blocks AS child ON child.parent_id = parent.id
            WHERE parent.block_type = 'nav_section'
              AND child.block_type = 'nav_item'
            ORDER BY parent.id
            """
        )
    ).all()

    for section_id, organization_id, title, created_by_id, updated_by_id in sections:
        epigraph_id = connection.execute(
            sa.text(
                """
                INSERT INTO municipal_blocks (
                    organization_id, parent_id, block_type, title, body,
                    data_json, position, status, created_by_id, updated_by_id,
                    created_at, updated_at
                )
                VALUES (
                    :organization_id, :parent_id, 'epigraph', :title, NULL,
                    NULL, 0, 'active', :created_by_id, :updated_by_id,
                    now(), now()
                )
                RETURNING id
                """
            ),
            {
                "organization_id": organization_id,
                "parent_id": section_id,
                "title": title,
                "created_by_id": created_by_id,
                "updated_by_id": updated_by_id,
            },
        ).scalar_one()

        connection.execute(
            sa.text(
                """
                UPDATE municipal_blocks
                SET parent_id = :epigraph_id
                WHERE parent_id = :section_id AND block_type = 'nav_item'
                """
            ),
            {"epigraph_id": epigraph_id, "section_id": section_id},
        )


def downgrade() -> None:
    connection = op.get_bind()

    # Los apartados vuelven a colgar de la pestaña. Se conserva su posición
    # relativa: dos epígrafes de la misma pestaña podrían chocar, así que se
    # renumeran de corrido por pestaña.
    connection.execute(
        sa.text(
            """
            UPDATE municipal_blocks AS item
            SET parent_id = epigraph.parent_id
            FROM municipal_blocks AS epigraph
            WHERE item.parent_id = epigraph.id
              AND item.block_type = 'nav_item'
              AND epigraph.block_type = 'epigraph'
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE municipal_blocks AS item
            SET position = ordered.rank - 1
            FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY parent_id ORDER BY position, id
                       ) AS rank
                FROM municipal_blocks
                WHERE block_type = 'nav_item'
            ) AS ordered
            WHERE item.id = ordered.id
            """
        )
    )
    connection.execute(
        sa.text("DELETE FROM municipal_blocks WHERE block_type = 'epigraph'")
    )
