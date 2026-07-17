"""reconcile ordinance defaults and municipality reference geography

Revision ID: 20260717_0029
Revises: 20260716_0028
Create Date: 2026-07-17

The old geography migration was applied to persistent databases with the same
revision ID later published for the ordinance default migration.  This
successor keeps the published graph canonical while safely adopting the exact
legacy schema when it is already present.
"""

from collections.abc import Sequence
import importlib.util
from pathlib import Path
import re
from types import ModuleType

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection

revision: str = "20260717_0029"
down_revision: str | None = "20260716_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_FILENAME = "20260716_0026_add_municipality_reference_geography.py"
_CREATED_SCHEMA_COMMENT = "alembic:20260717_0029:created-reference-geography"
_MANAGED_TABLES = {
    "reference_dataset_versions",
    "municipality_geography_snapshots",
}

# (data_type, character_length, numeric_precision, numeric_scale, nullable,
#  normalized_default)
_EXPECTED_COLUMNS: dict[
    str,
    dict[str, tuple[str, int | None, int | None, int | None, bool, str | None]],
] = {
    "municipalities": {
        "ine_check_digit": ("character varying", 1, None, None, True, None),
        "directory_reference_date": ("date", None, None, None, True, None),
        "directory_source_url": ("text", None, None, None, True, None),
        "directory_source_sha256": (
            "character varying",
            64,
            None,
            None,
            True,
            None,
        ),
    },
    "reference_dataset_versions": {
        "id": ("integer", None, 32, 0, False, "sequence"),
        "dataset_key": ("character varying", 80, None, None, False, None),
        "title": ("character varying", 255, None, None, False, None),
        "version_label": ("character varying", 100, None, None, False, None),
        "reference_date": ("date", None, None, None, False, None),
        "catalog_url": ("text", None, None, None, False, None),
        "download_url": ("text", None, None, None, False, None),
        "member_name": ("character varying", 255, None, None, False, None),
        "archive_sha256": ("character varying", 64, None, None, False, None),
        "content_sha256": ("character varying", 64, None, None, False, None),
        "license_name": ("character varying", 100, None, None, False, None),
        "license_url": ("text", None, None, None, False, None),
        "attribution": ("text", None, None, None, False, None),
        "retrieved_at": (
            "timestamp with time zone",
            None,
            None,
            None,
            False,
            None,
        ),
        "national_row_count": ("integer", None, 32, 0, False, None),
        "target_row_count": ("integer", None, 32, 0, False, None),
        "created_at": (
            "timestamp with time zone",
            None,
            None,
            None,
            False,
            "now",
        ),
        "updated_at": (
            "timestamp with time zone",
            None,
            None,
            None,
            False,
            "now",
        ),
    },
    "municipality_geography_snapshots": {
        "id": ("integer", None, 32, 0, False, "sequence"),
        "municipality_id": ("integer", None, 32, 0, False, None),
        "dataset_version_id": ("integer", None, 32, 0, False, None),
        "is_current": ("boolean", None, None, None, False, "true"),
        "source_municipality_code": (
            "character varying",
            11,
            None,
            None,
            False,
            None,
        ),
        "relationship_id": ("integer", None, 32, 0, False, None),
        "geographic_code": ("character varying", 5, None, None, False, None),
        "source_province_code": (
            "character varying",
            2,
            None,
            None,
            False,
            None,
        ),
        "source_province_name": (
            "character varying",
            255,
            None,
            None,
            False,
            None,
        ),
        "source_municipality_name": (
            "character varying",
            255,
            None,
            None,
            False,
            None,
        ),
        "source_population": ("integer", None, 32, 0, False, None),
        "surface_km2": ("numeric", None, 12, 6, False, None),
        "perimeter_m": ("numeric", None, 14, 3, False, None),
        "capital_ine_code": (
            "character varying",
            11,
            None,
            None,
            False,
            None,
        ),
        "capital_name": ("character varying", 255, None, None, False, None),
        "capital_population": ("integer", None, 32, 0, False, None),
        "mtn25_sheet": ("character varying", 50, None, None, False, None),
        "longitude": ("numeric", None, 12, 9, False, None),
        "latitude": ("numeric", None, 12, 9, False, None),
        "coordinate_origin": (
            "character varying",
            100,
            None,
            None,
            False,
            None,
        ),
        "altitude_m": ("numeric", None, 8, 2, False, None),
        "altitude_origin": (
            "character varying",
            100,
            None,
            None,
            False,
            None,
        ),
        "crs": ("character varying", 32, None, None, False, "epsg:4258"),
        "created_at": (
            "timestamp with time zone",
            None,
            None,
            None,
            False,
            "now",
        ),
        "updated_at": (
            "timestamp with time zone",
            None,
            None,
            None,
            False,
            "now",
        ),
    },
}

