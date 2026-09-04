import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_REVISION = "20260701_0020"
HEAD_REVISION = "20260904_0045"
LEGACY_GEOGRAPHY_REVISION = "20260716_0026"
LEGACY_GEOGRAPHY_PATH = (
    BACKEND_ROOT
    / "alembic"
    / "legacy"
    / "20260716_0026_add_municipality_reference_geography.py"
)
LEGACY_GEOGRAPHY_SHA256 = (
    "78b7dd5d0ff1b5c11f0516ad9154c922ea192157fe9267760d166d030a4ac474"
)
PROTOTYPE_TABLES = {
    "assistant_knowledge_proposals",
    "document_work_artifacts",
}
POPULATION_PROVENANCE_COLUMNS = {
    "population_reference_year",
    "population_source_url",
    "population_source_sha256",
}
POPULATION_PROVENANCE_CHECKS = {
    "ck_municipalities_population_reference_year",
    "ck_municipalities_population_provenance_complete",
    "ck_municipalities_population_provenance_has_population",
    "ck_municipalities_population_source_url",
    "ck_municipalities_population_source_sha256",
}
DIRECTORY_PROVENANCE_COLUMNS = {
    "ine_check_digit",
    "directory_reference_date",
    "directory_source_url",
    "directory_source_sha256",
}
DIRECTORY_PROVENANCE_CHECKS = {
    "ck_municipalities_directory_provenance_complete",
    "ck_municipalities_ine_check_digit",
    "ck_municipalities_directory_has_ine_code",
    "ck_municipalities_directory_source_url",
    "ck_municipalities_directory_source_sha256",
}
REFERENCE_DATASET_COLUMNS = {
    "id",
    "dataset_key",
    "title",
    "version_label",
    "reference_date",
    "catalog_url",
    "download_url",
    "member_name",
    "archive_sha256",
    "content_sha256",
    "license_name",
    "license_url",
    "attribution",
    "retrieved_at",
    "national_row_count",
    "target_row_count",
    "created_at",
    "updated_at",
}
MUNICIPALITY_GEOGRAPHY_COLUMNS = {
    "id",
    "municipality_id",
    "dataset_version_id",
    "is_current",
    "source_municipality_code",
    "relationship_id",
    "geographic_code",
    "source_province_code",
    "source_province_name",
    "source_municipality_name",
    "source_population",
    "surface_km2",
    "perimeter_m",
    "capital_ine_code",
    "capital_name",
    "capital_population",
    "mtn25_sheet",
    "longitude",
    "latitude",
    "coordinate_origin",
    "altitude_m",
    "altitude_origin",
    "crs",
    "created_at",
    "updated_at",
}
_MANAGED_GEOGRAPHY_TABLES = {
    "reference_dataset_versions",
    "municipality_geography_snapshots",
}
ASSISTANT_ATTACHMENT_SCHEMA = {
    "columns": {
        "id",
        "message_id",
        "document_id",
        "position",
        "context_status",
        "context_char_count",
        "authorization_checked_at",
        "authorized_by_id",
        "authorized_organization_id",
        "authorized_project_id",
        "authorized_document_checksum_sha256",
        "authorization_scope",
        "created_at",
        "updated_at",
    },
    "indexes": {
        "ix_assistant_message_attachments_message_id",
        "ix_assistant_message_attachments_document_id",
        "ix_assistant_message_attachments_authorized_by_id",
    },
    "foreign_keys": {
        ("message_id",),
        ("document_id",),
        ("authorized_by_id",),
    },
    "checks": {
        "ck_assistant_message_attachments_position",
        "ck_assistant_message_attachments_context_status",
        "ck_assistant_message_attachments_context_char_count",
        "ck_assistant_message_attachments_authorization_scope",
    },
    "unique_constraints": {
        "uq_assistant_message_attachments_message_document",
        "uq_assistant_message_attachments_message_position",
    },
}
ORDINANCE_ANALYSIS_SCHEMA = {
    "columns": {
        "id",
        "task_id",
        "ordinance_id",
        "source_ordinance_id",
        "source_updated_at",
        "source_hash",
        "source_digest",
        "status",
        "attempts",
        "error_message",
        "result_json",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
    },
    "indexes": {
        "ix_agent_office_ordinance_analysis_items_ordinance_id",
        "ix_agent_office_ord_analysis_task_status_source",
    },
    "checks": {
        "ck_agent_office_ord_analysis_items_status",
        "ck_agent_office_ord_analysis_items_attempts",
        "ck_agent_office_ord_analysis_items_source_identity",
    },
    "unique_constraints": {
        "uq_agent_office_ord_analysis_task_ordinance",
    },
}


def assert_pgvector_extension(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ")"
            )
        ).scalar_one() is True


def assert_pgvector_082_runtime(engine: Engine) -> None:
    with engine.connect() as connection:
        extension_state = connection.execute(
            text(
                """
                SELECT
                    extension.extversion,
                    available.default_version,
                    EXISTS (
                        SELECT 1
                        FROM pg_available_extension_versions AS version
                        WHERE version.name = 'vector'
                          AND version.version = '0.8.2'
                    ) AS target_available
                FROM pg_extension AS extension
                JOIN pg_available_extensions AS available
                  ON available.name = extension.extname
                WHERE extension.extname = 'vector'
                """
            )
        ).mappings().one()
        distance = connection.execute(
            text(
                "SELECT '[1,2,3]'::vector(3) "
                "<-> '[1,2,4]'::vector(3)"
            )
        ).scalar_one()

    assert extension_state == {
        "extversion": "0.8.2",
        "default_version": "0.8.2",
        "target_available": True,
    }
    assert float(distance) == pytest.approx(1.0)


def assert_postgis_extension(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_extension WHERE extname = 'postgis'"
                ")"
            )
        ).scalar_one() is True


def assert_spatial_extensions(engine: Engine) -> None:
    assert_pgvector_extension(engine)
    assert_postgis_extension(engine)
    with engine.connect() as connection:
        vector_distance = connection.execute(
            text(
                "SELECT '[1,2,3]'::vector(3) "
                "<-> '[1,2,4]'::vector(3)"
            )
        ).scalar_one()
        transformed = connection.execute(
            text(
                "SELECT ST_SRID(geom), ST_X(geom), ST_Y(geom) "
                "FROM (SELECT ST_Transform("
                "ST_SetSRID(ST_MakePoint(400000, 4600000), 25830), "
                "4326) AS geom) AS transformed"
            )
        ).one()

    assert float(vector_distance) == pytest.approx(1.0)
    assert transformed[0] == 4326
    assert -10 <= transformed[1] <= 5
    assert 35 <= transformed[2] <= 45


def assert_reference_geography_schema(inspector: Inspector) -> None:
    municipality_columns = {
        column["name"] for column in inspector.get_columns("municipalities")
    }
    municipality_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("municipalities")
    }
    assert DIRECTORY_PROVENANCE_COLUMNS <= municipality_columns
    assert DIRECTORY_PROVENANCE_CHECKS <= municipality_checks

    assert {
        column["name"]
        for column in inspector.get_columns("reference_dataset_versions")
    } == REFERENCE_DATASET_COLUMNS
    assert {
        column["name"]
        for column in inspector.get_columns("municipality_geography_snapshots")
    } == MUNICIPALITY_GEOGRAPHY_COLUMNS
    assert {
        index["name"]
        for index in inspector.get_indexes("reference_dataset_versions")
        if not index.get("duplicates_constraint")
    } == {"ix_ref_datasets_key_reference_date"}
    assert {
        index["name"]
        for index in inspector.get_indexes("municipality_geography_snapshots")
        if not index.get("duplicates_constraint")
    } == {"ix_muni_geo_dataset_version_id", "uq_muni_geo_current"}
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "municipality_geography_snapshots"
        )
    } == {("municipality_id",), ("dataset_version_id",)}


