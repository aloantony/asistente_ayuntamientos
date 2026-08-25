"""create explicit local mirror authorizations

Revision ID: 20260726_0042
Revises: 20260726_0041
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0042"
down_revision: str | None = "20260726_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE = "reference_mirror_authorization_reviews"
ROW_FUNCTION = "prevent_reference_mirror_authorization_mutation"
ROW_TRIGGER = "trg_reference_mirror_authorizations_immutable"
TRUNCATE_TRIGGER = "trg_reference_mirror_authorizations_truncate_immutable"
VALIDATE_FUNCTION = "validate_reference_mirror_authorization"
VALIDATE_TRIGGER = "trg_reference_mirror_authorizations_validate"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_reference_layers_provider_id_service",
        "reference_layers",
        ["provider_key", "id", "service_id"],
    )
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("layer_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("protocol", sa.String(length=32), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("canonical_origin", sa.Text(), nullable=False),
        sa.Column("allowed_origins_json", sa.JSON(), nullable=False),
        sa.Column("reviewed_document", sa.LargeBinary(), nullable=False),
        sa.Column("document_size_bytes", sa.Integer(), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_sha256", sa.String(length=64), nullable=False),
        sa.Column("supersedes_review_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "supersedes_review_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reviewer", sa.String(length=255), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("license_name", sa.String(length=500), nullable=False),
        sa.Column("license_url", sa.Text(), nullable=False),
        sa.Column("license_terms", sa.Text(), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column(
            "allow_metadata_probe",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_dataset_download",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_local_storage",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_local_service",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_bulk_tile_seed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> '' and btrim(reviewer) <> '' and "
            "btrim(license_name) <> '' and btrim(license_terms) <> ''",
            name="ck_reference_mirror_authorizations_required_text",
        ),
        sa.CheckConstraint(
            "document_size_bytes between 1 and 262144 and "
            "document_size_bytes = octet_length(reviewed_document)",
            name="ck_reference_mirror_authorizations_document_size",
        ),
        sa.CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$' and "
            "review_sha256 ~ '^[0-9a-f]{64}$' and "
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "(supersedes_review_sha256 is null or "
            "supersedes_review_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_mirror_authorizations_hashes",
        ),
        sa.CheckConstraint(
            "decision in ('approved', 'restricted', 'rejected')",
            name="ck_reference_mirror_authorizations_decision",
        ),
        sa.CheckConstraint(
            "protocol in ('wfs', 'ogc_api_features', 'wcs', "
            "'arcgis_rest', 'atom', 'download', 'wmts', 'xyz', "
            "'wms_tiles', 'local') and "
            "target_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_mirror_authorizations_source_kind",
        ),
        sa.CheckConstraint(
            "canonical_origin like 'https://%' and "
            "license_url like 'https://%' and "
            "json_typeof(allowed_origins_json) = 'array' and "
            "json_array_length(allowed_origins_json) between 1 and 32",
            name="ck_reference_mirror_authorizations_urls",
        ),
        sa.CheckConstraint(
            "(supersedes_review_id is null and "
            "supersedes_review_sha256 is null) or "
            "(supersedes_review_id is not null and "
            "supersedes_review_sha256 is not null)",
            name="ck_reference_mirror_authorizations_chain_shape",
        ),
        sa.CheckConstraint(
            "(not allow_metadata_probe and not allow_dataset_download and "
            "not allow_local_storage and not allow_local_service and "
            "not allow_bulk_tile_seed) or decision = 'approved'",
            name="ck_reference_mirror_authorizations_approved_permissions",
        ),
        sa.CheckConstraint(
            "not allow_local_storage or allow_dataset_download",
            name="ck_reference_mirror_authorizations_storage_download",
        ),
        sa.CheckConstraint(
            "not allow_local_service or allow_local_storage",
            name="ck_reference_mirror_authorizations_service_storage",
        ),
        sa.CheckConstraint(
            "not allow_local_service or "
            "(attribution is not null and btrim(attribution) <> '')",
            name="ck_reference_mirror_authorizations_service_attribution",
        ),
        sa.CheckConstraint(
            "target_kind <> 'tiles' or not allow_local_service or "
            "allow_bulk_tile_seed",
            name="ck_reference_mirror_authorizations_tiles_seed",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "layer_id", "service_id"],
            [
                "reference_layers.provider_key",
                "reference_layers.id",
                "reference_layers.service_id",
            ],
            name="fk_reference_mirror_authorizations_layer_service",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
            ],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_mirror_authorizations_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "supersedes_review_id",
                "supersedes_review_sha256",
            ],
            [
                "reference_mirror_authorization_reviews.provider_key",
                "reference_mirror_authorization_reviews.layer_id",
                "reference_mirror_authorization_reviews.source_id",
                "reference_mirror_authorization_reviews.id",
                "reference_mirror_authorization_reviews.review_sha256",
            ],
            name="fk_reference_mirror_authorizations_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_reference_mirror_authorization_reviews"),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "document_sha256",
            "review_sha256",
            name="uq_reference_mirror_authorizations_content",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "review_sha256",
            name="uq_reference_mirror_authorizations_review_hash",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "id",
            "review_sha256",
            name="uq_reference_mirror_authorizations_chain_target",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "source_definition_sha256",
            "id",
            "review_sha256",
            name="uq_reference_mirror_authorizations_run_target",
        ),
    )
    op.create_index(
        "ix_reference_mirror_authorizations_source_reviewed",
        TABLE,
        [
            "provider_key",
            "layer_id",
            "source_id",
            "reviewed_at",
            "id",
        ],
    )
    op.create_index(
        "uq_reference_mirror_authorizations_genesis",
        TABLE,
        ["provider_key", "layer_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_review_id is null"),
    )
    op.create_index(
        "uq_reference_mirror_authorizations_successor",
        TABLE,
        [
            "provider_key",
            "layer_id",
            "source_id",
            "supersedes_review_id",
        ],
        unique=True,
        postgresql_where=sa.text("supersedes_review_id is not null"),
    )

    _create_review_validation_trigger()
    _create_immutable_triggers()
    _add_run_authorization_link()
    _add_version_authorization_link()


def _add_run_authorization_link() -> None:
    op.add_column(
        "reference_sync_runs",
        sa.Column(
            "mirror_authorization_review_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )
    op.add_column(
        "reference_sync_runs",
        sa.Column(
            "mirror_authorization_review_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_reference_sync_runs_authorization",
        "reference_sync_runs",
        "(mirror_authorization_review_id is null and "
        "mirror_authorization_review_sha256 is null) or "
        "(mirror_authorization_review_id is not null and "
        "mirror_authorization_review_sha256 ~ '^[0-9a-f]{64}$')",
    )
    op.create_unique_constraint(
        "uq_reference_sync_runs_authorization",
        "reference_sync_runs",
        [
            "source_id",
            "id",
            "mirror_authorization_review_id",
            "mirror_authorization_review_sha256",
        ],
    )
    op.create_foreign_key(
        "fk_reference_sync_runs_mirror_authorization",
        "reference_sync_runs",
        TABLE,
        [
            "provider_key",
            "layer_id",
            "source_id",
            "source_definition_sha256",
            "mirror_authorization_review_id",
            "mirror_authorization_review_sha256",
        ],
        [
            "provider_key",
            "layer_id",
            "source_id",
            "source_definition_sha256",
            "id",
            "review_sha256",
        ],
        ondelete="RESTRICT",
    )


def _add_version_authorization_link() -> None:
    op.add_column(
        "reference_delivery_versions",
        sa.Column(
            "mirror_authorization_review_id",
            sa.BigInteger(),
            nullable=True,
        ),
    )
    op.add_column(
        "reference_delivery_versions",
        sa.Column(
            "mirror_authorization_review_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_reference_delivery_versions_authorization",
        "reference_delivery_versions",
        "(mirror_authorization_review_id is null and "
        "mirror_authorization_review_sha256 is null) or "
        "(mirror_authorization_review_id is not null and "
        "mirror_authorization_review_sha256 ~ '^[0-9a-f]{64}$')",
    )
    op.create_foreign_key(
        "fk_reference_delivery_versions_run_authorization",
        "reference_delivery_versions",
        "reference_sync_runs",
        [
            "source_id",
            "sync_run_id",
            "mirror_authorization_review_id",
            "mirror_authorization_review_sha256",
        ],
        [
            "source_id",
            "id",
            "mirror_authorization_review_id",
            "mirror_authorization_review_sha256",
        ],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_reference_delivery_versions_mirror_authorization",
        "reference_delivery_versions",
        TABLE,
        [
            "provider_key",
            "layer_id",
            "source_id",
            "mirror_authorization_review_id",
            "mirror_authorization_review_sha256",
        ],
        [
            "provider_key",
            "layer_id",
            "source_id",
            "id",
            "review_sha256",
        ],
        ondelete="RESTRICT",
    )


def _create_review_validation_trigger() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {VALIDATE_FUNCTION}()
        RETURNS trigger AS $$
        DECLARE
            origin_count integer;
            distinct_origin_count integer;
        BEGIN
            SELECT count(*), count(DISTINCT value)
            INTO origin_count, distinct_origin_count
            FROM json_array_elements_text(NEW.allowed_origins_json);

            IF origin_count <> distinct_origin_count OR EXISTS (
                SELECT 1
                FROM json_array_elements(
                    NEW.allowed_origins_json
                ) AS origin(value)
                WHERE json_typeof(origin.value) <> 'string'
                   OR origin.value #>> '{{}}'
                      !~ '^https://(\\[[0-9A-Fa-f:.]+\\]|[A-Za-z0-9.-]+)(:[0-9]+)?$'
            ) OR NOT EXISTS (
                SELECT 1
                FROM json_array_elements_text(
                    NEW.allowed_origins_json
                ) AS allowed(value)
                WHERE allowed.value = NEW.canonical_origin
            ) THEN
                RAISE EXCEPTION
                    'mirror authorization origins are invalid'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.supersedes_review_id = NEW.id THEN
                RAISE EXCEPTION
                    'mirror authorization cannot supersede itself'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.supersedes_review_id IS NOT NULL AND NOT EXISTS (
                SELECT 1
                FROM {TABLE} AS previous
                WHERE previous.provider_key = NEW.provider_key
                  AND previous.layer_id = NEW.layer_id
                  AND previous.source_id = NEW.source_id
                  AND previous.id = NEW.supersedes_review_id
                  AND previous.review_sha256
                      = NEW.supersedes_review_sha256
                  AND previous.reviewed_at < NEW.reviewed_at
            ) THEN
                RAISE EXCEPTION
                    'mirror authorization predecessor is invalid'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER {VALIDATE_TRIGGER}
        BEFORE INSERT ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {VALIDATE_FUNCTION}();
        """
    )