_EXPECTED_CONSTRAINTS: dict[str, dict[str, str]] = {
    "municipalities": {
        "ck_municipalities_directory_provenance_complete": (
            "CHECK (num_nonnulls(ine_check_digit, directory_reference_date, "
            "directory_source_url, directory_source_sha256) = ANY (ARRAY[0, 4]))"
        ),
        "ck_municipalities_ine_check_digit": (
            "CHECK (ine_check_digit IS NULL OR "
            "ine_check_digit::text ~ '^[0-9]$'::text)"
        ),
        "ck_municipalities_directory_has_ine_code": (
            "CHECK (directory_reference_date IS NULL OR "
            "ine_code::text ~ '^[0-9]{5}$'::text)"
        ),
        "ck_municipalities_directory_source_url": (
            "CHECK (directory_source_url IS NULL OR "
            "directory_source_url ~~ 'https://%'::text)"
        ),
        "ck_municipalities_directory_source_sha256": (
            "CHECK (directory_source_sha256 IS NULL OR "
            "directory_source_sha256::text ~ '^[0-9a-f]{64}$'::text)"
        ),
    },
    "reference_dataset_versions": {
        "reference_dataset_versions_pkey": "PRIMARY KEY (id)",
        "uq_ref_dataset_key_content_sha": (
            "UNIQUE (dataset_key, content_sha256)"
        ),
        "ck_ref_datasets_key_nonempty": (
            "CHECK (btrim(dataset_key::text) <> ''::text)"
        ),
        "ck_ref_datasets_labels_nonempty": (
            "CHECK (btrim(title::text) <> ''::text AND "
            "btrim(version_label::text) <> ''::text)"
        ),
        "ck_ref_datasets_source_urls": (
            "CHECK (catalog_url ~~ 'https://%'::text AND "
            "download_url ~~ 'https://%'::text)"
        ),
        "ck_ref_datasets_license_url": (
            "CHECK (license_url ~~ 'https://%'::text)"
        ),
        "ck_ref_datasets_sha256": (
            "CHECK (archive_sha256::text ~ '^[0-9a-f]{64}$'::text AND "
            "content_sha256::text ~ '^[0-9a-f]{64}$'::text)"
        ),
        "ck_ref_datasets_row_counts": (
            "CHECK (national_row_count > 0 AND target_row_count > 0 AND "
            "target_row_count <= national_row_count)"
        ),
    },
    "municipality_geography_snapshots": {
        "municipality_geography_snapshots_pkey": "PRIMARY KEY (id)",
        "municipality_geography_snapshots_dataset_version_id_fkey": (
            "FOREIGN KEY (dataset_version_id) REFERENCES "
            "reference_dataset_versions(id) ON DELETE RESTRICT"
        ),
        "municipality_geography_snapshots_municipality_id_fkey": (
            "FOREIGN KEY (municipality_id) REFERENCES municipalities(id) "
            "ON DELETE CASCADE"
        ),
        "uq_muni_geo_municipality_dataset": (
            "UNIQUE (municipality_id, dataset_version_id)"
        ),
        "uq_muni_geo_dataset_source_code": (
            "UNIQUE (dataset_version_id, source_municipality_code)"
        ),
        "uq_muni_geo_dataset_relationship": (
            "UNIQUE (dataset_version_id, relationship_id)"
        ),
        "uq_muni_geo_dataset_geographic_code": (
            "UNIQUE (dataset_version_id, geographic_code)"
        ),
        "uq_muni_geo_dataset_capital_code": (
            "UNIQUE (dataset_version_id, capital_ine_code)"
        ),
        "ck_muni_geo_source_code": (
            "CHECK (source_municipality_code::text ~ "
            "'^[0-9]{5}000000$'::text)"
        ),
        "ck_muni_geo_province_code": (
            "CHECK (source_province_code::text ~ '^[0-9]{2}$'::text AND "
            "source_province_code::text = "
            '"left"(source_municipality_code::text, 2))'
        ),
        "ck_muni_geo_identifiers": (
            "CHECK (relationship_id > 0 AND "
            "geographic_code::text ~ '^[0-9]{5}$'::text)"
        ),
        "ck_muni_geo_populations": (
            "CHECK (source_population >= 0 AND capital_population >= 0 AND "
            "capital_population <= source_population)"
        ),
        "ck_muni_geo_measurements": (
            "CHECK (surface_km2 > 0::numeric AND perimeter_m > 0::numeric)"
        ),
        "ck_muni_geo_capital_code": (
            "CHECK (capital_ine_code::text ~ '^[0-9]{11}$'::text AND "
            '"left"(capital_ine_code::text, 5) = '
            '"left"(source_municipality_code::text, 5))'
        ),
        "ck_muni_geo_coordinates": (
            "CHECK (longitude >= '-180'::integer::numeric AND "
            "longitude <= 180::numeric AND "
            "latitude >= '-90'::integer::numeric AND latitude <= 90::numeric)"
        ),
        "ck_muni_geo_labels_nonempty": (
            "CHECK (btrim(source_province_name::text) <> ''::text AND "
            "btrim(source_municipality_name::text) <> ''::text AND "
            "btrim(capital_name::text) <> ''::text AND "
            "btrim(crs::text) <> ''::text)"
        ),
    },
}

