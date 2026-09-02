"""merge legacy town hall tabs into one

El diseño abre el Ayuntamiento con cuatro pestañas —Información, Administración,
Personal y Mapa general— y mete los epígrafes dentro de la primera. El seed de
ADR-053 repartía ese contenido en cuatro pestañas propias, así que una
organización sembrada entonces enseña siete y la fila se parte en dos líneas.

Las pestañas sobrantes de aquel seed se pliegan dentro de «informacion»: sus
epígrafes pasan a colgar de ella y la pestaña vacía se archiva. Sólo se tocan
las que conservan su marca del seed; una pestaña creada a mano no la lleva y se
queda donde está. Cada epígrafe movido anota de dónde viene, de modo que la
vuelta atrás no depende de los títulos, que el ayuntamiento puede haber
cambiado. Ver ADR-056.

Revision ID: 20260903_0044
Revises: 20260901_0043
Create Date: 2026-09-03
"""

import json
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260903_0044"
down_revision: str | None = "20260901_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Pestañas que ADR-053 creaba además de «informacion». Son las que se pliegan.
LEGACY_TAB_KEYS = ("datos", "archivo", "telefonos")
KEEPER_KEY = "informacion"


def _seed_key(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("seed")
    return key if isinstance(key, str) else None


def _merged_from(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    origin = payload.get("merged_from")
    return origin if isinstance(origin, dict) else None


def _with_marker(raw: str | None, marker: dict | None) -> str | None:
    """Conserva lo que ya hubiera en `data_json` y sólo toca la marca."""
    payload: dict = {}
    if raw:
        try:
            loaded = json.loads(raw)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    if marker is None:
        payload.pop("merged_from", None)
    else:
        payload["merged_from"] = marker
    return json.dumps(payload, ensure_ascii=False) if payload else None


def upgrade() -> None:
    connection = op.get_bind()

    sections = connection.execute(
        sa.text(
            """
            SELECT id, organization_id, position, data_json
            FROM municipal_blocks
            WHERE block_type = 'nav_section' AND status = 'active'
            ORDER BY organization_id, position, id
            """
        )
    ).all()

    by_organization: dict[int, list] = {}
    for row in sections:
        by_organization.setdefault(row.organization_id, []).append(row)

    for rows in by_organization.values():
        keeper = next(
            (row for row in rows if _seed_key(row.data_json) == KEEPER_KEY), None
        )
        if keeper is None:
            # Sin la pestaña que recoge no hay dónde plegar: se deja igual.
            continue

        # Los epígrafes nuevos se añaden al final de los que ya tuviera.
        next_position = connection.execute(
            sa.text(
                """
                SELECT COALESCE(MAX(position) + 1, 0)
                FROM municipal_blocks
                WHERE parent_id = :parent AND block_type = 'epigraph'
                """
            ),
            {"parent": keeper.id},
        ).scalar_one()

        for row in rows:
            key = _seed_key(row.data_json)
            if row.id == keeper.id or key not in LEGACY_TAB_KEYS:
                continue

            epigraphs = connection.execute(
                sa.text(
                    """
                    SELECT id, position, data_json
                    FROM municipal_blocks
                    WHERE parent_id = :parent AND block_type = 'epigraph'
                    ORDER BY position, id
                    """
                ),
                {"parent": row.id},
            ).all()

            for epigraph in epigraphs:
                connection.execute(
                    sa.text(
                        """
                        UPDATE municipal_blocks
                        SET parent_id = :parent,
                            position = :position,
                            data_json = :data_json,
                            updated_at = NOW()
                        WHERE id = :id
                        """
                    ),
                    {
                        "parent": keeper.id,
                        "position": next_position,
                        "data_json": _with_marker(
                            epigraph.data_json,
                            {"seed": key, "position": epigraph.position},
                        ),
                        "id": epigraph.id,
                    },
                )
                next_position += 1

            connection.execute(
                sa.text(
                    """
                    UPDATE municipal_blocks
                    SET status = 'archived', updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {"id": row.id},
            )


def downgrade() -> None:
    connection = op.get_bind()

    archived = connection.execute(
        sa.text(
            """
            SELECT id, organization_id, data_json
            FROM municipal_blocks
            WHERE block_type = 'nav_section' AND status = 'archived'
            ORDER BY organization_id, position, id
            """
        )
    ).all()

    for row in archived:
        key = _seed_key(row.data_json)
        if key not in LEGACY_TAB_KEYS:
            continue

        returning = connection.execute(
            sa.text(
                """
                SELECT id, data_json
                FROM municipal_blocks
                WHERE block_type = 'epigraph'
                  AND organization_id = :organization
                """
            ),
            {"organization": row.organization_id},
        ).all()

        moved_back = False
        for epigraph in returning:
            origin = _merged_from(epigraph.data_json)
            if origin is None or origin.get("seed") != key:
                continue
            position = origin.get("position")
            connection.execute(
                sa.text(
                    """
                    UPDATE municipal_blocks
                    SET parent_id = :parent,
                        position = :position,
                        data_json = :data_json,
                        updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {
                    "parent": row.id,
                    "position": position if isinstance(position, int) else 0,
                    "data_json": _with_marker(epigraph.data_json, None),
                    "id": epigraph.id,
                },
            )
            moved_back = True

        if moved_back:
            connection.execute(
                sa.text(
                    """
                    UPDATE municipal_blocks
                    SET status = 'active', updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {"id": row.id},
            )
