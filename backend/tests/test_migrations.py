import os
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_REVISION = "20260701_0020"
HEAD_REVISION = "20260716_0028"
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


def assert_pgvector_extension(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ")"
            )
        ).scalar_one() is True


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
        assert_maintenance_trigger(engine)
        assert_pgvector_extension(engine)
        assert_reference_geography_schema(upgraded_inspector)
        assert_assistant_attachment_schema(upgraded_inspector)
        assert_document_project_scope_is_composite(upgraded_inspector)

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
        assert_asset_supporting_constraints_absent(downgraded_inspector)
        assert "assistant_message_attachments" not in (
            downgraded_inspector.get_table_names()
        )
        assert_document_project_scope_is_simple(downgraded_inspector)

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        reupgraded_inspector = inspect(engine)
        assert PROTOTYPE_TABLES.isdisjoint(
            reupgraded_inspector.get_table_names()
        )
        assert_asset_inventory_schema(reupgraded_inspector)
        assert_maintenance_schema(reupgraded_inspector)
        assert_maintenance_trigger(engine)
        assert_pgvector_extension(engine)
        assert_reference_geography_schema(reupgraded_inspector)
        assert_assistant_attachment_schema(reupgraded_inspector)
        assert_document_project_scope_is_composite(reupgraded_inspector)

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


def test_fresh_upgrade_and_asset_inventory_downgrade(
    migration_database_url: str,
) -> None:
    engine = create_engine(migration_database_url)

    try:
        run_alembic(migration_database_url, "upgrade", "head")
        assert_asset_inventory_schema(inspect(engine))
        assert_maintenance_schema(inspect(engine))
        assert_maintenance_trigger(engine)
        assert_pgvector_extension(engine)
        assert_reference_geography_schema(inspect(engine))
        assert_assistant_attachment_schema(inspect(engine))
        assert_document_project_scope_is_composite(inspect(engine))

        run_alembic(migration_database_url, "downgrade", "20260713_0021")
        downgraded_inspector = inspect(engine)
        assert set(ASSET_INVENTORY_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(MAINTENANCE_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert_asset_supporting_constraints_absent(downgraded_inspector)
        assert "assistant_message_attachments" not in (
            downgraded_inspector.get_table_names()
        )
        assert_document_project_scope_is_simple(downgraded_inspector)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260713_0021"

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_asset_inventory_schema(inspect(engine))
        assert_maintenance_schema(inspect(engine))
        assert_maintenance_trigger(engine)
        assert_pgvector_extension(engine)
        assert_reference_geography_schema(inspect(engine))
        assert_assistant_attachment_schema(inspect(engine))
        assert_document_project_scope_is_composite(inspect(engine))
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
    finally:
        engine.dispose()