def _create_immutable_triggers() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {ROW_FUNCTION}()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'reference mirror authorization is immutable'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER {ROW_TRIGGER}
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {ROW_FUNCTION}();

        CREATE TRIGGER {TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON {TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION {ROW_FUNCTION}();
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM {TABLE}) THEN
                RAISE EXCEPTION
                    'cannot downgrade: immutable mirror authorizations exist';
            END IF;
        END $$;
        """
    )

    op.drop_constraint(
        "fk_reference_delivery_versions_mirror_authorization",
        "reference_delivery_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reference_delivery_versions_run_authorization",
        "reference_delivery_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_reference_delivery_versions_authorization",
        "reference_delivery_versions",
        type_="check",
    )
    op.drop_column(
        "reference_delivery_versions",
        "mirror_authorization_review_sha256",
    )
    op.drop_column(
        "reference_delivery_versions",
        "mirror_authorization_review_id",
    )

    op.drop_constraint(
        "fk_reference_sync_runs_mirror_authorization",
        "reference_sync_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_reference_sync_runs_authorization",
        "reference_sync_runs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_reference_sync_runs_authorization",
        "reference_sync_runs",
        type_="check",
    )
    op.drop_column(
        "reference_sync_runs",
        "mirror_authorization_review_sha256",
    )
    op.drop_column(
        "reference_sync_runs",
        "mirror_authorization_review_id",
    )

    op.execute(f"DROP TRIGGER {TRUNCATE_TRIGGER} ON {TABLE}")
    op.execute(f"DROP TRIGGER {ROW_TRIGGER} ON {TABLE}")
    op.execute(f"DROP FUNCTION {ROW_FUNCTION}()")
    op.execute(f"DROP TRIGGER {VALIDATE_TRIGGER} ON {TABLE}")
    op.execute(f"DROP FUNCTION {VALIDATE_FUNCTION}()")
    op.drop_table(TABLE)

    op.drop_constraint(
        "uq_reference_layers_provider_id_service",
        "reference_layers",
        type_="unique",
    )
