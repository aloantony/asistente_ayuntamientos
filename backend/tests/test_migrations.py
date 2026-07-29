import os
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_REVISION = "20260701_0020"
HEAD_REVISION = "20260729_0022"
PROTOTYPE_TABLES = {
    "assistant_knowledge_proposals",
    "document_work_artifacts",
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
        assert PROTOTYPE_TABLES.isdisjoint(inspect(engine).get_table_names())

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
        assert_downgraded_prototype_schema(inspect(engine))

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert PROTOTYPE_TABLES.isdisjoint(inspect(engine).get_table_names())

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