# (unique, valid, ready, live, access_method, ordered_key_expressions,
#  predicate)
_EXPECTED_INDEXES: dict[
    str,
    dict[
        str,
        tuple[bool, bool, bool, bool, str, tuple[str, ...], str | None],
    ],
] = {
    "reference_dataset_versions": {
        "ix_ref_datasets_key_reference_date": (
            False,
            True,
            True,
            True,
            "btree",
            ("dataset_key", "reference_date"),
            None,
        ),
    },
    "municipality_geography_snapshots": {
        "ix_muni_geo_dataset_version_id": (
            False,
            True,
            True,
            True,
            "btree",
            ("dataset_version_id",),
            None,
        ),
        "uq_muni_geo_current": (
            True,
            True,
            True,
            True,
            "btree",
            ("municipality_id",),
            "is_current",
        ),
    },
}
_EXPECTED_TABLE_SECURITY = {
    "reference_dataset_versions": (False, False),
    "municipality_geography_snapshots": (False, False),
}


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _normalize_column_default(
    table_name: str,
    column_name: str,
    value: str | None,
) -> str | None:
    if value is None:
        return None
    normalized = _normalize_whitespace(value).lower()
    if column_name == "id":
        sequence_name = re.escape(f"{table_name}_id_seq")
        if re.fullmatch(
            rf"nextval\('(?:[^']+\.)?{sequence_name}'::regclass\)",
            normalized,
        ):
            return "sequence"
    if normalized == "now()":
        return "now"
    if normalized == "true":
        return "true"
    if normalized in {
        "'epsg:4258'::character varying",
        "'epsg:4258'::text",
    }:
        return "epsg:4258"
    return f"unexpected:{normalized}"


