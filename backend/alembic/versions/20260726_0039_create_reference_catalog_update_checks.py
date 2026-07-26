"""create durable reference catalog update checks

Revision ID: 20260726_0039
Revises: 20260726_0038
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0039"
down_revision: str | None = "20260726_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_catalog_observed_versions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("raw_catalog_json", sa.JSON(), nullable=False),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_catalog_observed_versions_provider_nonempty",
        ),
        sa.CheckConstraint(
            "source_url like 'https://%' and final_url like 'https://%'",
            name="ck_reference_catalog_observed_versions_urls_https",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$' and "
            "raw_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_catalog_observed_versions_hashes",
        ),
        sa.CheckConstraint(
            "size_bytes > 0 and size_bytes <= 2097152",
            name="ck_reference_catalog_observed_versions_size",
        ),
        sa.CheckConstraint(
            "length(source_url) <= 8192 and length(final_url) <= 8192 and "
            "octet_length(raw_catalog_json::text) <= 16777216 and "
            "octet_length(analysis_json::text) <= 1048576",
            name="ck_reference_catalog_observed_versions_bounds",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f(
                "pk_reference_catalog_observed_versions"
            ),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "content_sha256",
            name="uq_reference_catalog_observed_versions_content",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_catalog_observed_versions_provider_id",
        ),
    )
    op.create_index(
        "ix_reference_catalog_observed_versions_retrieved",
        "reference_catalog_observed_versions",
        ["provider_key", "retrieved_at", "id"],
        unique=False,
    )

    op.create_table(
        "reference_catalog_update_checks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("trigger_kind", sa.String(length=20), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("baseline_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("observed_version_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("request_etag", sa.Text(), nullable=True),
        sa.Column("request_last_modified", sa.Text(), nullable=True),
        sa.Column("http_status", sa.SmallInteger(), nullable=True),
        sa.Column(
            "not_modified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("response_final_url", sa.Text(), nullable=True),
        sa.Column("response_etag", sa.Text(), nullable=True),
        sa.Column("response_last_modified", sa.Text(), nullable=True),
        sa.Column(
            "response_size_bytes",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "response_raw_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "response_redirect_chain_json",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_retryable", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(idempotency_key) <> ''",
            name="ck_reference_catalog_update_checks_identity_nonempty",
        ),
        sa.CheckConstraint(
            "source_url like 'https://%' and "
            "(response_final_url is null or "
            "response_final_url like 'https://%')",
            name="ck_reference_catalog_update_checks_urls_https",
        ),
        sa.CheckConstraint(
            "trigger_kind in ('scheduled', 'manual')",
            name="ck_reference_catalog_update_checks_trigger_kind",
        ),
        sa.CheckConstraint(
            "status in ('unchanged', 'update_available', 'error')",
            name="ck_reference_catalog_update_checks_status",
        ),
        sa.CheckConstraint(
            "(response_raw_sha256 is null or "
            "response_raw_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_catalog_update_checks_hash",
        ),
        sa.CheckConstraint(
            "response_size_bytes >= 0 and "
            "response_size_bytes <= 2097152 and duration_ms >= 0 and "
            "next_check_at > checked_at",
            name="ck_reference_catalog_update_checks_measurements",
        ),
        sa.CheckConstraint(
            "(status in ('unchanged', 'update_available') and "
            "observed_version_id is not null and "
            "http_status is not null and http_status in (200, 304) and "
            "error_code is null and "
            "error_message is null and error_retryable is null) or "
            "(status = 'error' and observed_version_id is null and "
            "error_code is not null and error_message is not null and "
            "btrim(error_code) <> '' and btrim(error_message) <> '' and "
            "error_retryable is not null)",
            name="ck_reference_catalog_update_checks_result_shape",
        ),
        sa.CheckConstraint(
            "(not_modified and http_status = 304 and "
            "response_size_bytes = 0 and response_raw_sha256 is null) or "
            "(not not_modified and "
            "(http_status is null or http_status <> 304))",
            name="ck_reference_catalog_update_checks_not_modified",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) <= 128 and "
            "length(source_url) <= 8192 and "
            "(response_final_url is null or "
            "length(response_final_url) <= 8192) and "
            "(request_etag is null or length(request_etag) <= 4096) and "
            "(request_last_modified is null or "
            "length(request_last_modified) <= 4096) and "
            "(response_etag is null or length(response_etag) <= 4096) and "
            "(response_last_modified is null or "
            "length(response_last_modified) <= 4096) and "
            "(error_message is null or length(error_message) <= 4096) and "
            "octet_length(response_redirect_chain_json::text) <= 65536",
            name="ck_reference_catalog_update_checks_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "baseline_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_catalog_update_checks_baseline",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "observed_version_id"],
            [
                "reference_catalog_observed_versions.provider_key",
                "reference_catalog_observed_versions.id",
            ],
            name="fk_reference_catalog_update_checks_observed",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_catalog_update_checks"),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "idempotency_key",
            name="uq_reference_catalog_update_checks_idempotency",
        ),
    )
    op.create_index(
        "ix_reference_catalog_update_checks_latest",
        "reference_catalog_update_checks",
        ["provider_key", "checked_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_catalog_update_checks_status",
        "reference_catalog_update_checks",
        ["provider_key", "status", "checked_at"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION prevent_reference_catalog_observed_version_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'reference catalog observation evidence is immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_catalog_observed_versions_immutable
        BEFORE UPDATE OR DELETE ON reference_catalog_observed_versions
        FOR EACH ROW
        EXECUTE FUNCTION prevent_reference_catalog_observed_version_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_catalog_observed_versions_truncate_immutable
        BEFORE TRUNCATE ON reference_catalog_observed_versions
        FOR EACH STATEMENT
        EXECUTE FUNCTION prevent_reference_catalog_observed_version_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_reference_catalog_update_check_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'reference catalog check evidence is immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_catalog_update_checks_immutable
        BEFORE UPDATE OR DELETE ON reference_catalog_update_checks
        FOR EACH ROW
        EXECUTE FUNCTION prevent_reference_catalog_update_check_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reference_catalog_update_checks_truncate_immutable
        BEFORE TRUNCATE ON reference_catalog_update_checks
        FOR EACH STATEMENT
        EXECUTE FUNCTION prevent_reference_catalog_update_check_mutation()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE reference_catalog_update_checks, "
        "reference_catalog_observed_versions IN SHARE ROW EXCLUSIVE MODE"
    )
    evidence_count = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM reference_catalog_update_checks) + "
            "(SELECT count(*) FROM reference_catalog_observed_versions)"
        )
    ).scalar_one()
    if int(evidence_count):
        raise RuntimeError(
            "cannot downgrade: immutable reference catalog update evidence "
            f"exists (rows={evidence_count})"
        )

    op.execute(
        "DROP TRIGGER trg_reference_catalog_update_checks_truncate_immutable "
        "ON reference_catalog_update_checks"
    )
    op.execute(
        "DROP TRIGGER trg_reference_catalog_update_checks_immutable "
        "ON reference_catalog_update_checks"
    )
    op.execute(
        "DROP FUNCTION prevent_reference_catalog_update_check_mutation()"
    )
    op.execute(
        "DROP TRIGGER "
        "trg_reference_catalog_observed_versions_truncate_immutable "
        "ON reference_catalog_observed_versions"
    )
    op.execute(
        "DROP TRIGGER trg_reference_catalog_observed_versions_immutable "
        "ON reference_catalog_observed_versions"
    )
    op.execute(
        "DROP FUNCTION prevent_reference_catalog_observed_version_mutation()"
    )
    op.drop_index(
        "ix_reference_catalog_update_checks_status",
        table_name="reference_catalog_update_checks",
    )
    op.drop_index(
        "ix_reference_catalog_update_checks_latest",
        table_name="reference_catalog_update_checks",
    )
    op.drop_table("reference_catalog_update_checks")
    op.drop_index(
        "ix_reference_catalog_observed_versions_retrieved",
        table_name="reference_catalog_observed_versions",
    )
    op.drop_table("reference_catalog_observed_versions")
