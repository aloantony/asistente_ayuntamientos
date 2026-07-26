"""watch official style updates without automatic recipe changes

Revision ID: 20260726_0044
Revises: 20260726_0043
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0044"
down_revision: str | None = "20260726_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_style_observed_versions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("profile", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("semantic_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("semantic_summary_json", sa.JSON(), nullable=False),
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
            "btrim(provider_key) <> '' and btrim(profile) <> ''",
            name="ck_reference_style_observed_identity",
        ),
        sa.CheckConstraint(
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "raw_sha256 ~ '^[0-9a-f]{64}$' and "
            "semantic_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_style_observed_hashes",
        ),
        sa.CheckConstraint(
            "source_url like 'https://%' and final_url = source_url",
            name="ck_reference_style_observed_url",
        ),
        sa.CheckConstraint(
            "size_bytes between 1 and 65536",
            name="ck_reference_style_observed_size",
        ),
        sa.CheckConstraint(
            "storage_backend = 'filesystem' and "
            "storage_key ~ '^blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}$'",
            name="ck_reference_style_observed_storage",
        ),
        sa.CheckConstraint(
            "length(profile) <= 128 and length(source_url) <= 8192 and "
            "length(final_url) <= 8192 and length(storage_key) <= 256 and "
            "json_typeof(semantic_summary_json) = 'object' and "
            "octet_length(semantic_summary_json::text) <= 65536",
            name="ck_reference_style_observed_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_style_observed_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_style_observed_versions"),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "source_definition_sha256",
            "source_url",
            "raw_sha256",
            name="uq_reference_style_observed_content",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "source_definition_sha256",
            "id",
            name="uq_reference_style_observed_target",
        ),
    )
    op.create_index(
        "ix_reference_style_observed_source_retrieved",
        "reference_style_observed_versions",
        ["source_id", "retrieved_at", "id"],
        unique=False,
    )

    op.create_table(
        "reference_style_update_checks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("profile", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("trigger_kind", sa.String(length=20), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "baseline_raw_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "baseline_semantic_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("observed_version_id", sa.BigInteger(), nullable=True),
        sa.Column("authorization_review_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "authorization_review_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
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
            "btrim(provider_key) <> '' and btrim(profile) <> '' and "
            "btrim(idempotency_key) <> ''",
            name="ck_reference_style_checks_identity",
        ),
        sa.CheckConstraint(
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "baseline_raw_sha256 ~ '^[0-9a-f]{64}$' and "
            "baseline_semantic_sha256 ~ '^[0-9a-f]{64}$' and "
            "(authorization_review_sha256 is null or "
            "authorization_review_sha256 ~ '^[0-9a-f]{64}$') and "
            "(response_raw_sha256 is null or "
            "response_raw_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_style_checks_hashes",
        ),
        sa.CheckConstraint(
            "source_url like 'https://%' and "
            "(response_final_url is null or response_final_url = source_url)",
            name="ck_reference_style_checks_urls",
        ),
        sa.CheckConstraint(
            "trigger_kind in ('scheduled', 'manual') and "
            "status in ('unchanged', 'style_review_required', 'error')",
            name="ck_reference_style_checks_status",
        ),
        sa.CheckConstraint(
            "(authorization_review_id is null and "
            "authorization_review_sha256 is null) or "
            "(authorization_review_id is not null and "
            "authorization_review_sha256 is not null)",
            name="ck_reference_style_checks_authorization",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0 and next_check_at > checked_at and "
            "response_size_bytes between 0 and 65536",
            name="ck_reference_style_checks_measurements",
        ),
        sa.CheckConstraint(
            "(status in ('unchanged', 'style_review_required') and "
            "authorization_review_id is not null and "
            "http_status in (200, 304) and error_code is null and "
            "error_message is null and error_retryable is null) or "
            "(status = 'error' and observed_version_id is null and "
            "error_code is not null and btrim(error_code) <> '' and "
            "error_message is not null and btrim(error_message) <> '' and "
            "error_retryable is not null)",
            name="ck_reference_style_checks_result",
        ),
        sa.CheckConstraint(
            "(status = 'style_review_required' and "
            "observed_version_id is not null) or "
            "status <> 'style_review_required'",
            name="ck_reference_style_checks_candidate",
        ),
        sa.CheckConstraint(
            "(not_modified and http_status = 304 and "
            "response_size_bytes = 0 and response_raw_sha256 is null) or "
            "(not not_modified and "
            "(http_status is null or http_status <> 304 or "
            "status = 'error'))",
            name="ck_reference_style_checks_not_modified",
        ),
        sa.CheckConstraint(
            "(http_status = 200 and response_size_bytes > 0 and "
            "response_raw_sha256 is not null) or "
            "(http_status is null or http_status <> 200)",
            name="ck_reference_style_checks_http_200",
        ),
        sa.CheckConstraint(
            "length(profile) <= 128 and length(idempotency_key) <= 160 and "
            "length(source_url) <= 8192 and "
            "(request_etag is null or length(request_etag) <= 4096) and "
            "(request_last_modified is null or "
            "length(request_last_modified) <= 4096) and "
            "(response_etag is null or length(response_etag) <= 4096) and "
            "(response_last_modified is null or "
            "length(response_last_modified) <= 4096) and "
            "(error_message is null or length(error_message) <= 4096) and "
            "json_typeof(response_redirect_chain_json) = 'array' and "
            "octet_length(response_redirect_chain_json::text) <= 65536",
            name="ck_reference_style_checks_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_style_checks_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "source_definition_sha256",
                "observed_version_id",
            ],
            [
                "reference_style_observed_versions.provider_key",
                "reference_style_observed_versions.layer_id",
                "reference_style_observed_versions.source_id",
                "reference_style_observed_versions.source_definition_sha256",
                "reference_style_observed_versions.id",
            ],
            name="fk_reference_style_checks_observed",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "source_definition_sha256",
                "authorization_review_id",
                "authorization_review_sha256",
            ],
            [
                "reference_mirror_authorization_reviews.provider_key",
                "reference_mirror_authorization_reviews.layer_id",
                "reference_mirror_authorization_reviews.source_id",
                (
                    "reference_mirror_authorization_reviews."
                    "source_definition_sha256"
                ),
                "reference_mirror_authorization_reviews.id",
                "reference_mirror_authorization_reviews.review_sha256",
            ],
            name="fk_reference_style_checks_authorization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_style_update_checks"),
        ),
        sa.UniqueConstraint(
            "source_id",
            "source_definition_sha256",
            "idempotency_key",
            name="uq_reference_style_checks_idempotency",
        ),
    )
    op.create_index(
        "ix_reference_style_checks_latest",
        "reference_style_update_checks",
        ["source_id", "source_definition_sha256", "checked_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_style_checks_pending",
        "reference_style_update_checks",
        ["source_id", "status", "checked_at"],
        unique=False,
    )

    op.create_table(
        "reference_style_update_reviews",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("profile", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "baseline_raw_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "baseline_semantic_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("observed_version_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "observed_raw_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "observed_semantic_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reviewer", sa.String(length=255), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("reviewed_document", sa.LargeBinary(), nullable=False),
        sa.Column("document_size_bytes", sa.Integer(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(profile) <> '' and "
            "btrim(reviewer) <> '' and btrim(rationale) <> ''",
            name="ck_reference_style_reviews_identity",
        ),
        sa.CheckConstraint(
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "baseline_raw_sha256 ~ '^[0-9a-f]{64}$' and "
            "baseline_semantic_sha256 ~ '^[0-9a-f]{64}$' and "
            "observed_raw_sha256 ~ '^[0-9a-f]{64}$' and "
            "observed_semantic_sha256 ~ '^[0-9a-f]{64}$' and "
            "document_sha256 ~ '^[0-9a-f]{64}$' and "
            "review_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_style_reviews_hashes",
        ),
        sa.CheckConstraint(
            "source_url like 'https://%' and "
            "decision in ('retain_vendored', 'vendor_update_required')",
            name="ck_reference_style_reviews_decision",
        ),
        sa.CheckConstraint(
            "document_size_bytes between 1 and 65536 and "
            "document_size_bytes = octet_length(reviewed_document)",
            name="ck_reference_style_reviews_document",
        ),
        sa.CheckConstraint(
            "length(profile) <= 128 and length(source_url) <= 8192 and "
            "length(reviewer) <= 255 and length(rationale) <= 4096",
            name="ck_reference_style_reviews_bounds",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "source_definition_sha256",
                "observed_version_id",
            ],
            [
                "reference_style_observed_versions.provider_key",
                "reference_style_observed_versions.layer_id",
                "reference_style_observed_versions.source_id",
                "reference_style_observed_versions.source_definition_sha256",
                "reference_style_observed_versions.id",
            ],
            name="fk_reference_style_reviews_observed",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_style_update_reviews"),
        ),
        sa.UniqueConstraint(
            "observed_version_id",
            name="uq_reference_style_reviews_observed",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "review_sha256",
            name="uq_reference_style_reviews_hash",
        ),
    )
    op.create_index(
        "ix_reference_style_reviews_source_reviewed",
        "reference_style_update_reviews",
        ["source_id", "reviewed_at", "id"],
        unique=False,
    )

    for table_name, function_name, trigger_name, message in (
        (
            "reference_style_observed_versions",
            "prevent_reference_style_observed_mutation",
            "trg_reference_style_observed_immutable",
            "reference style observation evidence is immutable",
        ),
        (
            "reference_style_update_checks",
            "prevent_reference_style_check_mutation",
            "trg_reference_style_checks_immutable",
            "reference style check evidence is immutable",
        ),
        (
            "reference_style_update_reviews",
            "prevent_reference_style_review_mutation",
            "trg_reference_style_reviews_immutable",
            "reference style review evidence is immutable",
        ),
    ):
        op.execute(
            f"""
            CREATE FUNCTION {function_name}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '{message}' USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION {function_name}()
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name.replace(
                "_immutable", "_truncate_immutable"
            )}
            BEFORE TRUNCATE ON {table_name}
            FOR EACH STATEMENT EXECUTE FUNCTION {function_name}()
            """
        )


def downgrade() -> None:
    tables = (
        "reference_style_update_reviews",
        "reference_style_update_checks",
        "reference_style_observed_versions",
    )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE " + ", ".join(tables) + " IN SHARE ROW EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    evidence_count = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM reference_style_update_reviews) + "
            "(SELECT count(*) FROM reference_style_update_checks) + "
            "(SELECT count(*) FROM reference_style_observed_versions)"
        )
    ).scalar_one()
    if int(evidence_count):
        raise RuntimeError(
            "cannot downgrade: immutable official style update evidence "
            f"exists (rows={evidence_count})"
        )

    for table_name, function_name, trigger_name in (
        (
            "reference_style_update_reviews",
            "prevent_reference_style_review_mutation",
            "trg_reference_style_reviews_immutable",
        ),
        (
            "reference_style_update_checks",
            "prevent_reference_style_check_mutation",
            "trg_reference_style_checks_immutable",
        ),
        (
            "reference_style_observed_versions",
            "prevent_reference_style_observed_mutation",
            "trg_reference_style_observed_immutable",
        ),
    ):
        op.execute(
            f"DROP TRIGGER {trigger_name.replace(
                '_immutable', '_truncate_immutable'
            )} ON {table_name}"
        )
        op.execute(f"DROP TRIGGER {trigger_name} ON {table_name}")
        op.execute(f"DROP FUNCTION {function_name}()")

    op.drop_index(
        "ix_reference_style_reviews_source_reviewed",
        table_name="reference_style_update_reviews",
    )
    op.drop_table("reference_style_update_reviews")
    op.drop_index(
        "ix_reference_style_checks_pending",
        table_name="reference_style_update_checks",
    )
    op.drop_index(
        "ix_reference_style_checks_latest",
        table_name="reference_style_update_checks",
    )
    op.drop_table("reference_style_update_checks")
    op.drop_index(
        "ix_reference_style_observed_source_retrieved",
        table_name="reference_style_observed_versions",
    )
    op.drop_table("reference_style_observed_versions")
