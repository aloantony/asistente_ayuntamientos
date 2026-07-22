"""create immutable reference delivery evidence

Revision ID: 20260717_0033
Revises: 20260717_0032
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260717_0033"
down_revision: str | None = "20260717_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_reference_catalog_snapshots_provider_id_definition",
        "reference_catalog_snapshots",
        ["provider_key", "id", "definition_sha256"],
    )
    op.drop_constraint(
        "ck_reference_layer_styles_identity_nonempty",
        "reference_layer_styles",
        type_="check",
    )
    op.add_column(
        "reference_layer_styles",
        sa.Column("remote_name", sa.String(length=255), nullable=True),
    )
    op.execute("UPDATE reference_layer_styles SET remote_name = source_key")
    op.alter_column(
        "reference_layer_styles",
        "remote_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_reference_layer_styles_identity_nonempty",
        "reference_layer_styles",
        "btrim(provider_key) <> '' and btrim(source_key) <> '' and "
        "btrim(remote_name) <> '' and btrim(title) <> ''",
    )

    op.create_table(
        "reference_wms_capabilities_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("raw_xml", sa.LargeBinary(), nullable=False),
        sa.Column("raw_size_bytes", sa.Integer(), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("normalized_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "normalization_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("wms_version", sa.String(length=16), nullable=False),
        sa.Column("get_map_endpoint", sa.Text(), nullable=False),
        sa.Column("get_legend_endpoint", sa.Text(), nullable=True),
        sa.Column("get_feature_info_endpoint", sa.Text(), nullable=True),
        sa.Column("get_map_formats_json", sa.JSON(), nullable=False),
        sa.Column("get_legend_formats_json", sa.JSON(), nullable=False),
        sa.Column("get_feature_info_formats_json", sa.JSON(), nullable=False),
        sa.Column("layer_manifest_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_wms_capabilities_provider_nonempty",
        ),
        sa.CheckConstraint(
            "raw_size_bytes between 1 and 4194304 "
            "and raw_size_bytes = octet_length(raw_xml)",
            name="ck_reference_wms_capabilities_raw_size",
        ),
        sa.CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$' "
            "and normalized_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_wms_capabilities_hashes",
        ),
        sa.CheckConstraint(
            "normalization_version = 'siur-wms-capabilities-v1'",
            name="ck_reference_wms_capabilities_normalization",
        ),
        sa.CheckConstraint(
            "wms_version in ('1.1.1', '1.3.0')",
            name="ck_reference_wms_capabilities_version",
        ),
        sa.CheckConstraint(
            "get_map_endpoint like 'https://%' and "
            "(get_legend_endpoint is null or "
            "get_legend_endpoint like 'https://%') and "
            "(get_feature_info_endpoint is null or "
            "get_feature_info_endpoint like 'https://%')",
            name="ck_reference_wms_capabilities_endpoints",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_wms_capabilities_provider_service",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "raw_sha256",
            "normalized_sha256",
            name="uq_reference_wms_capabilities_content",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "id",
            name="uq_reference_wms_capabilities_provider_service_id",
        ),
    )
    op.create_index(
        "ix_reference_wms_capabilities_service_created",
        "reference_wms_capabilities_snapshots",
        ["provider_key", "service_id", "id"],
    )

    op.create_table(
        "reference_license_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("reviewed_document", sa.LargeBinary(), nullable=False),
        sa.Column("document_size_bytes", sa.Integer(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "supersedes_review_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reviewer", sa.String(length=255), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("license_name", sa.String(length=500), nullable=False),
        sa.Column("license_url", sa.Text(), nullable=True),
        sa.Column("license_terms", sa.Text(), nullable=False),
        sa.Column(
            "allow_proxy",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "allow_cache",
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
            "btrim(provider_key) <> '' and btrim(reviewer) <> '' "
            "and btrim(license_name) <> '' and btrim(license_terms) <> ''",
            name="ck_reference_license_reviews_required_text",
        ),
        sa.CheckConstraint(
            "document_size_bytes between 1 and 262144 "
            "and document_size_bytes = octet_length(reviewed_document)",
            name="ck_reference_license_reviews_document_size",
        ),
        sa.CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$' "
            "and review_sha256 ~ '^[0-9a-f]{64}$' and "
            "(supersedes_review_sha256 is null or "
            "supersedes_review_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_license_reviews_hashes",
        ),
        sa.CheckConstraint(
            "decision in ('approved', 'restricted', 'rejected')",
            name="ck_reference_license_reviews_decision",
        ),
        sa.CheckConstraint(
            "license_url is null or license_url like 'https://%'",
            name="ck_reference_license_reviews_license_url",
        ),
        sa.CheckConstraint(
            "not allow_cache or allow_proxy",
            name="ck_reference_license_reviews_cache_requires_proxy",
        ),
        sa.CheckConstraint(
            "(not allow_proxy and not allow_cache) or decision = 'approved'",
            name="ck_reference_license_reviews_permissions_approved",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_license_reviews_provider_service",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id", "supersedes_review_sha256"],
            [
                "reference_license_reviews.provider_key",
                "reference_license_reviews.service_id",
                "reference_license_reviews.review_sha256",
            ],
            name="fk_reference_license_reviews_supersedes",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "evidence_sha256",
            "review_sha256",
            name="uq_reference_license_reviews_content",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "id",
            name="uq_reference_license_reviews_provider_service_id",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "review_sha256",
            name="uq_reference_license_reviews_review_hash",
        ),
    )
    op.create_index(
        "ix_reference_license_reviews_service_reviewed",
        "reference_license_reviews",
        ["provider_key", "service_id", "id"],
    )
    op.create_index(
        "uq_reference_license_reviews_genesis",
        "reference_license_reviews",
        ["provider_key", "service_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_review_sha256 is null"),
    )
    op.create_index(
        "uq_reference_license_reviews_successor",
        "reference_license_reviews",
        ["provider_key", "service_id", "supersedes_review_sha256"],
        unique=True,
        postgresql_where=sa.text("supersedes_review_sha256 is not null"),
    )

    op.create_table(
        "reference_delivery_attestations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("catalog_snapshot_id", sa.Integer(), nullable=False),
        sa.Column(
            "catalog_definition_sha256",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("capabilities_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("license_review_id", sa.Integer(), nullable=False),
        sa.Column("attestation_kind", sa.String(length=20), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("previous_attestation_id", sa.Integer(), nullable=True),
        sa.Column(
            "previous_attestation_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("attestation_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_delivery_attestations_provider_nonempty",
        ),
        sa.CheckConstraint(
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$' "
            "and attestation_sha256 ~ '^[0-9a-f]{64}$' and "
            "(previous_attestation_sha256 is null or "
            "previous_attestation_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_delivery_attestations_hashes",
        ),
        sa.CheckConstraint(
            "attestation_kind in ('delivery', 'revocation')",
            name="ck_reference_delivery_attestations_kind",
        ),
        sa.CheckConstraint(
            "(sequence_number = 1 and previous_attestation_id is null and "
            "previous_attestation_sha256 is null) or "
            "(sequence_number > 1 and previous_attestation_id is not null "
            "and previous_attestation_sha256 is not null)",
            name="ck_reference_delivery_attestations_chain",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_delivery_attestations_provider_service",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "catalog_snapshot_id",
                "catalog_definition_sha256",
            ],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
                "reference_catalog_snapshots.definition_sha256",
            ],
            name="fk_reference_delivery_attestations_catalog",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id", "capabilities_snapshot_id"],
            [
                "reference_wms_capabilities_snapshots.provider_key",
                "reference_wms_capabilities_snapshots.service_id",
                "reference_wms_capabilities_snapshots.id",
            ],
            name="fk_reference_delivery_attestations_capabilities",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_key", "service_id", "license_review_id"],
            [
                "reference_license_reviews.provider_key",
                "reference_license_reviews.service_id",
                "reference_license_reviews.id",
            ],
            name="fk_reference_delivery_attestations_license",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "provider_key",
                "service_id",
                "previous_attestation_id",
                "previous_attestation_sha256",
            ],
            [
                "reference_delivery_attestations.provider_key",
                "reference_delivery_attestations.service_id",
                "reference_delivery_attestations.id",
                "reference_delivery_attestations.attestation_sha256",
            ],
            name="fk_reference_delivery_attestations_previous",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "attestation_sha256",
            name="uq_reference_delivery_attestations_hash",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "sequence_number",
            name="uq_reference_delivery_attestations_sequence",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "service_id",
            "id",
            "attestation_sha256",
            name="uq_reference_delivery_attestations_chain_target",
        ),
    )
    op.create_index(
        "ix_reference_delivery_attestations_current_lookup",
        "reference_delivery_attestations",
        ["provider_key", "service_id", "sequence_number"],
    )
    op.create_index(
        "uq_reference_delivery_attestations_genesis",
        "reference_delivery_attestations",
        ["provider_key", "service_id"],
        unique=True,
        postgresql_where=sa.text("previous_attestation_id is null"),
    )
    op.create_index(
        "uq_reference_delivery_attestations_successor",
        "reference_delivery_attestations",
        ["provider_key", "service_id", "previous_attestation_id"],
        unique=True,
        postgresql_where=sa.text("previous_attestation_id is not null"),
    )

    immutable_tables = (
        (
            "reference_wms_capabilities_snapshots",
            "prevent_reference_wms_capabilities_mutation",
            "trg_reference_wms_capabilities_immutable",
        ),
        (
            "reference_license_reviews",
            "prevent_reference_license_review_mutation",
            "trg_reference_license_reviews_immutable",
        ),
        (
            "reference_delivery_attestations",
            "prevent_reference_delivery_attestation_mutation",
            "trg_reference_delivery_attestations_immutable",
        ),
    )
    for table_name, function_name, trigger_name in immutable_tables:
        op.execute(
            f"""
            CREATE FUNCTION {function_name}()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'reference delivery evidence is immutable'
                USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION {function_name}()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE reference_layer_styles, "
        "reference_wms_capabilities_snapshots, "
        "reference_license_reviews, reference_delivery_attestations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    counts = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT count(*) FROM "
            "reference_wms_capabilities_snapshots) AS capabilities, "
            "(SELECT count(*) FROM reference_license_reviews) AS reviews, "
            "(SELECT count(*) FROM "
            "reference_delivery_attestations) AS attestations, "
            "(SELECT count(*) FROM reference_layer_styles "
            "WHERE remote_name <> source_key) AS exact_style_names"
        )
    ).mappings().one()
    if int(counts["exact_style_names"]):
        raise RuntimeError(
            "cannot downgrade: exact remote style names would be lost "
            f"(styles={counts['exact_style_names']})"
        )
    evidence_counts = {
        key: counts[key]
        for key in ("capabilities", "reviews", "attestations")
    }
    if any(int(value) for value in evidence_counts.values()):
        raise RuntimeError(
            "cannot downgrade: immutable reference delivery evidence exists "
            f"(capabilities={counts['capabilities']}, "
            f"reviews={counts['reviews']}, "
            f"attestations={counts['attestations']})"
        )

    immutable_tables = (
        (
            "reference_delivery_attestations",
            "prevent_reference_delivery_attestation_mutation",
            "trg_reference_delivery_attestations_immutable",
        ),
        (
            "reference_license_reviews",
            "prevent_reference_license_review_mutation",
            "trg_reference_license_reviews_immutable",
        ),
        (
            "reference_wms_capabilities_snapshots",
            "prevent_reference_wms_capabilities_mutation",
            "trg_reference_wms_capabilities_immutable",
        ),
    )
    for table_name, function_name, trigger_name in immutable_tables:
        op.execute(f"DROP TRIGGER {trigger_name} ON {table_name}")
        op.execute(f"DROP FUNCTION {function_name}()")

    op.drop_index(
        "ix_reference_delivery_attestations_current_lookup",
        table_name="reference_delivery_attestations",
    )
    op.drop_table("reference_delivery_attestations")
    op.drop_index(
        "uq_reference_license_reviews_successor",
        table_name="reference_license_reviews",
    )
    op.drop_index(
        "uq_reference_license_reviews_genesis",
        table_name="reference_license_reviews",
    )
    op.drop_index(
        "ix_reference_license_reviews_service_reviewed",
        table_name="reference_license_reviews",
    )
    op.drop_table("reference_license_reviews")
    op.drop_index(
        "ix_reference_wms_capabilities_service_created",
        table_name="reference_wms_capabilities_snapshots",
    )
    op.drop_table("reference_wms_capabilities_snapshots")
    op.drop_constraint(
        "ck_reference_layer_styles_identity_nonempty",
        "reference_layer_styles",
        type_="check",
    )
    op.drop_column("reference_layer_styles", "remote_name")
    op.create_check_constraint(
        "ck_reference_layer_styles_identity_nonempty",
        "reference_layer_styles",
        "btrim(provider_key) <> '' and btrim(source_key) <> '' and "
        "btrim(title) <> ''",
    )
    op.drop_constraint(
        "uq_reference_catalog_snapshots_provider_id_definition",
        "reference_catalog_snapshots",
        type_="unique",
    )
