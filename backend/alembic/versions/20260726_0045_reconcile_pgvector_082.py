"""reconcile pgvector runtime to the reviewed 0.8.2 release

Revision ID: 20260726_0045
Revises: 20260726_0044
Create Date: 2026-07-27

The vector extension is a shared database capability.  This revision refuses
to create or update it unless the server exposes the exact reviewed 0.8.2
control files and an explicit update path from the two supported predecessor
versions.  Downgrades deliberately retain 0.8.2 so application-schema
rollbacks cannot destructively remove or rewrite shared vector objects.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0045"
down_revision: str | None = "20260726_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EXTENSION_NAME = "vector"
_TARGET_VERSION = "0.8.2"
_SUPPORTED_PREDECESSORS = frozenset({"0.8.0", "0.8.1"})


def upgrade() -> None:
    connection = op.get_bind()
    _require_exact_target_files(connection)

    installed_version = connection.execute(
        sa.text(
            "SELECT extversion "
            "FROM pg_extension "
            "WHERE extname = :extension_name"
        ),
        {"extension_name": _EXTENSION_NAME},
    ).scalar_one_or_none()

    if installed_version is None:
        op.execute("CREATE EXTENSION vector VERSION '0.8.2'")
    elif installed_version == _TARGET_VERSION:
        pass
    elif installed_version in _SUPPORTED_PREDECESSORS:
        _require_update_path(connection, installed_version)
        op.execute("ALTER EXTENSION vector UPDATE TO '0.8.2'")
    else:
        raise RuntimeError(
            "Unsupported installed pgvector version "
            f"{installed_version!r}; migration 20260726_0045 only accepts "
            "an absent extension, 0.8.0, 0.8.1, or the exact target 0.8.2."
        )

    _verify_exact_runtime(connection)


def downgrade() -> None:
    # pgvector supplies shared types, operators, and indexes that can outlive
    # this application revision.  Reverting or removing the extension during
    # an application-schema downgrade would risk dependent data, so 0.8.2 is
    # intentionally retained.  Any future extension rollback needs its own
    # audited operational procedure.
    pass


def _require_exact_target_files(connection: sa.engine.Connection) -> None:
    default_version = connection.execute(
        sa.text(
            "SELECT default_version "
            "FROM pg_available_extensions "
            "WHERE name = :extension_name"
        ),
        {"extension_name": _EXTENSION_NAME},
    ).scalar_one_or_none()
    target_version_available = connection.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 "
            "FROM pg_available_extension_versions "
            "WHERE name = :extension_name AND version = :target_version"
            ")"
        ),
        {
            "extension_name": _EXTENSION_NAME,
            "target_version": _TARGET_VERSION,
        },
    ).scalar_one()

    if (
        default_version != _TARGET_VERSION
        or target_version_available is not True
    ):
        raise RuntimeError(
            "The exact pgvector 0.8.2 extension files are unavailable or "
            "not the server default. Deploy the reviewed PostgreSQL 17 image "
            "whose pg_available_extensions.default_version is 0.8.2 and "
            "whose pg_available_extension_versions catalog contains 0.8.2 "
            "before running migration 20260726_0045."
        )


def _require_update_path(
    connection: sa.engine.Connection,
    installed_version: str,
) -> None:
    update_path = connection.execute(
        sa.text(
            "SELECT path "
            "FROM pg_extension_update_paths(:extension_name) "
            "WHERE source = :installed_version "
            "AND target = :target_version"
        ),
        {
            "extension_name": _EXTENSION_NAME,
            "installed_version": installed_version,
            "target_version": _TARGET_VERSION,
        },
    ).scalar_one_or_none()
    if not isinstance(update_path, str) or not update_path.strip():
        raise RuntimeError(
            "No pgvector extension update path is available from "
            f"{installed_version} to {_TARGET_VERSION}; migration "
            "20260726_0045 refuses to modify the installed extension."
        )


def _verify_exact_runtime(connection: sa.engine.Connection) -> None:
    runtime = connection.execute(
        sa.text(
            """
            SELECT
                extension.extversion,
                vector_type.oid AS vector_type_oid,
                EXISTS (
                    SELECT 1
                    FROM pg_operator AS operator
                    JOIN pg_depend AS dependency
                      ON dependency.classid = 'pg_operator'::regclass
                     AND dependency.objid = operator.oid
                     AND dependency.objsubid = 0
                     AND dependency.refclassid = 'pg_extension'::regclass
                     AND dependency.refobjid = extension.oid
                     AND dependency.deptype = 'e'
                    WHERE operator.oprname = '<->'
                      AND operator.oprleft = vector_type.oid
                      AND operator.oprright = vector_type.oid
                ) AS has_l2_operator
            FROM pg_extension AS extension
            LEFT JOIN LATERAL (
                SELECT type.oid
                FROM pg_type AS type
                JOIN pg_depend AS dependency
                  ON dependency.classid = 'pg_type'::regclass
                 AND dependency.objid = type.oid
                 AND dependency.objsubid = 0
                 AND dependency.refclassid = 'pg_extension'::regclass
                 AND dependency.refobjid = extension.oid
                 AND dependency.deptype = 'e'
                WHERE type.typname = 'vector'
                ORDER BY type.oid
                LIMIT 1
            ) AS vector_type ON true
            WHERE extension.extname = :extension_name
            """
        ),
        {"extension_name": _EXTENSION_NAME},
    ).mappings().one_or_none()

    if (
        runtime is None
        or runtime["extversion"] != _TARGET_VERSION
        or runtime["vector_type_oid"] is None
        or runtime["has_l2_operator"] is not True
    ):
        raise RuntimeError(
            "pgvector runtime validation failed after migration "
            "20260726_0045: exact extension version 0.8.2, the extension-owned "
            "vector type, and its L2 distance operator are required."
        )

    operator_works = connection.execute(
        sa.text(
            "SELECT abs(("
            "'[1,2,3]'::vector(3) <-> '[1,2,4]'::vector(3)"
            ") - 1.0) < 0.000000000001"
        )
    ).scalar_one()
    if operator_works is not True:
        raise RuntimeError(
            "pgvector 0.8.2 L2 distance operator failed its runtime check."
        )