def test_pgvector_082_migration_creates_exact_extension_when_absent(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260726_0044")
    engine = create_engine(migration_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP EXTENSION vector"))
            assert connection.execute(
                text(
                    "SELECT extversion FROM pg_extension "
                    "WHERE extname = 'vector'"
                )
            ).scalar_one_or_none() is None

        run_alembic(migration_database_url, "upgrade", "head")
        assert_pgvector_082_runtime(engine)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
    finally:
        engine.dispose()


def test_pgvector_082_migration_preserves_080_table_data_and_index(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260726_0044")
    engine = create_engine(migration_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE SCHEMA pgvector_migration_fixture "
                    "AUTHORIZATION CURRENT_USER"
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE pgvector_migration_fixture.embeddings (
                        id integer PRIMARY KEY,
                        embedding vector(3) NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO pgvector_migration_fixture.embeddings (
                        id, embedding
                    ) VALUES
                        (1, '[1,2,3]'),
                        (2, '[1,2,4]')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX embeddings_embedding_hnsw_idx
                    ON pgvector_migration_fixture.embeddings
                    USING hnsw (embedding vector_l2_ops)
                    """
                )
            )
            before = connection.execute(
                text(
                    """
                    SELECT
                        namespace.oid AS schema_oid,
                        relation.oid AS table_oid,
                        index_relation.oid AS index_oid,
                        pg_get_userbyid(namespace.nspowner) AS schema_owner,
                        pg_get_userbyid(relation.relowner) AS table_owner,
                        pg_get_indexdef(index_relation.oid) AS index_definition
                    FROM pg_namespace AS namespace
                    JOIN pg_class AS relation
                      ON relation.relnamespace = namespace.oid
                     AND relation.relname = 'embeddings'
                    JOIN pg_index AS index_state
                      ON index_state.indrelid = relation.oid
                    JOIN pg_class AS index_relation
                      ON index_relation.oid = index_state.indexrelid
                     AND index_relation.relname =
                         'embeddings_embedding_hnsw_idx'
                    WHERE namespace.nspname =
                        'pgvector_migration_fixture'
                    """
                )
            ).mappings().one()
            # The reviewed 0.8.2 image intentionally exposes only 0.8.2 as a
            # fresh install, while retaining the 0.8.0 -> 0.8.2 update
            # scripts needed by persistent databases.  Mark this disposable
            # database as the supported predecessor to exercise Alembic's
            # real ALTER EXTENSION path in the regular migration suite.  A
            # separate container drill starts from the actual 0.8.0 image.
            connection.execute(
                text(
                    "UPDATE pg_extension "
                    "SET extversion = '0.8.0' "
                    "WHERE extname = 'vector'"
                )
            )
            assert connection.execute(
                text(
                    "SELECT extversion FROM pg_extension "
                    "WHERE extname = 'vector'"
                )
            ).scalar_one() == "0.8.0"

        run_alembic(migration_database_url, "upgrade", "head")
        assert_pgvector_082_runtime(engine)

        with engine.connect() as connection:
            after = connection.execute(
                text(
                    """
                    SELECT
                        namespace.oid AS schema_oid,
                        relation.oid AS table_oid,
                        index_relation.oid AS index_oid,
                        pg_get_userbyid(namespace.nspowner) AS schema_owner,
                        pg_get_userbyid(relation.relowner) AS table_owner,
                        pg_get_indexdef(index_relation.oid) AS index_definition,
                        index_state.indisvalid,
                        index_state.indisready
                    FROM pg_namespace AS namespace
                    JOIN pg_class AS relation
                      ON relation.relnamespace = namespace.oid
                     AND relation.relname = 'embeddings'
                    JOIN pg_index AS index_state
                      ON index_state.indrelid = relation.oid
                    JOIN pg_class AS index_relation
                      ON index_relation.oid = index_state.indexrelid
                     AND index_relation.relname =
                         'embeddings_embedding_hnsw_idx'
                    WHERE namespace.nspname =
                        'pgvector_migration_fixture'
                    """
                )
            ).mappings().one()
            rows = connection.execute(
                text(
                    """
                    SELECT id, embedding::text
                    FROM pgvector_migration_fixture.embeddings
                    ORDER BY id
                    """
                )
            ).all()
            distance = connection.execute(
                text(
                    """
                    SELECT first.embedding <-> second.embedding
                    FROM pgvector_migration_fixture.embeddings AS first
                    JOIN pgvector_migration_fixture.embeddings AS second
                      ON first.id = 1 AND second.id = 2
                    """
                )
            ).scalar_one()

        assert {
            key: after[key]
            for key in (
                "schema_oid",
                "table_oid",
                "index_oid",
                "schema_owner",
                "table_owner",
                "index_definition",
            )
        } == dict(before)
        assert after["indisvalid"] is True
        assert after["indisready"] is True
        assert rows == [(1, "[1,2,3]"), (2, "[1,2,4]")]
        assert float(distance) == pytest.approx(1.0)
    finally:
        engine.dispose()


def test_pgvector_082_migration_rejects_unexpected_installed_version(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260726_0044")
    engine = create_engine(migration_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE pg_extension "
                    "SET extversion = '0.7.4' "
                    "WHERE extname = 'vector'"
                )
            )

        result = run_alembic(
            migration_database_url,
            "upgrade",
            "head",
            check=False,
        )
        assert result.returncode != 0
        assert (
            "Unsupported installed pgvector version '0.7.4'"
            in result.stderr
        )
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT extversion FROM pg_extension "
                    "WHERE extname = 'vector'"
                )
            ).scalar_one() == "0.7.4"
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260726_0044"
    finally:
        engine.dispose()


def test_pgvector_082_downgrade_retains_shared_extension(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "head")
    engine = create_engine(migration_database_url)
    try:
        assert_pgvector_082_runtime(engine)
        run_alembic(migration_database_url, "downgrade", "20260726_0044")
        assert_pgvector_082_runtime(engine)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260726_0044"

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_pgvector_082_runtime(engine)
    finally:
        engine.dispose()


def assert_assistant_attachment_schema(inspector: Inspector) -> None:
    table_name = "assistant_message_attachments"
    assert {
        column["name"] for column in inspector.get_columns(table_name)
    } == ASSISTANT_ATTACHMENT_SCHEMA["columns"]
    assert {
        index["name"]
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    } == ASSISTANT_ATTACHMENT_SCHEMA["indexes"]
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(table_name)
    } == ASSISTANT_ATTACHMENT_SCHEMA["foreign_keys"]
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(table_name)
    } == ASSISTANT_ATTACHMENT_SCHEMA["checks"]
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
    } == ASSISTANT_ATTACHMENT_SCHEMA["unique_constraints"]


def assert_ordinance_analysis_schema(inspector: Inspector) -> None:
    table_name = "agent_office_ordinance_analysis_items"
    assert table_name in inspector.get_table_names()
    assert {
        column["name"] for column in inspector.get_columns(table_name)
    } == ORDINANCE_ANALYSIS_SCHEMA["columns"]
    assert {
        index["name"]
        for index in inspector.get_indexes(table_name)
        if not index.get("duplicates_constraint")
    } == ORDINANCE_ANALYSIS_SCHEMA["indexes"]
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(table_name)
    } == ORDINANCE_ANALYSIS_SCHEMA["checks"]
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table_name)
    } == ORDINANCE_ANALYSIS_SCHEMA["unique_constraints"]
    foreign_keys = {
        tuple(foreign_key["constrained_columns"]): (
            tuple(foreign_key["referred_columns"]),
            foreign_key.get("options", {}).get("ondelete"),
        )
        for foreign_key in inspector.get_foreign_keys(table_name)
    }
    assert foreign_keys == {
        ("task_id",): (("id",), "CASCADE"),
        ("ordinance_id",): (("id",), "SET NULL"),
    }


def assert_document_project_scope_is_composite(inspector: Inspector) -> None:
    project_foreign_keys = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys("documents")
        if foreign_key["referred_table"] == "projects"
    ]
    assert [
        (
            foreign_key["name"],
            tuple(foreign_key["constrained_columns"]),
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in project_foreign_keys
    ] == [
        (
            "fk_documents_project_organization",
            ("project_id", "organization_id"),
            ("id", "organization_id"),
        )
    ]
    assert "uq_projects_id_organization_id" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("projects")
    }


def assert_document_project_scope_is_simple(inspector: Inspector) -> None:
    project_foreign_keys = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys("documents")
        if foreign_key["referred_table"] == "projects"
    ]
    assert [
        tuple(foreign_key["constrained_columns"])
        for foreign_key in project_foreign_keys
    ] == [("project_id",)]
    assert "uq_projects_id_organization_id" not in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("projects")
    }


ASSET_INVENTORY_SCHEMA = {
    "municipal_asset_categories": {
        "columns": {
            "id",
            "organization_id",
            "code",
            "name",
            "description",
            "color",
            "sort_order",
            "status",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_municipal_asset_categories_org_status_sort",
            "ix_municipal_asset_categories_created_by_id",
            "ix_municipal_asset_categories_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id",),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_municipal_asset_categories_code",
            "ck_municipal_asset_categories_color",
            "ck_municipal_asset_categories_sort_order",
            "ck_municipal_asset_categories_status",
        },
        "unique_constraints": {
            "uq_municipal_asset_categories_org_code",
            "uq_municipal_asset_categories_id_org",
        },
    },
    "municipal_asset_types": {
        "columns": {
            "id",
            "organization_id",
            "category_id",
            "code",
            "name",
            "description",
            "sort_order",
            "status",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_municipal_asset_types_org_status_sort",
            "ix_municipal_asset_types_category_sort",
            "ix_municipal_asset_types_created_by_id",
            "ix_municipal_asset_types_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id",),
            ("category_id", "organization_id"),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_municipal_asset_types_code",
            "ck_municipal_asset_types_sort_order",
            "ck_municipal_asset_types_status",
        },
        "unique_constraints": {
            "uq_municipal_asset_types_org_category_code",
            "uq_municipal_asset_types_id_org",
        },
    },
    "municipal_assets": {
        "columns": {
            "id",
            "organization_id",
            "municipality_id",
            "asset_type_id",
            "location_id",
            "code",
            "name",
            "description",
            "status",
            "condition_status",
            "material",
            "dimensions",
            "installed_on",
            "last_inspected_on",
            "notes",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_municipal_assets_org_status_id",
            "ix_municipal_assets_org_type_id",
            "ix_municipal_assets_municipality_id",
            "ix_municipal_assets_asset_type_id",
            "ix_municipal_assets_location_id",
            "ix_municipal_assets_created_by_id",
            "ix_municipal_assets_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id", "municipality_id"),
            ("municipality_id",),
            ("asset_type_id", "organization_id"),
            ("location_id",),
            ("location_id", "organization_id", "municipality_id"),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_municipal_assets_code",
            "ck_municipal_assets_status",
            "ck_municipal_assets_condition_status",
        },
        "unique_constraints": {
            "uq_municipal_assets_org_code",
            "uq_municipal_assets_id_org_municipality",
        },
    },
}

MAINTENANCE_SCHEMA = {
    "maintenance_orders": {
        "columns": {
            "id",
            "organization_id",
            "municipality_id",
            "asset_id",
            "title",
            "description",
            "maintenance_type",
            "priority",
            "status",
            "scheduled_for",
            "estimated_minutes",
            "assigned_to_id",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_maintenance_orders_org_status_scheduled",
            "ix_maintenance_orders_asset_status",
            "ix_maintenance_orders_assigned_status_scheduled",
            "ix_maintenance_orders_municipality_id",
            "ix_maintenance_orders_created_by_id",
            "ix_maintenance_orders_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id", "municipality_id"),
            ("municipality_id",),
            ("asset_id", "organization_id", "municipality_id"),
            ("assigned_to_id",),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_maintenance_orders_title",
            "ck_maintenance_orders_type",
            "ck_maintenance_orders_priority",
            "ck_maintenance_orders_status",
            "ck_maintenance_orders_estimated_minutes",
            "ck_maintenance_orders_scheduled_date",
        },
        "unique_constraints": {"uq_maintenance_orders_id_org"},
    },
    "maintenance_order_events": {
        "columns": {
            "id",
            "order_id",
            "organization_id",
            "event_type",
            "from_status",
            "to_status",
            "changed_fields",
            "note",
            "actor_id",
            "created_at",
        },
        "indexes": {
            "ix_maintenance_order_events_order_id",
            "ix_maintenance_order_events_org_created",
            "ix_maintenance_order_events_actor_id",
        },
        "foreign_keys": {
            ("order_id", "organization_id"),
            ("actor_id",),
        },
        "checks": {
            "ck_maintenance_order_events_type",
            "ck_maintenance_order_events_from_status",
            "ck_maintenance_order_events_to_status",
        },
        "unique_constraints": set(),
    },
}
ASSET_SUPPORTING_UNIQUE_CONSTRAINTS = {
    "organizations": {"uq_organizations_id_municipality"},
    "geo_locations": {"uq_geo_locations_id_org_municipality"},
}

ROADMAP_SCHEMA = {
    "municipal_tasks": {
        "columns": {
            "id",
            "organization_id",
            "title",
            "description",
            "status",
            "priority",
            "due_date",
            "blocked_reason",
            "completed_at",
            "assignee_worker_id",
            "project_id",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_municipal_tasks_org_status_due",
            "ix_municipal_tasks_assignee_status",
            "ix_municipal_tasks_project_status",
            "ix_municipal_tasks_created_by_id",
            "ix_municipal_tasks_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id",),
            ("project_id", "organization_id"),
            ("assignee_worker_id", "organization_id"),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_municipal_tasks_status",
            "ck_municipal_tasks_priority",
            "ck_municipal_tasks_title",
            "ck_municipal_tasks_blocked_reason",
            "ck_municipal_tasks_completed_at",
        },
        "unique_constraints": {"uq_municipal_tasks_id_org"},
    },
    "municipal_task_events": {
        "columns": {
            "id",
            "task_id",
            "organization_id",
            "event_type",
            "from_status",
            "to_status",
            "changed_fields",
            "note",
            "actor_id",
            "created_at",
        },
        "indexes": {
            "ix_municipal_task_events_task",
            "ix_municipal_task_events_org_created",
            "ix_municipal_task_events_actor_id",
        },
        "foreign_keys": {
            ("task_id", "organization_id"),
            ("actor_id",),
        },
        "checks": {
            "ck_municipal_task_events_type",
            "ck_municipal_task_events_from_status",
            "ck_municipal_task_events_to_status",
        },
        "unique_constraints": set(),
    },
}

GOVERNMENT_STAFF_SCHEMA = {
    "government_members": {
        "columns": {
            "id",
            "organization_id",
            "level",
            "full_name",
            "role_title",
            "political_group",
            "email",
            "phone",
            "biography",
            "term_start_date",
            "term_end_date",
            "sort_order",
            "status",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_government_members_org_status_sort",
            "ix_government_members_created_by_id",
            "ix_government_members_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id",),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_government_members_level",
            "ck_government_members_status",
            "ck_government_members_sort_order",
            "ck_government_members_term_range",
        },
        "unique_constraints": set(),
    },
    "staff_posts": {
        "columns": {
            "id",
            "organization_id",
            "parent_id",
            "kind",
            "label",
            "description",
            "sort_order",
            "created_at",
            "updated_at",
        },
        "indexes": {"ix_staff_posts_org_sort"},
        "foreign_keys": {
            ("organization_id",),
            ("parent_id", "organization_id"),
        },
        "checks": {
            "ck_staff_posts_kind",
            "ck_staff_posts_sort_order",
            "ck_staff_posts_parent_not_self",
        },
        "unique_constraints": {"uq_staff_posts_id_org"},
    },
    "staff_workers": {
        "columns": {
            "id",
            "organization_id",
            "post_id",
            "full_name",
            "email",
            "phone",
            "description",
            "status",
            "schedule_summary",
            "schedule_days",
            "weekly_hours",
            "contract_type",
            "contract_start_date",
            "contract_end_date",
            "vacation_days_limit",
            "personal_days_limit",
            "bills_invoices",
            "created_by_id",
            "updated_by_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_staff_workers_org_status",
            "ix_staff_workers_created_by_id",
            "ix_staff_workers_updated_by_id",
        },
        "foreign_keys": {
            ("organization_id",),
            ("post_id", "organization_id"),
            ("created_by_id",),
            ("updated_by_id",),
        },
        "checks": {
            "ck_staff_workers_status",
            "ck_staff_workers_contract_type",
            "ck_staff_workers_contract_range",
            "ck_staff_workers_vacation_limit",
            "ck_staff_workers_personal_limit",
            "ck_staff_workers_weekly_hours",
        },
        "unique_constraints": {
            "uq_staff_workers_post",
            "uq_staff_workers_id_org",
        },
    },
    "staff_absences": {
        "columns": {
            "id",
            "worker_id",
            "organization_id",
            "absence_type",
            "start_date",
            "end_date",
            "reason",
            "created_at",
            "updated_at",
        },
        "indexes": {"ix_staff_absences_worker_start"},
        "foreign_keys": {("worker_id", "organization_id")},
        "checks": {
            "ck_staff_absences_type",
            "ck_staff_absences_range",
        },
        "unique_constraints": set(),
    },
    "staff_reports": {
        "columns": {
            "id",
            "worker_id",
            "organization_id",
            "report_type",
            "report_date",
            "plan",
            "closing",
            "incident",
            "author_id",
            "created_at",
            "updated_at",
        },
        "indexes": {
            "ix_staff_reports_worker_date",
            "ix_staff_reports_author_id",
        },
        "foreign_keys": {
            ("worker_id", "organization_id"),
            ("author_id",),
        },
        "checks": {"ck_staff_reports_type"},
        "unique_constraints": set(),
    },
    "staff_invoices": {
        "columns": {
            "id",
            "worker_id",
            "organization_id",
            "issued_on",
            "concept",
            "hours",
            "amount",
            "created_at",
            "updated_at",
        },
        "indexes": {"ix_staff_invoices_worker_date"},
        "foreign_keys": {("worker_id", "organization_id")},
        "checks": {
            "ck_staff_invoices_hours",
            "ck_staff_invoices_amount",
        },
        "unique_constraints": set(),
    },
    "staff_history_events": {
        "columns": {
            "id",
            "worker_id",
            "organization_id",
            "event_type",
            "from_status",
            "to_status",
            "changed_fields",
            "note",
            "actor_id",
            "created_at",
        },
        "indexes": {
            "ix_staff_history_events_worker",
            "ix_staff_history_events_org_created",
            "ix_staff_history_events_actor_id",
        },
        "foreign_keys": {
            ("worker_id", "organization_id"),
            ("actor_id",),
        },
        "checks": {
            "ck_staff_history_events_type",
            "ck_staff_history_events_from_status",
            "ck_staff_history_events_to_status",
        },
        "unique_constraints": set(),
    },
}

KNOWLEDGE_COLUMNS = {
    "id",
    "organization_id",
    "title",
    "summary",
    "content",
    "source_url",
    "source_title",
    "source_type",
    "confidence",
    "status",
    "sensitivity",
    "requires_legal_review",
    "source_conversation_id",
    "source_message_id",
    "proposed_by_id",
    "reviewed_by_id",
    "review_notes",
    "reviewed_at",
    "created_at",
    "updated_at",
}
KNOWLEDGE_INDEXES = {
    f"ix_assistant_knowledge_proposals_{column_name}"
    for column_name in (
        "organization_id",
        "source_conversation_id",
        "source_message_id",
        "proposed_by_id",
        "reviewed_by_id",
        "status",
    )
}
KNOWLEDGE_FOREIGN_KEY_COLUMNS = {
    (column_name,)
    for column_name in (
        "organization_id",
        "source_conversation_id",
        "source_message_id",
        "proposed_by_id",
        "reviewed_by_id",
    )
}
KNOWLEDGE_CHECKS = {
    "ck_assistant_knowledge_proposals_status",
    "ck_assistant_knowledge_proposals_source_type",
    "ck_assistant_knowledge_proposals_confidence",
    "ck_assistant_knowledge_proposals_sensitivity",
}

ARTIFACT_COLUMNS = {
    "id",
    "organization_id",
    "project_id",
    "artifact_type",
    "title",
    "content",
    "status",
    "source_summary",
    "source_document_ids",
    "review_notes",
    "export_format",
    "created_by_id",
    "reviewed_by_id",
    "export_requested_by_id",
    "exported_by_id",
    "source_conversation_id",
    "source_message_id",
    "reviewed_at",
    "export_requested_at",
    "exported_at",
    "created_at",
    "updated_at",
}
ARTIFACT_INDEXES = {
    f"ix_document_work_artifacts_{column_name}"
    for column_name in (
        "organization_id",
        "project_id",
        "status",
        "created_by_id",
        "reviewed_by_id",
        "export_requested_by_id",
        "exported_by_id",
        "source_conversation_id",
        "source_message_id",
    )
}
ARTIFACT_FOREIGN_KEY_COLUMNS = {
    (column_name,)
    for column_name in (
        "organization_id",
        "project_id",
        "created_by_id",
        "reviewed_by_id",
        "export_requested_by_id",
        "exported_by_id",
        "source_conversation_id",
        "source_message_id",
    )
}
ARTIFACT_CHECKS = {
    "ck_document_work_artifacts_type",
    "ck_document_work_artifacts_status",
}


def alembic_environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    return environment


def alembic_command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "alembic", *arguments]


def run_alembic(
    database_url: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        alembic_command(*arguments),
        cwd=BACKEND_ROOT,
        env=alembic_environment(database_url),
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def install_legacy_geography_revision(engine: Engine) -> None:
    """Apply and stamp the geography migration that historically used 0026."""

    assert hashlib.sha256(LEGACY_GEOGRAPHY_PATH.read_bytes()).hexdigest() == (
        LEGACY_GEOGRAPHY_SHA256
    )
    spec = importlib.util.spec_from_file_location(
        "test_legacy_20260716_0026_geography",
        LEGACY_GEOGRAPHY_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": LEGACY_GEOGRAPHY_REVISION},
        )


def start_alembic(
    database_url: str,
    *arguments: str,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        alembic_command(*arguments),
        cwd=BACKEND_ROOT,
        env=alembic_environment(database_url),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.fixture()
def migration_database_url() -> Generator[str, None, None]:
    configured_url = make_url(
        os.environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://app:app@127.0.0.1:5432/app",
        )
    )
    database_name = f"app_migration_test_{uuid.uuid4().hex}"
    server_url = configured_url.set(database="app")
    target_url = configured_url.set(database=database_name)
    server_engine = create_engine(
        server_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )

    with server_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        yield target_url.render_as_string(hide_password=False)
    finally:
        with server_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        server_engine.dispose()


def insert_prototype_row(connection: Connection, table_name: str) -> str:
    organization_id = connection.execute(
        text("SELECT id FROM organizations ORDER BY id LIMIT 1")
    ).scalar_one()
    row_title = f"concurrent-guard-check-{table_name}"

    if table_name == "assistant_knowledge_proposals":
        connection.execute(
            text(
                """
                INSERT INTO assistant_knowledge_proposals (
                    organization_id,
                    title,
                    summary,
                    source_url
                ) VALUES (
                    :organization_id,
                    :title,
                    'temporary migration verification row',
                    'https://example.test/migration-guard'
                )
                """
            ),
            {"organization_id": organization_id, "title": row_title},
        )
        return row_title

    project_id = connection.execute(
        text(
            """
            INSERT INTO projects (organization_id, name)
            VALUES (:organization_id, 'Migration guard project')
            RETURNING id
            """
        ),
        {"organization_id": organization_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO document_work_artifacts (
                organization_id,
                project_id,
                artifact_type,
                title,
                content,
                source_document_ids
            ) VALUES (
                :organization_id,
                :project_id,
                'note',
                :title,
                'temporary migration verification row',
                CAST('[]' AS JSON)
            )
            """
        ),
        {
            "organization_id": organization_id,
            "project_id": project_id,
            "title": row_title,
        },
    )
    return row_title


def wait_for_exclusive_lock(
    engine: Engine,
    table_name: str,
    process: subprocess.Popen[str],
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                "Migration exited before waiting for the table lock.\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )

        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks AS locks
                        JOIN pg_class AS tables
                          ON tables.oid = locks.relation
                        JOIN pg_namespace AS schemas
                          ON schemas.oid = tables.relnamespace
                        WHERE schemas.nspname = 'public'
                          AND tables.relname = :table_name
                          AND locks.mode = 'AccessExclusiveLock'
                          AND NOT locks.granted
                    )
                    """
                ),
                {"table_name": table_name},
            ).scalar_one()
        if waiting:
            return
        time.sleep(0.05)

    pytest.fail(f"Migration did not wait for an exclusive lock on {table_name}")


def assert_downgraded_prototype_schema(inspector: Inspector) -> None:
    assert {
        column["name"]
        for column in inspector.get_columns("assistant_knowledge_proposals")
    } == KNOWLEDGE_COLUMNS
    assert {
        index["name"]
        for index in inspector.get_indexes("assistant_knowledge_proposals")
    } == KNOWLEDGE_INDEXES
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "assistant_knowledge_proposals"
        )
    } == KNOWLEDGE_FOREIGN_KEY_COLUMNS
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "assistant_knowledge_proposals"
        )
    } == KNOWLEDGE_CHECKS

    assert {
        column["name"]
        for column in inspector.get_columns("document_work_artifacts")
    } == ARTIFACT_COLUMNS
    assert {
        index["name"]
        for index in inspector.get_indexes("document_work_artifacts")
    } == ARTIFACT_INDEXES
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("document_work_artifacts")
    } == ARTIFACT_FOREIGN_KEY_COLUMNS
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "document_work_artifacts"
        )
    } == ARTIFACT_CHECKS


def assert_asset_inventory_schema(inspector: Inspector) -> None:
    for table_name, expected in ASSET_INVENTORY_SCHEMA.items():
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == expected["columns"]
        assert {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        } == expected["indexes"]
        assert {
            tuple(foreign_key["constrained_columns"])
            for foreign_key in inspector.get_foreign_keys(table_name)
        } == expected["foreign_keys"]
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        } == expected["checks"]
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        } == expected["unique_constraints"]

    location_tenant_foreign_key = next(
        foreign_key
        for foreign_key in inspector.get_foreign_keys("municipal_assets")
        if foreign_key["name"] == "fk_municipal_assets_location_tenant"
    )
    assert location_tenant_foreign_key["options"].get("deferrable") is True
    assert (
        location_tenant_foreign_key["options"].get("initially") == "DEFERRED"
    )

    for table_name, expected_constraints in (
        ASSET_SUPPORTING_UNIQUE_CONSTRAINTS.items()
    ):
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        } == expected_constraints


def assert_asset_supporting_constraints_absent(inspector: Inspector) -> None:
    for table_name, constraint_names in (
        ASSET_SUPPORTING_UNIQUE_CONSTRAINTS.items()
    ):
        existing_names = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        }
        assert constraint_names.isdisjoint(existing_names)


def assert_maintenance_schema(inspector: Inspector) -> None:
    for table_name, expected in MAINTENANCE_SCHEMA.items():
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == expected["columns"]
        assert {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        } == expected["indexes"]
        assert {
            tuple(foreign_key["constrained_columns"])
            for foreign_key in inspector.get_foreign_keys(table_name)
        } == expected["foreign_keys"]
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        } == expected["checks"]
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        } == expected["unique_constraints"]


MUNICIPAL_DATA_TABLES = {
    "padron_annual_records",
    "climate_records",
    "household_stats",
    "utility_supplies",
    "water_meters",
    "water_meter_readings",
}


ADMINISTRATION_TABLES = {
    "office_hours",
    "municipal_procedures",
    "municipal_licences",
    "municipal_contracts",
    "municipal_grants",
    "transparency_items",
    "municipal_news",
    "municipal_notices",
    "municipal_notice_events",
}


BUDGET_PLENO_TABLES = {
    "municipal_budgets",
    "budget_lines",
    "budget_amendments",
    "budget_expenses",
    "treasury_movements",
    "council_sessions",
    "council_agenda_items",
}


def assert_budget_pleno_schema(inspector: Inspector) -> None:
    """El presupuesto y los plenos existen, sin columnas derivadas guardadas."""
    table_names = set(inspector.get_table_names())
    assert BUDGET_PLENO_TABLES <= table_names

    # La ejecucion se calcula al consultar: si alguien la materializa, este
    # test tiene que enterarse.
    budget_columns = {
        column["name"] for column in inspector.get_columns("municipal_budgets")
    }
    assert budget_columns.isdisjoint(
        {"executed_expense", "available_credit", "total_expense"}
    )

    # Un ano, un presupuesto; y un codigo de partida no se repite dentro de el.
    assert "uq_municipal_budgets_org_year" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("municipal_budgets")
    }
    assert "uq_budget_lines_budget_code" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("budget_lines")
    }

    # La tesoreria no cuelga del presupuesto: el dinero tiene su calendario.
    assert not any(
        foreign_key["referred_table"] == "municipal_budgets"
        for foreign_key in inspector.get_foreign_keys("treasury_movements")
    )

    # Ninguna tabla nueva apunta a `municipalities` (ADR-038).
    for table_name in BUDGET_PLENO_TABLES:
        assert not any(
            foreign_key["referred_table"] == "municipalities"
            for foreign_key in inspector.get_foreign_keys(table_name)
        )


def assert_administration_schema(inspector: Inspector) -> None:
    """Administracion y comunicacion existen y siguen aisladas por organizacion."""
    table_names = set(inspector.get_table_names())
    assert ADMINISTRATION_TABLES <= table_names

    # Las referencias de expediente se numeran por municipio, asi que la misma
    # en otro ayuntamiento es legitima.
    for table_name, constraint_name in (
        ("municipal_licences", "uq_municipal_licences_org_reference"),
        ("municipal_contracts", "uq_municipal_contracts_org_reference"),
        ("municipal_procedures", "uq_municipal_procedures_org_slug"),
        ("municipal_news", "uq_municipal_news_org_slug"),
    ):
        assert constraint_name in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        }

    # El historial de un bando se ata por (id, organization_id): retirar uno no
    # puede alcanzar al de otro ayuntamiento.
    assert ("notice_id", "organization_id") in {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("municipal_notice_events")
    }


def assert_municipal_data_schema(inspector: Inspector) -> None:
    """Las series municipales existen y siguen aisladas por organización.

    No se fija cada columna una a una como en el inventario: lo que importa aquí
    es que ninguna serie pueda cruzar de ayuntamiento, y eso lo garantiza la
    clave ajena a `organizations` que se comprueba tabla por tabla.
    """
    table_names = set(inspector.get_table_names())
    assert MUNICIPAL_DATA_TABLES <= table_names

    # Una fila por periodo y organizacion: dos del mismo ano se contradicen y
    # ninguna grafica sabria cual creer.
    expected_uniques = {
        "padron_annual_records": "uq_padron_annual_records_org_year",
        "climate_records": "uq_climate_records_org_period",
        "household_stats": "uq_household_stats_org_year",
        "water_meters": "uq_water_meters_org_code",
        "water_meter_readings": "uq_water_meter_readings_meter_date",
    }
    for table_name, constraint_name in expected_uniques.items():
        assert constraint_name in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        }

    # Toda serie cuelga de una organizacion: es lo que impide que crucen de
    # ayuntamiento aunque una consulta olvide filtrar.
    for table_name in MUNICIPAL_DATA_TABLES:
        assert any(
            "organization_id" in foreign_key["constrained_columns"]
            for foreign_key in inspector.get_foreign_keys(table_name)
        )

    # Los contadores NO apuntan directamente a `municipalities`: esa clave
    # obligaria a bloquear la tabla al soltarlos en un downgrade, y bastaria un
    # escritor abierto para que la bajada esperase en vez de fallar.
    assert not any(
        foreign_key["referred_table"] == "municipalities"
        for foreign_key in inspector.get_foreign_keys("water_meters")
    )

    # El escudo es una referencia a `documents`, no un almacen paralelo.
    assert "organization_branding" in table_names
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("organization_branding")
    } == {("organization_id",), ("crest_document_id",)}


def assert_roadmap_schema(inspector: Inspector) -> None:
    for table_name, expected in ROADMAP_SCHEMA.items():
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == expected["columns"]
        assert {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        } == expected["indexes"]
        assert {
            tuple(foreign_key["constrained_columns"])
            for foreign_key in inspector.get_foreign_keys(table_name)
        } == expected["foreign_keys"]
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        } == expected["checks"]
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        } == expected["unique_constraints"]
    # "Vencida" no es una columna: es una lectura del calendario. Si algún día
    # alguien la materializa, este test tiene que enterarse.
    assert "overdue" not in {
        column["name"] for column in inspector.get_columns("municipal_tasks")
    }


def assert_government_staff_schema(inspector: Inspector) -> None:
    for table_name, expected in GOVERNMENT_STAFF_SCHEMA.items():
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == expected["columns"]
        assert {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        } == expected["indexes"]
        assert {
            tuple(foreign_key["constrained_columns"])
            for foreign_key in inspector.get_foreign_keys(table_name)
        } == expected["foreign_keys"]
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
        } == expected["checks"]
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(table_name)
        } == expected["unique_constraints"]


def assert_maintenance_trigger(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_maintenance_order_events_immutable'
                      AND NOT tgisinternal
                )
                """
            )
        ).scalar_one() is True


def assert_maintenance_trigger_absent(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM pg_proc
                    WHERE proname = 'prevent_maintenance_order_event_mutation'
                )
                """
            )
        ).scalar_one() is True


def assert_security_events_trigger(engine: Engine) -> None:
    """La traza de seguridad solo vale si la base impide reescribirla."""
    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_trigger
                    WHERE tgname = 'trg_security_events_immutable'
                      AND NOT tgisinternal
                )
                """
            )
        ).scalar_one() is True


