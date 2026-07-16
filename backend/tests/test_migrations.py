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
HEAD_REVISION = "20260716_0025"
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


def assert_pgvector_extension(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ")"
            )
        ).scalar_one() is True

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

        run_alembic(migration_database_url, "downgrade", "20260713_0021")
        downgraded_inspector = inspect(engine)
        assert set(ASSET_INVENTORY_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert set(MAINTENANCE_SCHEMA).isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert_asset_supporting_constraints_absent(downgraded_inspector)
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
    finally:
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