def _legacy_module() -> ModuleType:
    migration_path = Path(__file__).resolve().parents[1] / "legacy" / _LEGACY_FILENAME
    spec = importlib.util.spec_from_file_location(
        "alembic_legacy_20260716_0026_geography",
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load archived migration {migration_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The archived module intentionally remains byte-for-byte identical.  Its
    # operations must run through this migration's live Alembic proxy.
    module.op = op
    return module


def _ordinance_default(connection: Connection) -> str:
    row = connection.execute(
        sa.text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'ordinances' "
            "AND column_name = 'curation_status'"
        )
    ).first()
    if row is None:
        raise RuntimeError(
            "Cannot reconcile 20260717_0029: ordinances.curation_status "
            "does not exist"
        )
    expression = row[0]
    if expression is None:
        raise RuntimeError(
            "Cannot reconcile 20260717_0029: ordinances.curation_status "
            "has no server default"
        )
    normalized = _normalize_whitespace(str(expression)).lower()
    match = re.fullmatch(
        r"'(approved|pending_review)'::(?:character varying|text)",
        normalized,
    )
    if match is None:
        raise RuntimeError(
            "Cannot reconcile 20260717_0029: unexpected server default for "
            f"ordinances.curation_status: {expression!r}. Expected approved "
            "or pending_review."
        )
    return match.group(1)


def _column_fingerprints(
    connection: Connection,
) -> dict[str, dict[str, tuple[str, int | None, int | None, int | None, bool, str | None]]]:
    rows = connection.execute(
        sa.text(
            "SELECT table_name, column_name, data_type, "
            "character_maximum_length, numeric_precision, numeric_scale, "
            "is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name IN ('municipalities', "
            "'reference_dataset_versions', "
            "'municipality_geography_snapshots')"
        )
    ).mappings()
    fingerprints: dict[
        str,
        dict[
            str,
            tuple[str, int | None, int | None, int | None, bool, str | None],
        ],
    ] = {}
    for row in rows:
        table_name = str(row["table_name"])
        column_name = str(row["column_name"])
        fingerprints.setdefault(table_name, {})[column_name] = (
            str(row["data_type"]),
            row["character_maximum_length"],
            row["numeric_precision"],
            row["numeric_scale"],
            row["is_nullable"] == "YES",
            _normalize_column_default(
                table_name,
                column_name,
                row["column_default"],
            ),
        )
    return fingerprints


def _constraint_fingerprints(connection: Connection) -> dict[str, dict[str, str]]:
    rows = connection.execute(
        sa.text(
            "SELECT table_class.relname AS table_name, "
            "constraint_row.conname AS constraint_name, "
            "pg_get_constraintdef(constraint_row.oid, true) AS definition "
            "FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS table_class "
            "ON table_class.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS table_namespace "
            "ON table_namespace.oid = table_class.relnamespace "
            "WHERE table_namespace.nspname = current_schema() "
            "AND table_class.relname IN ('municipalities', "
            "'reference_dataset_versions', "
            "'municipality_geography_snapshots')"
        )
    ).mappings()
    fingerprints: dict[str, dict[str, str]] = {}
    for row in rows:
        fingerprints.setdefault(str(row["table_name"]), {})[
            str(row["constraint_name"])
        ] = _normalize_whitespace(str(row["definition"]))
    return fingerprints


def _index_fingerprints(
    connection: Connection,
) -> dict[
    str,
    dict[
        str,
        tuple[bool, bool, bool, bool, str, tuple[str, ...], str | None],
    ],
]:
    rows = connection.execute(
        sa.text(
            "SELECT table_class.relname AS table_name, "
            "index_class.relname AS index_name, index_row.indisunique, "
            "index_row.indisvalid, index_row.indisready, index_row.indislive, "
            "access_method.amname AS access_method, "
            "ARRAY(SELECT pg_get_indexdef(index_row.indexrelid, key_number, true) "
            "FROM generate_series(1, index_row.indnkeyatts) AS key_number "
            "ORDER BY key_number) AS key_expressions, "
            "pg_get_expr(index_row.indpred, index_row.indrelid, true) "
            "AS predicate "
            "FROM pg_index AS index_row "
            "JOIN pg_class AS table_class ON table_class.oid = index_row.indrelid "
            "JOIN pg_namespace AS table_namespace "
            "ON table_namespace.oid = table_class.relnamespace "
            "JOIN pg_class AS index_class "
            "ON index_class.oid = index_row.indexrelid "
            "JOIN pg_am AS access_method ON access_method.oid = index_class.relam "
            "WHERE table_namespace.nspname = current_schema() "
            "AND table_class.relname IN ('reference_dataset_versions', "
            "'municipality_geography_snapshots') "
            "AND NOT EXISTS (SELECT 1 FROM pg_constraint AS constraint_row "
            "WHERE constraint_row.conindid = index_row.indexrelid)"
        )
    ).mappings()
    fingerprints: dict[
        str,
        dict[
            str,
            tuple[bool, bool, bool, bool, str, tuple[str, ...], str | None],
        ],
    ] = {}
    for row in rows:
        predicate = row["predicate"]
        fingerprints.setdefault(str(row["table_name"]), {})[
            str(row["index_name"])
        ] = (
            bool(row["indisunique"]),
            bool(row["indisvalid"]),
            bool(row["indisready"]),
            bool(row["indislive"]),
            str(row["access_method"]),
            tuple(str(value) for value in row["key_expressions"]),
            _normalize_whitespace(str(predicate)) if predicate is not None else None,
        )
    return fingerprints


def _table_security_fingerprints(
    connection: Connection,
) -> dict[str, tuple[bool, bool]]:
    rows = connection.execute(
        sa.text(
            "SELECT table_class.relname AS table_name, "
            "table_class.relrowsecurity, table_class.relforcerowsecurity "
            "FROM pg_class AS table_class "
            "JOIN pg_namespace AS table_namespace "
            "ON table_namespace.oid = table_class.relnamespace "
            "WHERE table_namespace.nspname = current_schema() "
            "AND table_class.relkind = 'r' "
            "AND table_class.relname IN ('reference_dataset_versions', "
            "'municipality_geography_snapshots')"
        )
    ).mappings()
    return {
        str(row["table_name"]): (
            bool(row["relrowsecurity"]),
            bool(row["relforcerowsecurity"]),
        )
        for row in rows
    }


def _sequence_ownership_errors(
    connection: Connection,
    columns: dict[
        str,
        dict[
            str,
            tuple[str, int | None, int | None, int | None, bool, str | None],
        ],
    ],
) -> list[str]:
    errors: list[str] = []
    for table_name in sorted(_MANAGED_TABLES):
        if "id" not in columns.get(table_name, {}):
            continue
        sequence_name = f"{table_name}_id_seq"
        row = connection.execute(
            sa.text(
                "WITH sequence_names AS (SELECT "
                "pg_get_serial_sequence("
                "format('%I.%I', current_schema(), "
                "CAST(:table_name AS text)), 'id'"
                ") AS owned_sequence, "
                "format('%I.%I', current_schema(), "
                "CAST(:sequence_name AS text)) "
                "AS expected_sequence) "
                "SELECT owned_sequence, expected_sequence, "
                "to_regclass(owned_sequence)::oid AS owned_oid, "
                "to_regclass(expected_sequence)::oid AS expected_oid "
                "FROM sequence_names"
            ),
            {
                "table_name": table_name,
                "sequence_name": sequence_name,
            },
        ).mappings().one()
        if (
            row["owned_oid"] is None
            or row["expected_oid"] is None
            or row["owned_oid"] != row["expected_oid"]
        ):
            errors.append(
                f"{table_name}.id sequence ownership differs: expected "
                f"{row['expected_sequence']!r}, found {row['owned_sequence']!r}"
            )
    return errors


def _mapping_errors(
    label: str,
    expected: dict[str, object],
    actual: dict[str, object],
) -> list[str]:
    errors: list[str] = []
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    changed = sorted(
        key for key in expected.keys() & actual.keys() if expected[key] != actual[key]
    )
    if missing:
        errors.append(f"{label} missing {missing}")
    if unexpected:
        errors.append(f"{label} unexpected {unexpected}")
    for key in changed:
        errors.append(
            f"{label} {key!r} differs: expected {expected[key]!r}, "
            f"found {actual[key]!r}"
        )
    return errors


def _geography_schema_errors(connection: Connection) -> list[str]:
    columns = _column_fingerprints(connection)
    constraints = _constraint_fingerprints(connection)
    indexes = _index_fingerprints(connection)
    table_security = _table_security_fingerprints(connection)
    errors: list[str] = []

    for table_name, expected_columns in _EXPECTED_COLUMNS.items():
        actual_columns = columns.get(table_name, {})
        if table_name == "municipalities":
            actual_columns = {
                name: value
                for name, value in actual_columns.items()
                if name in expected_columns
            }
        errors.extend(
            _mapping_errors(
                f"{table_name} columns",
                expected_columns,
                actual_columns,
            )
        )

    for table_name, expected_constraints in _EXPECTED_CONSTRAINTS.items():
        actual_constraints = constraints.get(table_name, {})
        if table_name == "municipalities":
            actual_constraints = {
                name: value
                for name, value in actual_constraints.items()
                if name in expected_constraints
            }
        errors.extend(
            _mapping_errors(
                f"{table_name} constraints",
                expected_constraints,
                actual_constraints,
            )
        )

    for table_name, expected_indexes in _EXPECTED_INDEXES.items():
        errors.extend(
            _mapping_errors(
                f"{table_name} indexes",
                expected_indexes,
                indexes.get(table_name, {}),
            )
        )

    errors.extend(
        _mapping_errors(
            "managed table RLS flags",
            _EXPECTED_TABLE_SECURITY,
            table_security,
        )
    )
    errors.extend(_sequence_ownership_errors(connection, columns))

    return errors


def _geography_state(connection: Connection) -> str:
    inspector = sa.inspect(connection)
    table_names = set(inspector.get_table_names())
    municipality_columns = {
        column["name"] for column in inspector.get_columns("municipalities")
    }
    municipality_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("municipalities")
    }
    managed_columns = set(_EXPECTED_COLUMNS["municipalities"])
    managed_checks = set(_EXPECTED_CONSTRAINTS["municipalities"])
    has_any_managed_object = bool(
        table_names & _MANAGED_TABLES
        or municipality_columns & managed_columns
        or municipality_checks & managed_checks
    )
    if not has_any_managed_object:
        return "absent"

    errors = _geography_schema_errors(connection)
    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(
            "Refusing to reconcile partial or incompatible municipality "
            f"reference geography schema: {detail}"
        )
    return "compatible"