def assert_security_events_trigger_absent(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM pg_proc
                    WHERE proname = 'prevent_security_event_mutation'
                )
                """
            )
        ).scalar_one() is True


@pytest.mark.parametrize("table_name", sorted(PROTOTYPE_TABLES))
def test_cleanup_waits_for_and_preserves_concurrent_rows(
    migration_database_url: str,
    table_name: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", DEPLOYED_REVISION)
    engine = create_engine(migration_database_url)
    writer = engine.connect()
    transaction = writer.begin()
    migration_process: subprocess.Popen[str] | None = None

    try:
        row_title = insert_prototype_row(writer, table_name)
        migration_process = start_alembic(
            migration_database_url,
            "upgrade",
            "head",
        )
        wait_for_exclusive_lock(engine, table_name, migration_process)
        assert migration_process.poll() is None

        transaction.commit()
        stdout, stderr = migration_process.communicate(timeout=15)
        assert migration_process.returncode != 0, stdout
        assert (
            f"Refusing to remove populated prototype tables: {table_name}"
            in stderr
        )

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == DEPLOYED_REVISION
            assert connection.execute(
                text(f"SELECT COUNT(*) FROM {table_name} WHERE title = :title"),
                {"title": row_title},
            ).scalar_one() == 1
    finally:
        if migration_process is not None and migration_process.poll() is None:
            migration_process.kill()
            migration_process.communicate()
        if transaction.is_active:
            transaction.rollback()
        writer.close()
        engine.dispose()


def test_reconciles_deployed_revision_and_reversible_schema(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", DEPLOYED_REVISION)
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO assistant_admin_feedback (
                        category,
                        title,
                        description
                    ) VALUES (
                        'improvement',
                        'migration-preservation-check',
                        'temporary migration verification row'
                    )
                    """
                )
            )

        run_alembic(migration_database_url, "upgrade", "head")
        upgraded_inspector = inspect(engine)
        assert PROTOTYPE_TABLES.isdisjoint(upgraded_inspector.get_table_names())
        assert_asset_inventory_schema(upgraded_inspector)
        assert_maintenance_schema(upgraded_inspector)
        assert_government_staff_schema(upgraded_inspector)
        assert_roadmap_schema(upgraded_inspector)
        assert_municipal_data_schema(upgraded_inspector)
        assert_administration_schema(upgraded_inspector)
        assert_budget_pleno_schema(upgraded_inspector)
        assert_maintenance_trigger(engine)
        assert_spatial_extensions(engine)
        assert_reference_geography_schema(upgraded_inspector)
        assert_assistant_attachment_schema(upgraded_inspector)
        assert_ordinance_analysis_schema(upgraded_inspector)
        assert_document_project_scope_is_composite(upgraded_inspector)
        assert "sidebar_shortcut_ids" in {
            column["name"]
            for column in upgraded_inspector.get_columns("users")
        }
        assert_security_events_trigger(engine)
        assert_pgvector_extension(engine)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
            assert connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM assistant_admin_feedback
                    WHERE title = 'migration-preservation-check'
                    """
                )
            ).scalar_one() == 1

        run_alembic(migration_database_url, "downgrade", DEPLOYED_REVISION)
        downgraded_inspector = inspect(engine)
        assert_downgraded_prototype_schema(downgraded_inspector)
        assert set(ASSET_INVENTORY_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(MAINTENANCE_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(GOVERNMENT_STAFF_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(ROADMAP_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert MUNICIPAL_DATA_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert ADMINISTRATION_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert BUDGET_PLENO_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert_asset_supporting_constraints_absent(downgraded_inspector)
        assert "assistant_message_attachments" not in (
            downgraded_inspector.get_table_names()
        )
        assert "agent_office_ordinance_analysis_items" not in (
            downgraded_inspector.get_table_names()
        )
        assert_document_project_scope_is_simple(downgraded_inspector)
        assert "security_events" not in downgraded_inspector.get_table_names()
        assert_security_events_trigger_absent(engine)

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        reupgraded_inspector = inspect(engine)
        assert PROTOTYPE_TABLES.isdisjoint(
            reupgraded_inspector.get_table_names()
        )
        assert_asset_inventory_schema(reupgraded_inspector)
        assert_maintenance_schema(reupgraded_inspector)
        assert_government_staff_schema(reupgraded_inspector)
        assert_roadmap_schema(reupgraded_inspector)
        assert_municipal_data_schema(reupgraded_inspector)
        assert_administration_schema(reupgraded_inspector)
        assert_budget_pleno_schema(reupgraded_inspector)
        assert_maintenance_trigger(engine)
        assert_spatial_extensions(engine)
        assert_reference_geography_schema(reupgraded_inspector)
        assert_assistant_attachment_schema(reupgraded_inspector)
        assert_ordinance_analysis_schema(reupgraded_inspector)
        assert_document_project_scope_is_composite(reupgraded_inspector)
        assert "sidebar_shortcut_ids" in {
            column["name"]
            for column in reupgraded_inspector.get_columns("users")
        }
        assert_security_events_trigger(engine)
        assert_pgvector_extension(engine)

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
            assert connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM assistant_admin_feedback
                    WHERE title = 'migration-preservation-check'
                    """
                )
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_ordinance_review_default_changes_without_reclassifying_existing_rows(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0025")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name,
                        province,
                        autonomous_community
                    ) VALUES (
                        'Municipio migración de ordenanzas',
                        'Burgos',
                        'Castilla y León'
                    ) RETURNING id
                    """
                )
            ).scalar_one()
            previous_status = connection.execute(
                text(
                    """
                    INSERT INTO ordinances (
                        municipality_id,
                        title,
                        topic,
                        ordinance_type
                    ) VALUES (
                        :municipality_id,
                        'Ordenanza anterior a revisión segura',
                        'migración',
                        'ordinance'
                    ) RETURNING curation_status
                    """
                ),
                {"municipality_id": municipality_id},
            ).scalar_one()
            assert previous_status == "approved"

        run_alembic(migration_database_url, "upgrade", "head")
        with engine.begin() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT curation_status
                    FROM ordinances
                    WHERE title = 'Ordenanza anterior a revisión segura'
                    """
                )
            ).scalar_one() == "approved"
            new_status = connection.execute(
                text(
                    """
                    INSERT INTO ordinances (
                        municipality_id,
                        title,
                        topic,
                        ordinance_type
                    ) VALUES (
                        :municipality_id,
                        'Ordenanza posterior pendiente',
                        'migración',
                        'ordinance'
                    ) RETURNING curation_status
                    """
                ),
                {"municipality_id": municipality_id},
            ).scalar_one()
            assert new_status == "pending_review"

        run_alembic(migration_database_url, "downgrade", "20260716_0025")
        with engine.begin() as connection:
            downgraded_status = connection.execute(
                text(
                    """
                    INSERT INTO ordinances (
                        municipality_id,
                        title,
                        topic,
                        ordinance_type
                    ) VALUES (
                        :municipality_id,
                        'Ordenanza tras downgrade',
                        'migración',
                        'ordinance'
                    ) RETURNING curation_status
                    """
                ),
                {"municipality_id": municipality_id},
            ).scalar_one()
            assert downgraded_status == "approved"

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "legacy_head",
    ["20260716_0026", "20260716_0027", "20260716_0028"],
)
def test_reconciles_applied_legacy_geography_without_losing_data(
    migration_database_url: str,
    legacy_head: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0025")
    engine = create_engine(migration_database_url)

    try:
        install_legacy_geography_revision(engine)
        if legacy_head != LEGACY_GEOGRAPHY_REVISION:
            run_alembic(migration_database_url, "upgrade", legacy_head)

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == legacy_head
            default_before = connection.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'ordinances' "
                    "AND column_name = 'curation_status'"
                )
            ).scalar_one()
            assert default_before.startswith("'approved'")

            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name, province, autonomous_community, ine_code,
                        population, ine_check_digit,
                        directory_reference_date, directory_source_url,
                        directory_source_sha256
                    ) VALUES (
                        :name, 'Burgos', 'Castilla y León', '09001', 100,
                        '7', DATE '2026-01-01',
                        'https://www.ine.es/daco/daco42/codmun/diccionario26.xlsx',
                        :directory_sha
                    ) RETURNING id
                    """
                ),
                {
                    "name": f"Legacy geography {legacy_head}",
                    "directory_sha": "1" * 64,
                },
            ).scalar_one()
            ordinance = connection.execute(
                text(
                    """
                    INSERT INTO ordinances (
                        municipality_id, title, topic, ordinance_type
                    ) VALUES (
                        :municipality_id, :title, 'migración', 'ordinance'
                    ) RETURNING id, curation_status
                    """
                ),
                {
                    "municipality_id": municipality_id,
                    "title": f"Ordenanza histórica {legacy_head}",
                },
            ).mappings().one()
            assert ordinance["curation_status"] == "approved"
            dataset_version_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_dataset_versions (
                        dataset_key, title, version_label, reference_date,
                        catalog_url, download_url, member_name,
                        archive_sha256, content_sha256, license_name,
                        license_url, attribution, retrieved_at,
                        national_row_count, target_row_count
                    ) VALUES (
                        'legacy_geography', 'Legacy geography', :version_label,
                        DATE '2026-03-31', 'https://example.test/catalog',
                        'https://example.test/download', 'MUNICIPIOS.csv',
                        :archive_sha, :content_sha, 'CC BY 4.0',
                        'https://creativecommons.org/licenses/by/4.0/',
                        'IGN', TIMESTAMPTZ '2026-07-16 20:00:00+00',
                        8132, 2248
                    ) RETURNING id
                    """
                ),
                {
                    "version_label": f"Legacy {legacy_head}",
                    "archive_sha": "2" * 64,
                    "content_sha": "3" * 64,
                },
            ).scalar_one()
            snapshot_id = connection.execute(
                text(
                    """
                    INSERT INTO municipality_geography_snapshots (
                        municipality_id, dataset_version_id,
                        source_municipality_code, relationship_id,
                        geographic_code, source_province_code,
                        source_province_name, source_municipality_name,
                        source_population, surface_km2, perimeter_m,
                        capital_ine_code, capital_name, capital_population,
                        mtn25_sheet, longitude, latitude, coordinate_origin,
                        altitude_m, altitude_origin
                    ) VALUES (
                        :municipality_id, :dataset_version_id,
                        '09001000000', 1090017, '09001', '09', 'Burgos',
                        'Abajas', 100, 35.07, 25000,
                        '09001000101', 'Abajas', 100, '0167-1',
                        -3.580000000, 42.620000000,
                        'Detección automática', 840, 'MDT'
                    ) RETURNING id
                    """
                ),
                {
                    "municipality_id": municipality_id,
                    "dataset_version_id": dataset_version_id,
                },
            ).scalar_one()

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_reference_geography_schema(inspect(engine))
        assert_assistant_attachment_schema(inspect(engine))
        assert_ordinance_analysis_schema(inspect(engine))
        assert_document_project_scope_is_composite(inspect(engine))

        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
            assert connection.execute(
                text(
                    "SELECT count(*) FROM reference_dataset_versions "
                    "WHERE id = :dataset_version_id"
                ),
                {"dataset_version_id": dataset_version_id},
            ).scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT count(*) FROM municipality_geography_snapshots "
                    "WHERE id = :snapshot_id AND municipality_id = :municipality_id"
                ),
                {
                    "snapshot_id": snapshot_id,
                    "municipality_id": municipality_id,
                },
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT curation_status FROM ordinances WHERE id = :id"),
                {"id": ordinance["id"]},
            ).scalar_one() == "approved"
            new_status = connection.execute(
                text(
                    """
                    INSERT INTO ordinances (
                        municipality_id, title, topic, ordinance_type
                    ) VALUES (
                        :municipality_id, :title, 'migración', 'ordinance'
                    ) RETURNING curation_status
                    """
                ),
                {
                    "municipality_id": municipality_id,
                    "title": f"Ordenanza reconciliada {legacy_head}",
                },
            ).scalar_one()
            assert new_status == "pending_review"

        blocked_downgrade = run_alembic(
            migration_database_url,
            "downgrade",
            "20260716_0028",
            check=False,
        )
        assert blocked_downgrade.returncode != 0
        assert "geography schema predates this revision" in (
            blocked_downgrade.stderr
        )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
            assert connection.execute(
                text(
                    "SELECT count(*) FROM municipality_geography_snapshots "
                    "WHERE id = :snapshot_id"
                ),
                {"snapshot_id": snapshot_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_legacy_geography_must_reconcile_before_downgrade_or_stamp(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0025")
    engine = create_engine(migration_database_url)

    try:
        install_legacy_geography_revision(engine)

        for arguments in (
            ("downgrade", "20260716_0025"),
            ("stamp", "head"),
        ):
            result = run_alembic(
                migration_database_url,
                *arguments,
                check=False,
            )
            assert result.returncode != 0
            assert "first mutating operation must be" in result.stderr
            with engine.connect() as connection:
                assert connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == LEGACY_GEOGRAPHY_REVISION
            assert_reference_geography_schema(inspect(engine))

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('20260716_0025')"
                )
            )
        multiple_result = run_alembic(
            migration_database_url,
            "stamp",
            "head",
            check=False,
        )
        assert multiple_result.returncode != 0
        assert "missing, empty, or contains multiple revisions" in (
            multiple_result.stderr
        )

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
        empty_result = run_alembic(
            migration_database_url,
            "upgrade",
            "head",
            check=False,
        )
        assert empty_result.returncode != 0
        assert "missing, empty, or contains multiple revisions" in (
            empty_result.stderr
        )
        assert_reference_geography_schema(inspect(engine))
    finally:
        engine.dispose()


def test_legacy_geography_downgrade_rejects_without_waiting_for_writer(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0025")
    engine = create_engine(migration_database_url)
    writer: Connection | None = None
    transaction = None
    migration_process: subprocess.Popen[str] | None = None

    try:
        install_legacy_geography_revision(engine)
        run_alembic(migration_database_url, "upgrade", "head")
        with engine.begin() as connection:
            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name, province, autonomous_community
                    ) VALUES (
                        'Legacy lock check', 'Burgos', 'Castilla y León'
                    ) RETURNING id
                    """
                )
            ).scalar_one()

        writer = engine.connect()
        transaction = writer.begin()
        writer.execute(
            text(
                "UPDATE municipalities SET name = name "
                "WHERE id = :municipality_id"
            ),
            {"municipality_id": municipality_id},
        )

        migration_process = start_alembic(
            migration_database_url,
            "downgrade",
            "20260716_0028",
        )
        stdout, stderr = migration_process.communicate(timeout=15)

        assert migration_process.returncode != 0, stdout
        assert "geography schema predates this revision" in stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
    finally:
        if migration_process is not None and migration_process.poll() is None:
            migration_process.kill()
            migration_process.communicate()
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        if writer is not None:
            writer.close()
        engine.dispose()


def test_reconciliation_rejects_partial_geography_schema(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0028")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE municipalities "
                    "ADD COLUMN ine_check_digit varchar(1)"
                )
            )

        result = run_alembic(
            migration_database_url,
            "upgrade",
            "head",
            check=False,
        )

        assert result.returncode != 0
        assert "partial or incompatible municipality reference geography" in (
            result.stderr
        )
        inspector = inspect(engine)
        assert _MANAGED_GEOGRAPHY_TABLES.isdisjoint(inspector.get_table_names())
        assert "ine_check_digit" in {
            column["name"] for column in inspector.get_columns("municipalities")
        }
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0028"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (
            "ALTER SEQUENCE reference_dataset_versions_id_seq OWNED BY NONE",
            "sequence ownership differs",
        ),
        (
            "ALTER TABLE reference_dataset_versions "
            "ENABLE ROW LEVEL SECURITY",
            "managed table RLS flags",
        ),
        (
            "ALTER TABLE reference_dataset_versions ALTER COLUMN id "
            "SET DEFAULT (nextval("
            "'reference_dataset_versions_id_seq'::regclass) + 1)",
            "reference_dataset_versions columns 'id' differs",
        ),
    ],
)
def test_reconciliation_rejects_tampered_legacy_geography_fingerprint(
    migration_database_url: str,
    mutation: str,
    expected_error: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0025")
    engine = create_engine(migration_database_url)

    try:
        install_legacy_geography_revision(engine)
        run_alembic(migration_database_url, "upgrade", "20260716_0028")
        with engine.begin() as connection:
            dataset_version_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_dataset_versions (
                        dataset_key, title, version_label, reference_date,
                        catalog_url, download_url, member_name,
                        archive_sha256, content_sha256, license_name,
                        license_url, attribution, retrieved_at,
                        national_row_count, target_row_count
                    ) VALUES (
                        'tampered_legacy', 'Tampered legacy', '2026',
                        DATE '2026-03-31', 'https://example.test/catalog',
                        'https://example.test/download', 'MUNICIPIOS.csv',
                        :archive_sha, :content_sha, 'CC BY 4.0',
                        'https://creativecommons.org/licenses/by/4.0/',
                        'IGN', TIMESTAMPTZ '2026-07-16 20:00:00+00',
                        8132, 2248
                    ) RETURNING id
                    """
                ),
                {
                    "archive_sha": "4" * 64,
                    "content_sha": "5" * 64,
                },
            ).scalar_one()
            connection.execute(text(mutation))

        result = run_alembic(
            migration_database_url,
            "upgrade",
            "head",
            check=False,
        )

        assert result.returncode != 0
        assert expected_error in result.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0028"
            assert connection.execute(
                text(
                    "SELECT count(*) FROM reference_dataset_versions "
                    "WHERE id = :dataset_version_id"
                ),
                {"dataset_version_id": dataset_version_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_reconciliation_rejects_unknown_ordinance_default_before_ddl(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0028")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE ordinances ALTER COLUMN curation_status "
                    "SET DEFAULT 'manual_review'"
                )
            )

        result = run_alembic(
            migration_database_url,
            "upgrade",
            "head",
            check=False,
        )

        assert result.returncode != 0
        assert "unexpected server default for ordinances.curation_status" in (
            result.stderr
        )
        assert _MANAGED_GEOGRAPHY_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0028"
    finally:
        engine.dispose()


def test_reconciliation_upgrade_has_bounded_schema_lock_wait(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0028")
    engine = create_engine(migration_database_url)
    writer = engine.connect()
    transaction = writer.begin()
    migration_process: subprocess.Popen[str] | None = None

    try:
        municipality_id = writer.execute(
            text(
                """
                INSERT INTO municipalities (
                    name, province, autonomous_community
                ) VALUES (
                    'Upgrade lock check', 'Burgos', 'Castilla y León'
                ) RETURNING id
                """
            )
        ).scalar_one()
        writer.execute(
            text(
                "UPDATE municipalities SET name = name "
                "WHERE id = :municipality_id"
            ),
            {"municipality_id": municipality_id},
        )

        migration_process = start_alembic(
            migration_database_url,
            "upgrade",
            "head",
        )
        # The migration's PostgreSQL lock timeout remains five seconds. Give
        # the Alembic subprocess enough startup margin when the full suite is
        # exercising PostGIS concurrently, while still bounding a missing
        # lock-timeout regression.
        stdout, stderr = migration_process.communicate(timeout=30)

        assert migration_process.returncode != 0, stdout
        assert "lock timeout" in stderr.lower()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0028"
        inspector = inspect(engine)
        assert _MANAGED_GEOGRAPHY_TABLES.isdisjoint(inspector.get_table_names())
        assert DIRECTORY_PROVENANCE_COLUMNS.isdisjoint(
            {
                column["name"]
                for column in inspector.get_columns("municipalities")
            }
        )
    finally:
        if migration_process is not None and migration_process.poll() is None:
            migration_process.kill()
            migration_process.communicate()
        if transaction.is_active:
            transaction.rollback()
        writer.close()
        engine.dispose()


def test_postgis_upgrade_from_reconciled_head_is_non_destructive(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0029")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name, province, autonomous_community, ine_code
                    ) VALUES (
                        'PostGIS migration check', 'Burgos',
                        'Castilla y León', '09998'
                    ) RETURNING id
                    """
                )
            ).scalar_one()

        assert_pgvector_extension(engine)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT NOT EXISTS ("
                    "SELECT 1 FROM pg_extension WHERE extname = 'postgis'"
                    ")"
                )
            ).scalar_one() is True

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_spatial_extensions(engine)

        run_alembic(migration_database_url, "downgrade", "20260717_0029")
        assert_spatial_extensions(engine)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260717_0029"
            assert connection.execute(
                text(
                    "SELECT count(*) FROM municipalities "
                    "WHERE id = :municipality_id"
                ),
                {"municipality_id": municipality_id},
            ).scalar_one() == 1

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_spatial_extensions(engine)
    finally:
        engine.dispose()


def test_fresh_upgrade_and_asset_inventory_downgrade(
    migration_database_url: str,
) -> None:
    engine = create_engine(migration_database_url)

    try:
        run_alembic(migration_database_url, "upgrade", "head")
        assert_asset_inventory_schema(inspect(engine))
        assert_maintenance_schema(inspect(engine))
        assert_government_staff_schema(inspect(engine))
        assert_roadmap_schema(inspect(engine))
        assert_municipal_data_schema(inspect(engine))
        assert_administration_schema(inspect(engine))
        assert_budget_pleno_schema(inspect(engine))
        assert_maintenance_trigger(engine)
        assert_spatial_extensions(engine)
        assert_reference_geography_schema(inspect(engine))
        assert_assistant_attachment_schema(inspect(engine))
        assert_ordinance_analysis_schema(inspect(engine))
        assert_document_project_scope_is_composite(inspect(engine))
        assert_security_events_trigger(engine)
        assert_pgvector_extension(engine)

        run_alembic(migration_database_url, "downgrade", "20260713_0021")
        downgraded_inspector = inspect(engine)
        assert set(ASSET_INVENTORY_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(MAINTENANCE_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(GOVERNMENT_STAFF_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(ROADMAP_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert MUNICIPAL_DATA_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert ADMINISTRATION_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert BUDGET_PLENO_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert_asset_supporting_constraints_absent(downgraded_inspector)
        assert "assistant_message_attachments" not in (
            downgraded_inspector.get_table_names()
        )
        assert "agent_office_ordinance_analysis_items" not in (
            downgraded_inspector.get_table_names()
        )
        assert_document_project_scope_is_simple(downgraded_inspector)
        assert "security_events" not in downgraded_inspector.get_table_names()
        assert_security_events_trigger_absent(engine)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260713_0021"

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_asset_inventory_schema(inspect(engine))
        assert_maintenance_schema(inspect(engine))
        assert_government_staff_schema(inspect(engine))
        assert_roadmap_schema(inspect(engine))
        assert_municipal_data_schema(inspect(engine))
        assert_administration_schema(inspect(engine))
        assert_budget_pleno_schema(inspect(engine))
        assert_maintenance_trigger(engine)
        assert_spatial_extensions(engine)
        assert_reference_geography_schema(inspect(engine))
        assert_assistant_attachment_schema(inspect(engine))
        assert_ordinance_analysis_schema(inspect(engine))
        assert_document_project_scope_is_composite(inspect(engine))
        assert_security_events_trigger(engine)
        assert_pgvector_extension(engine)
    finally:
        engine.dispose()


def test_document_project_scope_upgrade_from_0026_and_downgrade(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0026")
    engine = create_engine(migration_database_url)

    try:
        assert_document_project_scope_is_simple(inspect(engine))
        run_alembic(migration_database_url, "upgrade", "20260716_0027")
        assert_document_project_scope_is_composite(inspect(engine))

        with pytest.raises(DBAPIError), engine.begin() as connection:
            first_organization_id = connection.execute(
                text("SELECT id FROM organizations ORDER BY id LIMIT 1")
            ).scalar_one()
            second_organization_id = connection.execute(
                text(
                    "INSERT INTO organizations (name) "
                    "VALUES ('document-scope-second-org') RETURNING id"
                )
            ).scalar_one()
            project_id = connection.execute(
                text(
                    "INSERT INTO projects (name, organization_id) "
                    "VALUES ('document-scope-project', :organization_id) "
                    "RETURNING id"
                ),
                {"organization_id": first_organization_id},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO documents ("
                    "organization_id, project_id, original_filename, "
                    "stored_filename, storage_key, content_type, size_bytes, "
                    "checksum_sha256"
                    ") VALUES ("
                    ":organization_id, :project_id, 'bad.txt', 'bad.txt', "
                    "'bad-scope-key', 'text/plain', 1, :checksum"
                    ")"
                ),
                {
                    "organization_id": second_organization_id,
                    "project_id": project_id,
                    "checksum": "0" * 64,
                },
            )

        run_alembic(migration_database_url, "downgrade", "20260716_0026")
        assert_document_project_scope_is_simple(inspect(engine))
    finally:
        engine.dispose()


def test_document_project_scope_preflight_rejects_inconsistent_existing_row(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0026")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            first_organization_id = connection.execute(
                text("SELECT id FROM organizations ORDER BY id LIMIT 1")
            ).scalar_one()
            second_organization_id = connection.execute(
                text(
                    "INSERT INTO organizations (name) "
                    "VALUES ('preflight-second-org') RETURNING id"
                )
            ).scalar_one()
            project_id = connection.execute(
                text(
                    "INSERT INTO projects (name, organization_id) "
                    "VALUES ('preflight-project', :organization_id) RETURNING id"
                ),
                {"organization_id": first_organization_id},
            ).scalar_one()
            document_id = connection.execute(
                text(
                    "INSERT INTO documents ("
                    "organization_id, project_id, original_filename, "
                    "stored_filename, storage_key, content_type, size_bytes, "
                    "checksum_sha256"
                    ") VALUES ("
                    ":organization_id, :project_id, 'bad.txt', 'bad.txt', "
                    "'preflight-bad-scope-key', 'text/plain', 1, :checksum"
                    ") RETURNING id"
                ),
                {
                    "organization_id": second_organization_id,
                    "project_id": project_id,
                    "checksum": "0" * 64,
                },
            ).scalar_one()

        result = run_alembic(
            migration_database_url,
            "upgrade",
            "20260716_0027",
            check=False,
        )

        assert result.returncode != 0
        assert (
            "Cannot enforce document/project tenant integrity: document "
            f"{document_id}"
        ) in result.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0026"
        assert_document_project_scope_is_simple(inspect(engine))
    finally:
        engine.dispose()


def test_attachment_audit_upgrade_preserves_legacy_rows_and_blocks_downgrade(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0027")
    engine = create_engine(migration_database_url)

    try:
        checksum = "a" * 64
        with engine.begin() as connection:
            user_id = connection.execute(
                text(
                    "INSERT INTO users (email, hashed_password, full_name) "
                    "VALUES ('attachment-audit@example.test', 'hash', "
                    "'Attachment audit') RETURNING id"
                )
            ).scalar_one()
            organization_id = connection.execute(
                text(
                    "INSERT INTO organizations (name) "
                    "VALUES ('Attachment audit organization') RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO organization_users (organization_id, user_id) "
                    "VALUES (:organization_id, :user_id)"
                ),
                {"organization_id": organization_id, "user_id": user_id},
            )
            project_id = connection.execute(
                text(
                    "INSERT INTO projects (name, organization_id) "
                    "VALUES ('Attachment audit project', :organization_id) "
                    "RETURNING id"
                ),
                {"organization_id": organization_id},
            ).scalar_one()
            document_id = connection.execute(
                text(
                    "INSERT INTO documents ("
                    "organization_id, project_id, original_filename, "
                    "stored_filename, storage_key, content_type, size_bytes, "
                    "checksum_sha256, uploaded_by_id"
                    ") VALUES ("
                    ":organization_id, :project_id, 'audit.txt', 'audit.txt', "
                    ":storage_key, 'text/plain', 6, :checksum, :user_id"
                    ") RETURNING id"
                ),
                {
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "storage_key": (
                        f"organizations/{organization_id}/projects/"
                        f"{project_id}/audit.txt"
                    ),
                    "checksum": checksum,
                    "user_id": user_id,
                },
            ).scalar_one()
            conversation_id = connection.execute(
                text(
                    "INSERT INTO assistant_conversations (title, created_by_id) "
                    "VALUES ('Attachment audit', :user_id) RETURNING id"
                ),
                {"user_id": user_id},
            ).scalar_one()
            message_id = connection.execute(
                text(
                    "INSERT INTO assistant_messages ("
                    "conversation_id, role, content"
                    ") VALUES (:conversation_id, 'user', 'audit') RETURNING id"
                ),
                {"conversation_id": conversation_id},
            ).scalar_one()
            relation_id = connection.execute(
                text(
                    "INSERT INTO assistant_message_attachments ("
                    "message_id, document_id, position, context_status, "
                    "context_char_count"
                    ") VALUES ("
                    ":message_id, :document_id, 0, 'ready', 6"
                    ") RETURNING id"
                ),
                {"message_id": message_id, "document_id": document_id},
            ).scalar_one()

        run_alembic(migration_database_url, "upgrade", "20260716_0028")
        with engine.connect() as connection:
            audit = connection.execute(
                text(
                    "SELECT authorized_by_id, authorized_organization_id, "
                    "authorized_project_id, "
                    "authorized_document_checksum_sha256, authorization_scope, "
                    "authorization_checked_at IS NOT NULL AS checked "
                    "FROM assistant_message_attachments WHERE id = :id"
                ),
                {"id": relation_id},
            ).mappings().one()
        assert dict(audit) == {
            "authorized_by_id": user_id,
            "authorized_organization_id": organization_id,
            "authorized_project_id": project_id,
            "authorized_document_checksum_sha256": checksum,
            "authorization_scope": "legacy_unverified",
            "checked": True,
        }

        result = run_alembic(
            migration_database_url,
            "downgrade",
            "20260716_0027",
            check=False,
        )

        assert result.returncode != 0
        assert "contains 1 attachment audit row(s)" in result.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0028"
            assert connection.execute(
                text(
                    "SELECT count(*) FROM assistant_message_attachments "
                    "WHERE id = :id"
                ),
                {"id": relation_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_attachment_audit_downgrade_waits_for_concurrent_insert(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0028")
    engine = create_engine(migration_database_url)
    writer = engine.connect()
    transaction = writer.begin()
    migration_process: subprocess.Popen[str] | None = None

    try:
        suffix = uuid.uuid4().hex
        checksum = "b" * 64
        user_id = writer.execute(
            text(
                "INSERT INTO users (email, hashed_password, full_name) "
                "VALUES (:email, 'hash', 'Concurrent attachment audit') "
                "RETURNING id"
            ),
            {"email": f"attachment-concurrent-{suffix}@example.test"},
        ).scalar_one()
        organization_id = writer.execute(
            text(
                "INSERT INTO organizations (name) "
                "VALUES (:name) RETURNING id"
            ),
            {"name": f"Concurrent attachment organization {suffix}"},
        ).scalar_one()
        project_id = writer.execute(
            text(
                "INSERT INTO projects (name, organization_id) "
                "VALUES (:name, :organization_id) RETURNING id"
            ),
            {
                "name": f"Concurrent attachment project {suffix}",
                "organization_id": organization_id,
            },
        ).scalar_one()
        document_id = writer.execute(
            text(
                "INSERT INTO documents ("
                "organization_id, project_id, original_filename, "
                "stored_filename, storage_key, content_type, size_bytes, "
                "checksum_sha256, uploaded_by_id"
                ") VALUES ("
                ":organization_id, :project_id, 'concurrent.txt', "
                "'concurrent.txt', :storage_key, 'text/plain', 6, "
                ":checksum, :user_id"
                ") RETURNING id"
            ),
            {
                "organization_id": organization_id,
                "project_id": project_id,
                "storage_key": (
                    f"organizations/{organization_id}/projects/"
                    f"{project_id}/concurrent.txt"
                ),
                "checksum": checksum,
                "user_id": user_id,
            },
        ).scalar_one()
        conversation_id = writer.execute(
            text(
                "INSERT INTO assistant_conversations (title, created_by_id) "
                "VALUES ('Concurrent attachment audit', :user_id) "
                "RETURNING id"
            ),
            {"user_id": user_id},
        ).scalar_one()
        message_id = writer.execute(
            text(
                "INSERT INTO assistant_messages (conversation_id, role, content) "
                "VALUES (:conversation_id, 'user', 'audit') RETURNING id"
            ),
            {"conversation_id": conversation_id},
        ).scalar_one()
        relation_id = writer.execute(
            text(
                "INSERT INTO assistant_message_attachments ("
                "message_id, document_id, position, context_status, "
                "context_char_count, authorization_checked_at, "
                "authorized_by_id, authorized_organization_id, "
                "authorized_project_id, authorized_document_checksum_sha256, "
                "authorization_scope"
                ") VALUES ("
                ":message_id, :document_id, 0, 'ready', 6, now(), "
                ":user_id, :organization_id, :project_id, :checksum, "
                "'superuser'"
                ") RETURNING id"
            ),
            {
                "message_id": message_id,
                "document_id": document_id,
                "user_id": user_id,
                "organization_id": organization_id,
                "project_id": project_id,
                "checksum": checksum,
            },
        ).scalar_one()

        migration_process = start_alembic(
            migration_database_url,
            "downgrade",
            "20260716_0027",
        )
        wait_for_exclusive_lock(
            engine,
            "assistant_message_attachments",
            migration_process,
        )
        assert migration_process.poll() is None

        transaction.commit()
        stdout, stderr = migration_process.communicate(timeout=15)
        assert migration_process.returncode != 0, stdout
        assert "contains 1 attachment audit row(s)" in stderr

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260716_0028"
            assert connection.execute(
                text(
                    "SELECT count(*) FROM assistant_message_attachments "
                    "WHERE id = :id"
                ),
                {"id": relation_id},
            ).scalar_one() == 1
    finally:
        if migration_process is not None and migration_process.poll() is None:
            migration_process.kill()
            migration_process.communicate()
        if transaction.is_active:
            transaction.rollback()
        writer.close()
        engine.dispose()


def test_population_provenance_migration_is_additive_and_reversible(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260715_0023")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name, province, autonomous_community, ine_code,
                        population
                    ) VALUES (
                        'Population migration town', 'Burgos',
                        'Castilla y León', '09137', 123
                    ) RETURNING id
                    """
                )
            ).scalar_one()

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        inspector = inspect(engine)
        columns = {
            column["name"] for column in inspector.get_columns("municipalities")
        }
        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("municipalities")
        }
        assert POPULATION_PROVENANCE_COLUMNS <= columns
        assert POPULATION_PROVENANCE_CHECKS <= checks

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT population, population_reference_year,
                           population_source_url, population_source_sha256
                    FROM municipalities
                    WHERE id = :municipality_id
                    """
                ),
                {"municipality_id": municipality_id},
            ).one()
            assert row == (123, None, None, None)

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE municipalities
                        SET population_reference_year = 2025
                        WHERE id = :municipality_id
                        """
                    ),
                    {"municipality_id": municipality_id},
                )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE municipalities
                    SET population_reference_year = 2025,
                        population_source_url =
                            'https://www.ine.es/pob_xls/pobmun.zip',
                        population_source_sha256 = :source_sha256
                    WHERE id = :municipality_id
                    """
                ),
                {
                    "municipality_id": municipality_id,
                    "source_sha256": "a" * 64,
                },
            )

        run_alembic(migration_database_url, "downgrade", "20260715_0023")
        downgraded_columns = {
            column["name"]
            for column in inspect(engine).get_columns("municipalities")
        }
        assert POPULATION_PROVENANCE_COLUMNS.isdisjoint(downgraded_columns)
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT population FROM municipalities WHERE id = :municipality_id"
                ),
                {"municipality_id": municipality_id},
            ).scalar_one() == 123

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    """
                    SELECT population_reference_year
                    FROM municipalities
                    WHERE id = :municipality_id
                    """
                ),
                {"municipality_id": municipality_id},
            ).scalar_one_or_none() is None
    finally:
        engine.dispose()


def test_reference_geography_migration_from_0025_is_constrained_and_reversible(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260716_0025")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name, province, autonomous_community, ine_code,
                        population
                    ) VALUES (
                        'Reference geography town', 'Ávila',
                        'Castilla y León', '05001', 214
                    ) RETURNING id
                    """
                )
            ).scalar_one()

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        inspector = inspect(engine)
        assert_reference_geography_schema(inspector)

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE municipalities
                        SET ine_check_digit = '3'
                        WHERE id = :municipality_id
                        """
                    ),
                    {"municipality_id": municipality_id},
                )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE municipalities
                    SET ine_check_digit = '3',
                        directory_reference_date = DATE '2026-01-01',
                        directory_source_url =
                            'https://www.ine.es/daco/daco42/codmun/diccionario26.xlsx',
                        directory_source_sha256 = :source_sha256
                    WHERE id = :municipality_id
                    """
                ),
                {
                    "municipality_id": municipality_id,
                    "source_sha256": "a" * 64,
                },
            )
            dataset_version_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_dataset_versions (
                        dataset_key, title, version_label, reference_date,
                        catalog_url, download_url, member_name,
                        archive_sha256, content_sha256, license_name,
                        license_url, attribution, retrieved_at,
                        national_row_count, target_row_count
                    ) VALUES (
                        'ign_ngmep_municipalities', 'NGMEP', 'NGMEP 2026',
                        DATE '2026-03-31', 'https://example.test/catalog',
                        'https://example.test/download', 'MUNICIPIOS.csv',
                        :archive_sha256, :content_sha256, 'CC BY 4.0',
                        'https://creativecommons.org/licenses/by/4.0/',
                        'IGN', TIMESTAMPTZ '2026-07-16 20:00:00+00',
                        8132, 2248
                    ) RETURNING id
                    """
                ),
                {
                    "archive_sha256": "b" * 64,
                    "content_sha256": "c" * 64,
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO municipality_geography_snapshots (
                        municipality_id, dataset_version_id,
                        source_municipality_code, relationship_id,
                        geographic_code, source_province_code,
                        source_province_name, source_municipality_name,
                        source_population, surface_km2, perimeter_m,
                        capital_ine_code, capital_name, capital_population,
                        mtn25_sheet, longitude, latitude, coordinate_origin,
                        altitude_m, altitude_origin
                    ) VALUES (
                        :municipality_id, :dataset_version_id,
                        '05001000000', 1050013, '05003', '05', 'Ávila',
                        'Adanero', 214, 31.417781, 24382,
                        '05001000101', 'Adanero', 214, '0481-2',
                        -4.604007136, 40.943787890,
                        'Detección automática', 908, 'MDT'
                    )
                    """
                ),
                {
                    "municipality_id": municipality_id,
                    "dataset_version_id": dataset_version_id,
                },
            )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                second_dataset_id = connection.execute(
                    text(
                        """
                        INSERT INTO reference_dataset_versions (
                            dataset_key, title, version_label, reference_date,
                            catalog_url, download_url, member_name,
                            archive_sha256, content_sha256, license_name,
                            license_url, attribution, retrieved_at,
                            national_row_count, target_row_count
                        ) VALUES (
                            'ign_ngmep_municipalities', 'NGMEP', 'NGMEP 2027',
                            DATE '2027-03-31', 'https://example.test/catalog',
                            'https://example.test/download', 'MUNICIPIOS.csv',
                            :archive_sha256, :content_sha256, 'CC BY 4.0',
                            'https://creativecommons.org/licenses/by/4.0/',
                            'IGN', TIMESTAMPTZ '2027-07-16 20:00:00+00',
                            8132, 2248
                        ) RETURNING id
                        """
                    ),
                    {
                        "archive_sha256": "d" * 64,
                        "content_sha256": "e" * 64,
                    },
                ).scalar_one()
                connection.execute(
                    text(
                        """
                        INSERT INTO municipality_geography_snapshots (
                            municipality_id, dataset_version_id,
                            source_municipality_code, relationship_id,
                            geographic_code, source_province_code,
                            source_province_name, source_municipality_name,
                            source_population, surface_km2, perimeter_m,
                            capital_ine_code, capital_name, capital_population,
                            mtn25_sheet, longitude, latitude,
                            coordinate_origin, altitude_m, altitude_origin
                        ) VALUES (
                            :municipality_id, :dataset_version_id,
                            '05001000000', 1050013, '05003', '05', 'Ávila',
                            'Adanero', 214, 31.417781, 24382,
                            '05001000101', 'Adanero', 214, '0481-2',
                            -4.604007136, 40.943787890,
                            'Detección automática', 908, 'MDT'
                        )
                        """
                    ),
                    {
                        "municipality_id": municipality_id,
                        "dataset_version_id": second_dataset_id,
                    },
                )

        blocked_downgrade = run_alembic(
            migration_database_url,
            "downgrade",
            "20260716_0028",
            check=False,
        )
        assert blocked_downgrade.returncode != 0
        assert "municipality reference geography contains data" in (
            blocked_downgrade.stderr
        )
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
            assert connection.execute(
                text(
                    "SELECT count(*) FROM municipality_geography_snapshots "
                    "WHERE municipality_id = :municipality_id"
                ),
                {"municipality_id": municipality_id},
            ).scalar_one() == 1

        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM municipality_geography_snapshots "
                    "WHERE municipality_id = :municipality_id"
                ),
                {"municipality_id": municipality_id},
            )
            connection.execute(text("DELETE FROM reference_dataset_versions"))
            connection.execute(
                text(
                    "UPDATE municipalities SET ine_check_digit = NULL, "
                    "directory_reference_date = NULL, "
                    "directory_source_url = NULL, "
                    "directory_source_sha256 = NULL "
                    "WHERE id = :municipality_id"
                ),
                {"municipality_id": municipality_id},
            )

        run_alembic(migration_database_url, "downgrade", "20260716_0025")
        downgraded = inspect(engine)
        assert {
            "reference_dataset_versions",
            "municipality_geography_snapshots",
        }.isdisjoint(downgraded.get_table_names())
        assert DIRECTORY_PROVENANCE_COLUMNS.isdisjoint(
            {
                column["name"]
                for column in downgraded.get_columns("municipalities")
            }
        )
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT population FROM municipalities WHERE id = :municipality_id"
                ),
                {"municipality_id": municipality_id},
            ).scalar_one() == 214

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_reference_geography_schema(inspect(engine))
    finally:
        engine.dispose()


def test_maintenance_migration_is_reversible_and_events_are_immutable(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260715_0022")
    engine = create_engine(migration_database_url)

    try:
        inspector = inspect(engine)
        assert set(MAINTENANCE_SCHEMA).isdisjoint(inspector.get_table_names())
        assert "uq_municipal_assets_id_org_municipality" not in {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("municipal_assets")
        }

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_maintenance_schema(inspect(engine))
        assert_maintenance_trigger(engine)
        assert_security_events_trigger(engine)

        with engine.begin() as connection:
            user_id = connection.execute(
                text(
                    """
                    INSERT INTO users (
                        email, hashed_password, full_name, is_active, is_superuser
                    ) VALUES (
                        'maintenance-migration@example.test', 'hash',
                        'Maintenance Migration', true, false
                    ) RETURNING id
                    """
                )
            ).scalar_one()
            municipality_id = connection.execute(
                text(
                    """
                    INSERT INTO municipalities (
                        name, province, autonomous_community, ine_code
                    ) VALUES (
                        'Migration Town', 'Burgos', 'Castilla y Leon',
                        'maint-migration'
                    ) RETURNING id
                    """
                )
            ).scalar_one()
            organization_id = connection.execute(
                text(
                    """
                    INSERT INTO organizations (name, municipality_id, status)
                    VALUES ('Migration Council', :municipality_id, 'active')
                    RETURNING id
                    """
                ),
                {"municipality_id": municipality_id},
            ).scalar_one()
            category_id = connection.execute(
                text(
                    """
                    INSERT INTO municipal_asset_categories (
                        organization_id, code, name
                    ) VALUES (:organization_id, 'migration', 'Migration')
                    RETURNING id
                    """
                ),
                {"organization_id": organization_id},
            ).scalar_one()
            type_id = connection.execute(
                text(
                    """
                    INSERT INTO municipal_asset_types (
                        organization_id, category_id, code, name
                    ) VALUES (
                        :organization_id, :category_id, 'migration', 'Migration'
                    ) RETURNING id
                    """
                ),
                {
                    "organization_id": organization_id,
                    "category_id": category_id,
                },
            ).scalar_one()
            asset_id = connection.execute(
                text(
                    """
                    INSERT INTO municipal_assets (
                        organization_id, municipality_id, asset_type_id, name
                    ) VALUES (
                        :organization_id, :municipality_id, :type_id,
                        'Migration asset'
                    ) RETURNING id
                    """
                ),
                {
                    "organization_id": organization_id,
                    "municipality_id": municipality_id,
                    "type_id": type_id,
                },
            ).scalar_one()
            order_id = connection.execute(
                text(
                    """
                    INSERT INTO maintenance_orders (
                        organization_id, municipality_id, asset_id, title,
                        created_by_id, updated_by_id
                    ) VALUES (
                        :organization_id, :municipality_id, :asset_id,
                        'Migration order', :user_id, :user_id
                    ) RETURNING id
                    """
                ),
                {
                    "organization_id": organization_id,
                    "municipality_id": municipality_id,
                    "asset_id": asset_id,
                    "user_id": user_id,
                },
            ).scalar_one()
            event_id = connection.execute(
                text(
                    """
                    INSERT INTO maintenance_order_events (
                        order_id, organization_id, event_type, to_status,
                        changed_fields, actor_id
                    ) VALUES (
                        :order_id, :organization_id, 'created', 'planned',
                        CAST('["title"]' AS JSON), :user_id
                    ) RETURNING id
                    """
                ),
                {
                    "order_id": order_id,
                    "organization_id": organization_id,
                    "user_id": user_id,
                },
            ).scalar_one()

        with engine.begin() as connection:
            other_organization_id = connection.execute(
                text(
                    """
                    INSERT INTO organizations (name, municipality_id, status)
                    VALUES ('Other Migration Council', :municipality_id, 'active')
                    RETURNING id
                    """
                ),
                {"municipality_id": municipality_id},
            ).scalar_one()
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO maintenance_orders (
                            organization_id, municipality_id, asset_id, title,
                            created_by_id, updated_by_id
                        ) VALUES (
                            :organization_id, :municipality_id, :asset_id,
                            'Cross-tenant order', :user_id, :user_id
                        )
                        """
                    ),
                    {
                        "organization_id": other_organization_id,
                        "municipality_id": municipality_id,
                        "asset_id": asset_id,
                        "user_id": user_id,
                    },
                )

        for statement in (
            "UPDATE maintenance_order_events SET note = 'tampered' WHERE id = :id",
            "DELETE FROM maintenance_order_events WHERE id = :id",
        ):
            with pytest.raises(
                DBAPIError,
                match="maintenance order events are immutable",
            ):
                with engine.begin() as connection:
                    connection.execute(text(statement), {"id": event_id})

        run_alembic(migration_database_url, "downgrade", "20260715_0022")
        downgraded = inspect(engine)
        assert set(MAINTENANCE_SCHEMA).isdisjoint(downgraded.get_table_names())
        assert "uq_municipal_assets_id_org_municipality" not in {
            constraint["name"]
            for constraint in downgraded.get_unique_constraints("municipal_assets")
        }
        assert_maintenance_trigger_absent(engine)

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_maintenance_schema(inspect(engine))
        assert_maintenance_trigger(engine)
        assert_security_events_trigger(engine)
    finally:
        engine.dispose()


def test_fresh_upgrade_creates_nullable_user_sidebar_shortcuts(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "head")
    engine = create_engine(migration_database_url)

    try:
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("users")
        }
        sidebar_shortcuts = columns["sidebar_shortcut_ids"]
        assert sidebar_shortcuts["nullable"] is True
        assert sidebar_shortcuts["default"] is None
        assert str(sidebar_shortcuts["type"]) == "JSON"
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
    finally:
        engine.dispose()


def test_sidebar_shortcuts_upgrade_preserves_users_and_guards_downgrade(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0033")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            user_id = connection.execute(
                text(
                    "INSERT INTO users (email, hashed_password, full_name) "
                    "VALUES ('sidebar-migration@example.test', 'hash', "
                    "'Sidebar Migration') RETURNING id"
                )
            ).scalar_one()

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT sidebar_shortcut_ids FROM users WHERE id = :user_id"
                ),
                {"user_id": user_id},
            ).scalar_one() is None

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE users SET sidebar_shortcut_ids = "
                    "'[\"requirements\"]'::json WHERE id = :user_id"
                ),
                {"user_id": user_id},
            )

        blocked = run_alembic(
            migration_database_url,
            "downgrade",
            "20260717_0033",
            check=False,
        )
        assert blocked.returncode != 0
        assert "user sidebar shortcut preferences exist" in blocked.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE users SET sidebar_shortcut_ids = NULL "
                    "WHERE id = :user_id"
                ),
                {"user_id": user_id},
            )
        run_alembic(migration_database_url, "downgrade", "20260717_0033")
        downgraded_columns = {
            column["name"] for column in inspect(engine).get_columns("users")
        }
        assert "sidebar_shortcut_ids" not in downgraded_columns
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            ).scalar_one() == 1

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
    finally:
        engine.dispose()


def test_town_hall_items_move_under_an_epigraph_and_back(
    migration_database_url: str,
) -> None:
    """ADR-054: la pestaña gana un epígrafe que adopta sus apartados."""
    run_alembic(migration_database_url, "upgrade", "20260806_0042")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            organization_id = connection.execute(
                text(
                    "INSERT INTO organizations (name) "
                    "VALUES ('town-hall-epigraph-org') RETURNING id"
                )
            ).scalar_one()
            section_id = connection.execute(
                text(
                    "INSERT INTO municipal_blocks "
                    "(organization_id, block_type, title, position) "
                    "VALUES (:organization_id, 'nav_section', 'Información', 0) "
                    "RETURNING id"
                ),
                {"organization_id": organization_id},
            ).scalar_one()
            for position, title in enumerate(("Historia", "Fiestas")):
                connection.execute(
                    text(
                        "INSERT INTO municipal_blocks "
                        "(organization_id, parent_id, block_type, title, position) "
                        "VALUES (:organization_id, :parent_id, 'nav_item', "
                        ":title, :position)"
                    ),
                    {
                        "organization_id": organization_id,
                        "parent_id": section_id,
                        "title": title,
                        "position": position,
                    },
                )
            # Una pestaña sin apartados no debe recibir epígrafe.
            connection.execute(
                text(
                    "INSERT INTO municipal_blocks "
                    "(organization_id, block_type, title, position) "
                    "VALUES (:organization_id, 'nav_section', 'Vacía', 1)"
                ),
                {"organization_id": organization_id},
            )

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")

        with engine.connect() as connection:
            epigraphs = connection.execute(
                text(
                    "SELECT id, parent_id, title FROM municipal_blocks "
                    "WHERE organization_id = :organization_id "
                    "AND block_type = 'epigraph'"
                ),
                {"organization_id": organization_id},
            ).all()
            assert len(epigraphs) == 1
            epigraph_id, epigraph_parent, epigraph_title = epigraphs[0]
            assert epigraph_parent == section_id
            assert epigraph_title == "Información"

            assert connection.execute(
                text(
                    "SELECT count(*) FROM municipal_blocks "
                    "WHERE block_type = 'nav_item' AND parent_id = :parent_id"
                ),
                {"parent_id": epigraph_id},
            ).scalar_one() == 2

        run_alembic(migration_database_url, "downgrade", "20260806_0042")

        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM municipal_blocks "
                    "WHERE block_type = 'epigraph'"
                )
            ).scalar_one() == 0
            titles = connection.execute(
                text(
                    "SELECT title FROM municipal_blocks "
                    "WHERE block_type = 'nav_item' AND parent_id = :parent_id "
                    "ORDER BY position"
                ),
                {"parent_id": section_id},
            ).scalars().all()
            assert titles == ["Historia", "Fiestas"]

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
    finally:
        engine.dispose()


def test_legacy_town_hall_tabs_fold_into_one_and_unfold(
    migration_database_url: str,
) -> None:
    """ADR-059: las pestañas sobrantes del seed viejo se pliegan en «informacion».

    Se comprueban las tres cosas que importan: que los epígrafes acaban en la
    pestaña que recoge, que una pestaña hecha a mano no se toca, y que la vuelta
    atrás los devuelve a su sitio aunque se hayan renombrado por el camino.
    """
    run_alembic(migration_database_url, "upgrade", "20260901_0043")
    engine = create_engine(migration_database_url)

    def nueva_pestaña(connection, organization_id, title, position, seed):
        return connection.execute(
            text(
                "INSERT INTO municipal_blocks "
                "(organization_id, block_type, title, position, data_json) "
                "VALUES (:organization_id, 'nav_section', :title, :position, "
                ":data_json) RETURNING id"
            ),
            {
                "organization_id": organization_id,
                "title": title,
                "position": position,
                "data_json": json.dumps({"seed": seed}) if seed else None,
            },
        ).scalar_one()

    def nuevo_epigrafe(connection, organization_id, parent_id, title):
        return connection.execute(
            text(
                "INSERT INTO municipal_blocks "
                "(organization_id, parent_id, block_type, title, position) "
                "VALUES (:organization_id, :parent_id, 'epigraph', :title, 0) "
                "RETURNING id"
            ),
            {
                "organization_id": organization_id,
                "parent_id": parent_id,
                "title": title,
            },
        ).scalar_one()

    try:
        with engine.begin() as connection:
            organization_id = connection.execute(
                text(
                    "INSERT INTO organizations (name) "
                    "VALUES ('town-hall-merge-org') RETURNING id"
                )
            ).scalar_one()
            keeper = nueva_pestaña(
                connection, organization_id, "Información del municipio", 0, "informacion"
            )
            keeper_epigraph = nuevo_epigrafe(
                connection, organization_id, keeper, "Información del municipio"
            )
            datos = nueva_pestaña(
                connection, organization_id, "Datos del municipio", 1, "datos"
            )
            datos_epigraph = nuevo_epigrafe(
                connection, organization_id, datos, "Datos del municipio"
            )
            # Una pestaña que el ayuntamiento creó a mano: sin marca del seed.
            propia = nueva_pestaña(connection, organization_id, "Turismo", 2, None)
            propia_epigraph = nuevo_epigrafe(
                connection, organization_id, propia, "Playas"
            )

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")

        with engine.connect() as connection:
            activas = connection.execute(
                text(
                    "SELECT id, title FROM municipal_blocks "
                    "WHERE organization_id = :organization_id "
                    "AND block_type = 'nav_section' AND status = 'active' "
                    "ORDER BY position"
                ),
                {"organization_id": organization_id},
            ).all()
            assert [row.id for row in activas] == [keeper, propia]

            padres = dict(
                connection.execute(
                    text(
                        "SELECT id, parent_id FROM municipal_blocks "
                        "WHERE organization_id = :organization_id "
                        "AND block_type = 'epigraph'"
                    ),
                    {"organization_id": organization_id},
                ).all()
            )
            assert padres[keeper_epigraph] == keeper
            assert padres[datos_epigraph] == keeper
            # La pestaña hecha a mano conserva el suyo.
            assert padres[propia_epigraph] == propia

        # Renombrar la tarjeta no puede romper la vuelta atrás: la marca no
        # viaja en el título.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE municipal_blocks SET title = 'Padrón' WHERE id = :id"
                ),
                {"id": datos_epigraph},
            )

        run_alembic(migration_database_url, "downgrade", "20260901_0043")

        with engine.connect() as connection:
            padres = dict(
                connection.execute(
                    text(
                        "SELECT id, parent_id FROM municipal_blocks "
                        "WHERE organization_id = :organization_id "
                        "AND block_type = 'epigraph'"
                    ),
                    {"organization_id": organization_id},
                ).all()
            )
            assert padres[datos_epigraph] == datos
            assert padres[keeper_epigraph] == keeper
            estado = connection.execute(
                text("SELECT status FROM municipal_blocks WHERE id = :id"),
                {"id": datos},
            ).scalar_one()
            assert estado == "active"
            # La marca se retira al volver: no queda rastro en `data_json`.
            marca = connection.execute(
                text("SELECT data_json FROM municipal_blocks WHERE id = :id"),
                {"id": datos_epigraph},
            ).scalar_one()
            assert marca is None or "merged_from" not in marca
    finally:
        engine.dispose()