def upgrade() -> None:
    connection = op.get_bind()
    current_default = _ordinance_default(connection)
    geography_state = _geography_state(connection)
    op.execute("SET LOCAL lock_timeout = '5s'")

    if geography_state == "absent":
        legacy = _legacy_module()
        legacy.upgrade()
        errors = _geography_schema_errors(connection)
        if errors:
            raise RuntimeError(
                "Archived geography DDL did not produce the expected schema: "
                + "; ".join(errors)
            )
        # Keep the ownership marker on the implicit sequence.  Alembic's
        # metadata comparison intentionally ignores standalone sequences, so
        # this does not create a permanent ``alembic check`` diff.
        op.execute(
            "COMMENT ON SEQUENCE reference_dataset_versions_id_seq IS "
            f"'{_CREATED_SCHEMA_COMMENT}'"
        )

    if current_default == "approved":
        op.alter_column(
            "ordinances",
            "curation_status",
            existing_type=sa.String(length=30),
            existing_nullable=False,
            server_default=sa.text("'pending_review'"),
        )


def downgrade() -> None:
    connection = op.get_bind()
    geography_state = _geography_state(connection)
    if geography_state == "absent":
        return

    sequence_comment = connection.execute(
        sa.text(
            "SELECT obj_description("
            "to_regclass('reference_dataset_versions_id_seq'), 'pg_class')"
        )
    ).scalar_one_or_none()
    if sequence_comment != _CREATED_SCHEMA_COMMENT:
        raise RuntimeError(
            "Refusing to downgrade 20260717_0029: the municipality reference "
            "geography schema predates this revision and is not owned by it. "
            "Its schema and data were preserved."
        )

    # Only a schema created by this revision may be removed.  Bound the lock
    # wait so a busy application fails the migration instead of leaving an
    # operator waiting indefinitely.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE municipality_geography_snapshots, "
        "reference_dataset_versions, municipalities IN ACCESS EXCLUSIVE MODE"
    )
    errors = _geography_schema_errors(connection)
    if errors:
        raise RuntimeError(
            "Refusing to downgrade incompatible municipality reference "
            "geography schema after locking: " + "; ".join(errors)
        )

    # Count every row even if municipalities gained RLS independently.  If
    # the migration role cannot bypass a policy, PostgreSQL fails closed.
    op.execute("SET LOCAL row_security = off")
    counts = connection.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM reference_dataset_versions) "
            "AS dataset_versions, "
            "(SELECT count(*) FROM municipality_geography_snapshots) "
            "AS geography_snapshots, "
            "(SELECT count(*) FROM municipalities "
            "WHERE num_nonnulls(ine_check_digit, directory_reference_date, "
            "directory_source_url, directory_source_sha256) > 0) "
            "AS municipalities_with_directory_provenance"
        )
    ).mappings().one()
    if any(int(value) > 0 for value in counts.values()):
        raise RuntimeError(
            "Refusing to downgrade 20260717_0029: municipality reference "
            "geography contains data "
            f"(dataset_versions={counts['dataset_versions']}, "
            f"geography_snapshots={counts['geography_snapshots']}, "
            "municipalities_with_directory_provenance="
            f"{counts['municipalities_with_directory_provenance']}). "
            "Preserve or migrate those records before retrying."
        )

    legacy = _legacy_module()
    legacy.downgrade()
