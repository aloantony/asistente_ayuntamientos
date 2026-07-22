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
HEAD_REVISION = "20260723_0035"
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
REFERENCE_CATALOG_TABLES = {
    "reference_catalog_snapshots",
    "reference_services",
    "reference_layers",
    "reference_layer_styles",
    "organization_reference_layer_settings",
}
REFERENCE_DELIVERY_EVIDENCE_TABLES = {
    "reference_wms_capabilities_snapshots",
    "reference_license_reviews",
    "reference_delivery_attestations",
}
REFERENCE_DELIVERY_EVIDENCE_COLUMNS = {
    "reference_wms_capabilities_snapshots": {
        "id",
        "provider_key",
        "service_id",
        "raw_xml",
        "raw_size_bytes",
        "raw_sha256",
        "normalized_sha256",
        "normalization_version",
        "wms_version",
        "get_map_endpoint",
        "get_legend_endpoint",
        "get_feature_info_endpoint",
        "get_map_formats_json",
        "get_legend_formats_json",
        "get_feature_info_formats_json",
        "layer_manifest_json",
        "created_at",
    },
    "reference_license_reviews": {
        "id",
        "provider_key",
        "service_id",
        "reviewed_document",
        "document_size_bytes",
        "evidence_sha256",
        "review_sha256",
        "supersedes_review_sha256",
        "decision",
        "reviewer",
        "reviewed_at",
        "license_name",
        "license_url",
        "license_terms",
        "allow_proxy",
        "allow_cache",
        "created_at",
    },
    "reference_delivery_attestations": {
        "id",
        "provider_key",
        "service_id",
        "catalog_snapshot_id",
        "catalog_definition_sha256",
        "capabilities_snapshot_id",
        "license_review_id",
        "attestation_kind",
        "sequence_number",
        "previous_attestation_id",
        "previous_attestation_sha256",
        "attestation_sha256",
        "created_at",
    },
}
REFERENCE_MIRROR_TABLES = {
    "reference_layer_sources",
    "reference_sync_runs",
    "reference_source_artifacts",
    "reference_sync_run_artifacts",
    "reference_delivery_versions",
    "reference_delivery_version_artifacts",
    "reference_delivery_assets",
    "reference_delivery_promotions",
    "reference_layer_delivery_state",
}
REFERENCE_MIRROR_COLUMNS = {
    "reference_layer_sources": {
        "id",
        "provider_key",
        "layer_id",
        "source_key",
        "protocol",
        "target_kind",
        "endpoint_url",
        "remote_name",
        "source_format",
        "sync_strategy",
        "config_json",
        "definition_sha256",
        "enabled",
        "is_primary",
        "priority",
        "check_interval_seconds",
        "full_refresh_interval_seconds",
        "next_check_at",
        "created_at",
        "updated_at",
    },
    "reference_sync_runs": {
        "id",
        "source_id",
        "requested_by_id",
        "source_definition_json",
        "source_definition_sha256",
        "trigger_kind",
        "check_mode",
        "status",
        "attempt_no",
        "expected_active_generation",
        "lease_token",
        "lease_expires_at",
        "heartbeat_at",
        "queued_at",
        "started_at",
        "finished_at",
        "observed_etag",
        "observed_last_modified",
        "observed_version",
        "observed_manifest_sha256",
        "error_code",
        "error_summary",
        "stats_json",
        "created_at",
        "updated_at",
    },
    "reference_source_artifacts": {
        "id",
        "source_id",
        "artifact_kind",
        "source_url",
        "final_url",
        "source_version",
        "upstream_etag",
        "upstream_last_modified",
        "media_type",
        "storage_backend",
        "storage_key",
        "size_bytes",
        "sha256",
        "metadata_json",
        "retrieved_at",
        "created_at",
    },
    "reference_sync_run_artifacts": {
        "source_id",
        "run_id",
        "artifact_id",
        "role",
        "created_at",
    },
    "reference_delivery_versions": {
        "id",
        "provider_key",
        "layer_id",
        "source_id",
        "sync_run_id",
        "catalog_snapshot_id",
        "catalog_definition_sha256",
        "sequence_number",
        "delivery_kind",
        "source_version",
        "content_sha256",
        "manifest_sha256",
        "validation_sha256",
        "reference_at",
        "crs",
        "bounds_json",
        "feature_count",
        "validation_json",
        "created_at",
    },
    "reference_delivery_version_artifacts": {
        "source_id",
        "version_id",
        "artifact_id",
        "role",
        "created_at",
    },
    "reference_delivery_assets": {
        "id",
        "version_id",
        "asset_key",
        "asset_kind",
        "is_primary",
        "storage_backend",
        "storage_key",
        "media_type",
        "sha256",
        "size_bytes",
        "metadata_json",
        "created_at",
    },
    "reference_delivery_promotions": {
        "id",
        "provider_key",
        "layer_id",
        "sequence_number",
        "action",
        "from_version_id",
        "to_version_id",
        "run_id",
        "actor_id",
        "reason",
        "previous_event_id",
        "previous_event_sha256",
        "event_sha256",
        "created_at",
    },
    "reference_layer_delivery_state": {
        "provider_key",
        "layer_id",
        "status",
        "active_version_id",
        "generation",
        "last_promotion_id",
        "updated_at",
    },
}
REFERENCE_CATALOG_COLUMNS = {
    "reference_catalog_snapshots": {
        "id",
        "provider_key",
        "source_url",
        "content_sha256",
        "definition_sha256",
        "raw_catalog_json",
        "normalized_definition_json",
        "retrieved_at",
        "service_count",
        "group_count",
        "layer_count",
        "unresolved_count",
        "status",
        "is_current",
        "created_at",
        "updated_at",
    },
    "reference_services": {
        "id",
        "last_seen_snapshot_id",
        "provider_key",
        "source_key",
        "title",
        "upstream_protocol",
        "base_url",
        "capabilities_url",
        "version",
        "default_crs",
        "default_format",
        "attribution",
        "license_name",
        "license_url",
        "license_status",
        "cache_policy",
        "capabilities_sha256",
        "status",
        "last_error",
        "created_at",
        "updated_at",
    },
    "reference_layers": {
        "id",
        "last_seen_snapshot_id",
        "service_id",
        "parent_id",
        "provider_key",
        "source_key",
        "node_type",
        "title",
        "description",
        "remote_name",
        "role",
        "renderer",
        "delivery_mode",
        "style_name",
        "image_format",
        "supported_crs_json",
        "bounds_json",
        "options_json",
        "sort_order",
        "default_visible",
        "default_opacity",
        "min_zoom",
        "max_zoom",
        "min_scale_denominator",
        "max_scale_denominator",
        "queryable",
        "downloadable",
        "legend_url",
        "metadata_url",
        "status",
        "created_at",
        "updated_at",
    },
    "reference_layer_styles": {
        "id",
        "last_seen_snapshot_id",
        "layer_id",
        "provider_key",
        "source_key",
        "title",
        "description",
        "legend_url",
        "sort_order",
        "is_default",
        "status",
        "created_at",
        "updated_at",
    },
    "organization_reference_layer_settings": {
        "id",
        "organization_id",
        "layer_id",
        "visible",
        "opacity",
        "updated_by_id",
        "created_at",
        "updated_at",
    },
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


def assert_reference_catalog_schema(inspector: Inspector) -> None:
    table_names = set(inspector.get_table_names())
    assert REFERENCE_CATALOG_TABLES <= table_names
    for table_name, expected_columns in REFERENCE_CATALOG_COLUMNS.items():
        effective_columns = set(expected_columns)
        if (
            table_name == "reference_layer_styles"
            and "reference_delivery_attestations" in table_names
        ):
            effective_columns.add("remote_name")
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == effective_columns

    assert {
        index["name"]
        for index in inspector.get_indexes("reference_catalog_snapshots")
        if not index.get("duplicates_constraint")
    } == {"uq_reference_catalog_snapshots_current_provider"}
    assert {
        index["name"]
        for index in inspector.get_indexes("reference_services")
        if not index.get("duplicates_constraint")
    } == {
        "ix_reference_services_snapshot",
        "ix_reference_services_status",
    }
    assert {
        index["name"]
        for index in inspector.get_indexes("reference_layers")
        if not index.get("duplicates_constraint")
    } == {
        "ix_reference_layers_parent_order",
        "ix_reference_layers_service_status",
        "ix_reference_layers_snapshot",
    }
    assert {
        index["name"]
        for index in inspector.get_indexes("reference_layer_styles")
        if not index.get("duplicates_constraint")
    } == {
        "ix_reference_layer_styles_layer_order",
        "ix_reference_layer_styles_snapshot",
        "uq_reference_layer_styles_default",
    }
    style_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("reference_layer_styles")
        if not index.get("duplicates_constraint")
    }
    default_style_index = style_indexes["uq_reference_layer_styles_default"]
    assert default_style_index["unique"] is True
    assert default_style_index["column_names"] == [
        "provider_key",
        "layer_id",
    ]
    assert "is_default" in str(default_style_index.get("dialect_options", {}))
    assert {
        index["name"]
        for index in inspector.get_indexes(
            "organization_reference_layer_settings"
        )
        if not index.get("duplicates_constraint")
    } == {
        "ix_org_reference_layer_settings_layer",
        "ix_org_reference_layer_settings_updated_by",
    }

    expected_snapshot_uniques = {
        "uq_reference_catalog_snapshots_provider_hashes",
        "uq_reference_catalog_snapshots_provider_id",
    }
    if "reference_delivery_attestations" in inspector.get_table_names():
        expected_snapshot_uniques.add(
            "uq_reference_catalog_snapshots_provider_id_definition"
        )
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "reference_catalog_snapshots"
        )
    } == expected_snapshot_uniques
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("reference_services")
    } == {
        "uq_reference_services_provider_id",
        "uq_reference_services_provider_source",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("reference_layers")
    } == {
        "uq_reference_layers_provider_id",
        "uq_reference_layers_provider_source",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "reference_layer_styles"
        )
    } == {
        "uq_reference_layer_styles_provider_id",
        "uq_reference_layer_styles_provider_layer_source",
    }
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("reference_services")
    } == {("provider_key", "last_seen_snapshot_id")}
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys("reference_layers")
    } == {
        ("provider_key", "last_seen_snapshot_id"),
        ("provider_key", "service_id"),
        ("provider_key", "parent_id"),
    }
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "reference_layer_styles"
        )
    } == {
        ("provider_key", "last_seen_snapshot_id"),
        ("provider_key", "layer_id"),
    }
    style_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "reference_layer_styles"
        )
    }
    assert style_foreign_keys[
        "fk_reference_layer_styles_provider_snapshot"
    ]["referred_table"] == "reference_catalog_snapshots"
    assert style_foreign_keys[
        "fk_reference_layer_styles_provider_snapshot"
    ]["referred_columns"] == ["provider_key", "id"]
    assert style_foreign_keys[
        "fk_reference_layer_styles_provider_snapshot"
    ]["options"]["ondelete"] == "RESTRICT"
    assert style_foreign_keys[
        "fk_reference_layer_styles_provider_layer"
    ]["referred_table"] == "reference_layers"
    assert style_foreign_keys[
        "fk_reference_layer_styles_provider_layer"
    ]["referred_columns"] == ["provider_key", "id"]
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "reference_layer_styles"
        )
    } == {
        "ck_reference_layer_styles_identity_nonempty",
        "ck_reference_layer_styles_legend_url",
        "ck_reference_layer_styles_sort_order",
        "ck_reference_layer_styles_status",
    }


def assert_reference_delivery_evidence_schema(inspector: Inspector) -> None:
    assert REFERENCE_DELIVERY_EVIDENCE_TABLES <= set(
        inspector.get_table_names()
    )
    for table_name, expected_columns in (
        REFERENCE_DELIVERY_EVIDENCE_COLUMNS.items()
    ):
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == expected_columns

    capability_indexes = {
        index["name"]: index
        for index in inspector.get_indexes(
            "reference_wms_capabilities_snapshots"
        )
        if not index.get("duplicates_constraint")
    }
    assert set(capability_indexes) == {
        "ix_reference_wms_capabilities_service_created"
    }
    assert capability_indexes[
        "ix_reference_wms_capabilities_service_created"
    ]["column_names"] == ["provider_key", "service_id", "id"]
    review_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("reference_license_reviews")
        if not index.get("duplicates_constraint")
    }
    assert set(review_indexes) == {
        "ix_reference_license_reviews_service_reviewed",
        "uq_reference_license_reviews_genesis",
        "uq_reference_license_reviews_successor",
    }
    assert review_indexes[
        "ix_reference_license_reviews_service_reviewed"
    ]["column_names"] == ["provider_key", "service_id", "id"]
    assert review_indexes[
        "uq_reference_license_reviews_genesis"
    ]["column_names"] == ["provider_key", "service_id"]
    assert review_indexes[
        "uq_reference_license_reviews_successor"
    ]["column_names"] == [
        "provider_key",
        "service_id",
        "supersedes_review_sha256",
    ]
    attestation_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("reference_delivery_attestations")
        if not index.get("duplicates_constraint")
    }
    assert set(attestation_indexes) == {
        "ix_reference_delivery_attestations_current_lookup",
        "uq_reference_delivery_attestations_genesis",
        "uq_reference_delivery_attestations_successor",
    }
    assert attestation_indexes[
        "ix_reference_delivery_attestations_current_lookup"
    ]["column_names"] == ["provider_key", "service_id", "sequence_number"]

    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "reference_wms_capabilities_snapshots"
        )
    } == {
        "uq_reference_wms_capabilities_content",
        "uq_reference_wms_capabilities_provider_service_id",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "reference_license_reviews"
        )
    } == {
        "uq_reference_license_reviews_content",
        "uq_reference_license_reviews_provider_service_id",
        "uq_reference_license_reviews_review_hash",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "reference_delivery_attestations"
        )
    } == {
        "uq_reference_delivery_attestations_chain_target",
        "uq_reference_delivery_attestations_hash",
        "uq_reference_delivery_attestations_sequence",
    }
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "reference_delivery_attestations"
        )
    } == {
        ("provider_key", "service_id"),
        (
            "provider_key",
            "catalog_snapshot_id",
            "catalog_definition_sha256",
        ),
        ("provider_key", "service_id", "capabilities_snapshot_id"),
        ("provider_key", "service_id", "license_review_id"),
        (
            "provider_key",
            "service_id",
            "previous_attestation_id",
            "previous_attestation_sha256",
        ),
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "reference_wms_capabilities_snapshots"
        )
    } == {
        "ck_reference_wms_capabilities_provider_nonempty",
        "ck_reference_wms_capabilities_raw_size",
        "ck_reference_wms_capabilities_hashes",
        "ck_reference_wms_capabilities_normalization",
        "ck_reference_wms_capabilities_version",
        "ck_reference_wms_capabilities_endpoints",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "reference_license_reviews"
        )
    } == {
        "ck_reference_license_reviews_required_text",
        "ck_reference_license_reviews_document_size",
        "ck_reference_license_reviews_hashes",
        "ck_reference_license_reviews_decision",
        "ck_reference_license_reviews_license_url",
        "ck_reference_license_reviews_cache_requires_proxy",
        "ck_reference_license_reviews_permissions_approved",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "reference_delivery_attestations"
        )
    } == {
        "ck_reference_delivery_attestations_provider_nonempty",
        "ck_reference_delivery_attestations_hashes",
        "ck_reference_delivery_attestations_kind",
        "ck_reference_delivery_attestations_chain",
    }
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "reference_wms_capabilities_snapshots"
        )
    } == {("provider_key", "service_id")}
    assert {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "reference_license_reviews"
        )
    } == {
        ("provider_key", "service_id"),
        ("provider_key", "service_id", "supersedes_review_sha256"),
    }


def assert_reference_mirror_schema(
    inspector: Inspector,
    *,
    shared_artifact_storage: bool = True,
) -> None:
    assert REFERENCE_MIRROR_TABLES <= set(inspector.get_table_names())
    for table_name, expected_columns in REFERENCE_MIRROR_COLUMNS.items():
        assert {
            column["name"] for column in inspector.get_columns(table_name)
        } == expected_columns

    expected_indexes = {
        "reference_layer_sources": {
            "ix_reference_layer_sources_due",
            "ix_reference_layer_sources_layer_priority",
            "uq_reference_layer_sources_primary",
        },
        "reference_sync_runs": {
            "ix_reference_sync_runs_queued",
            "ix_reference_sync_runs_requested_by",
            "ix_reference_sync_runs_running_lease",
            "ix_reference_sync_runs_source_history",
            "uq_reference_sync_runs_open_source",
        },
        "reference_source_artifacts": {
            "ix_reference_source_artifacts_source_history",
            *(
                {"ix_reference_source_artifacts_storage"}
                if shared_artifact_storage
                else set()
            ),
        },
        "reference_sync_run_artifacts": {
            "ix_reference_sync_run_artifacts_artifact",
        },
        "reference_delivery_versions": {
            "ix_reference_delivery_versions_catalog_snapshot",
            "ix_reference_delivery_versions_source_history",
        },
        "reference_delivery_version_artifacts": {
            "ix_reference_delivery_version_artifacts_artifact",
        },
        "reference_delivery_assets": {
            "ix_reference_delivery_assets_storage",
            "uq_reference_delivery_assets_primary",
        },
        "reference_delivery_promotions": {
            "ix_reference_delivery_promotions_actor",
            "ix_reference_delivery_promotions_current_lookup",
            "ix_reference_delivery_promotions_from_version",
            "ix_reference_delivery_promotions_run",
            "ix_reference_delivery_promotions_to_version",
            "uq_reference_delivery_promotions_genesis",
            "uq_reference_delivery_promotions_successor",
        },
        "reference_layer_delivery_state": {
            "ix_reference_layer_delivery_state_active_version",
            "ix_reference_layer_delivery_state_last_promotion",
        },
    }
    for table_name, expected in expected_indexes.items():
        assert {
            index["name"]
            for index in inspector.get_indexes(table_name)
            if not index.get("duplicates_constraint")
        } == expected

    source_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "reference_layer_sources"
        )
    }
    assert source_foreign_keys[
        "fk_reference_layer_sources_provider_layer"
    ]["constrained_columns"] == ["provider_key", "layer_id"]

    version_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "reference_delivery_versions"
        )
    }
    assert version_foreign_keys[
        "fk_reference_delivery_versions_layer_source"
    ]["constrained_columns"] == ["provider_key", "layer_id", "source_id"]
    assert version_foreign_keys[
        "fk_reference_delivery_versions_source_run"
    ]["constrained_columns"] == ["source_id", "sync_run_id"]
    assert version_foreign_keys[
        "fk_reference_delivery_versions_catalog"
    ]["constrained_columns"] == [
        "provider_key",
        "catalog_snapshot_id",
        "catalog_definition_sha256",
    ]

    run_artifact_foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "reference_sync_run_artifacts"
        )
    }
    assert run_artifact_foreign_keys == {
        ("source_id", "run_id"),
        ("source_id", "artifact_id"),
    }
    version_artifact_foreign_keys = {
        tuple(foreign_key["constrained_columns"])
        for foreign_key in inspector.get_foreign_keys(
            "reference_delivery_version_artifacts"
        )
    }
    assert version_artifact_foreign_keys == {
        ("source_id", "version_id"),
        ("source_id", "artifact_id"),
    }

    state_foreign_keys = {
        foreign_key["name"]: foreign_key
        for foreign_key in inspector.get_foreign_keys(
            "reference_layer_delivery_state"
        )
    }
    assert set(state_foreign_keys) == {
        "fk_reference_layer_delivery_state_provider_layer",
        "fk_reference_layer_delivery_state_active_version",
        "fk_reference_layer_delivery_state_last_promotion",
    }
    assert state_foreign_keys[
        "fk_reference_layer_delivery_state_active_version"
    ]["constrained_columns"] == [
        "provider_key",
        "layer_id",
        "active_version_id",
    ]


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
        assert_spatial_extensions(engine)
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
        assert_spatial_extensions(engine)
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
        stdout, stderr = migration_process.communicate(timeout=15)

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


def test_reference_catalog_migration_from_postgis_head_is_reversible(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0030")
    engine = create_engine(migration_database_url)

    try:
        assert REFERENCE_CATALOG_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_reference_catalog_schema(inspect(engine))
        assert_spatial_extensions(engine)

        run_alembic(migration_database_url, "downgrade", "20260717_0030")
        assert REFERENCE_CATALOG_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        assert_spatial_extensions(engine)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260717_0030"

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_reference_catalog_schema(inspect(engine))
    finally:
        engine.dispose()


def test_reference_layer_styles_migration_is_isolated_and_reversible(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0031")
    engine = create_engine(migration_database_url)

    try:
        before = inspect(engine)
        assert "reference_layers" in before.get_table_names()
        assert "reference_layer_styles" not in before.get_table_names()

        run_alembic(migration_database_url, "upgrade", "20260717_0032")
        assert_reference_catalog_schema(inspect(engine))

        run_alembic(migration_database_url, "downgrade", "20260717_0031")
        downgraded = inspect(engine)
        assert "reference_layers" in downgraded.get_table_names()
        assert "reference_layer_styles" not in downgraded.get_table_names()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260717_0031"

        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_reference_catalog_schema(inspect(engine))
    finally:
        engine.dispose()


def test_reference_delivery_evidence_migration_is_immutable_and_guarded(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0032")
    engine = create_engine(migration_database_url)

    try:
        assert REFERENCE_DELIVERY_EVIDENCE_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        run_alembic(migration_database_url, "upgrade", "20260717_0033")
        assert_reference_catalog_schema(inspect(engine))
        assert_reference_delivery_evidence_schema(inspect(engine))

        run_alembic(migration_database_url, "downgrade", "20260717_0032")
        assert REFERENCE_DELIVERY_EVIDENCE_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        run_alembic(migration_database_url, "upgrade", "head")
        assert_reference_delivery_evidence_schema(inspect(engine))

        with engine.begin() as connection:
            snapshot_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_catalog_snapshots (
                        provider_key, source_url, content_sha256,
                        definition_sha256, raw_catalog_json,
                        normalized_definition_json, retrieved_at,
                        service_count, group_count, layer_count,
                        unresolved_count, status, is_current
                    ) VALUES (
                        'siur', 'https://example.test/settings.json', :content,
                        :definition, CAST('{}' AS JSON), CAST('{}' AS JSON),
                        now(), 1, 0, 1, 0, 'applied', true
                    ) RETURNING id
                    """
                ),
                {"content": "c" * 64, "definition": "d" * 64},
            ).scalar_one()
            service_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_services (
                        last_seen_snapshot_id, provider_key, source_key, title,
                        upstream_protocol, base_url, version, license_status,
                        cache_policy, status
                    ) VALUES (
                        :snapshot_id, 'siur', 'service:test', 'Test WMS',
                        'wms',
                        'https://idecyl.jcyl.es/geoserver/test/wms',
                        '1.3.0', 'pending', 'none', 'active'
                    ) RETURNING id
                    """
                ),
                {"snapshot_id": snapshot_id},
            ).scalar_one()
            capabilities_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_wms_capabilities_snapshots (
                        provider_key, service_id, raw_xml, raw_size_bytes,
                        raw_sha256, normalized_sha256, normalization_version,
                        wms_version, get_map_endpoint,
                        get_legend_endpoint, get_feature_info_endpoint,
                        get_map_formats_json, get_legend_formats_json,
                        get_feature_info_formats_json, layer_manifest_json
                    ) VALUES (
                        'siur', :service_id, :raw_xml, :raw_size,
                        :raw_hash, :normalized_hash,
                        'siur-wms-capabilities-v1', '1.3.0',
                        'https://idecyl.jcyl.es/geoserver/test/wms',
                        'https://idecyl.jcyl.es/geoserver/test/wms',
                        'https://idecyl.jcyl.es/geoserver/test/wms',
                        CAST('["image/png"]' AS JSON),
                        CAST('["image/png"]' AS JSON),
                        CAST('["application/json"]' AS JSON),
                        CAST(:manifest AS JSON)
                    ) RETURNING id
                    """
                ),
                {
                    "service_id": service_id,
                    "raw_xml": b"<WMS_Capabilities/>",
                    "raw_size": len(b"<WMS_Capabilities/>"),
                    "raw_hash": "a" * 64,
                    "normalized_hash": "b" * 64,
                    "manifest": json.dumps(
                        [
                            {
                                "name": "test:layer",
                                "crs": ["EPSG:3857"],
                                "queryable": True,
                                "styles": [""],
                            }
                        ]
                    ),
                },
            ).scalar_one()
            review_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_license_reviews (
                        provider_key, service_id, reviewed_document,
                        document_size_bytes, evidence_sha256, review_sha256,
                        decision, reviewer, reviewed_at, license_name,
                        license_url, license_terms, allow_proxy, allow_cache
                    ) VALUES (
                        'siur', :service_id, :document, :document_size,
                        :evidence_hash, :review_hash, 'approved',
                        'Migration test reviewer', now(),
                        'Synthetic migration test license',
                        'https://example.test/license',
                        'Synthetic only; not a real SIUR approval.', true, true
                    ) RETURNING id
                    """
                ),
                {
                    "service_id": service_id,
                    "document": b"{}",
                    "document_size": 2,
                    "evidence_hash": "e" * 64,
                    "review_hash": "b" * 64,
                },
            ).scalar_one()
            attestation_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_delivery_attestations (
                        provider_key, service_id, catalog_snapshot_id,
                        catalog_definition_sha256, capabilities_snapshot_id,
                        license_review_id, attestation_kind, sequence_number,
                        previous_attestation_id,
                        previous_attestation_sha256, attestation_sha256
                    ) VALUES (
                        'siur', :service_id, :snapshot_id, :definition,
                        :capabilities_id, :review_id, 'delivery', 1,
                        NULL, NULL, :attestation_hash
                    ) RETURNING id
                    """
                ),
                {
                    "service_id": service_id,
                    "snapshot_id": snapshot_id,
                    "definition": "d" * 64,
                    "capabilities_id": capabilities_id,
                    "review_id": review_id,
                    "attestation_hash": "f" * 64,
                },
            ).scalar_one()

        invalid_review_permissions = (
            ("approved", False, True, "1" * 64, "2" * 64),
            ("restricted", True, False, "3" * 64, "4" * 64),
        )
        for decision, allow_proxy, allow_cache, evidence_hash, review_hash in (
            invalid_review_permissions
        ):
            with pytest.raises(DBAPIError) as error:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO reference_license_reviews (
                                provider_key, service_id, reviewed_document,
                                document_size_bytes, evidence_sha256,
                                review_sha256, supersedes_review_sha256,
                                decision, reviewer,
                                reviewed_at, license_name, license_terms,
                                allow_proxy, allow_cache
                            ) VALUES (
                                'siur', :service_id, CAST('{}' AS BYTEA), 2,
                                :evidence_hash, :review_hash, :supersedes,
                                :decision,
                                'Invalid permissions test', now(),
                                'Synthetic invalid license',
                                'Synthetic invalid permissions only.',
                                :allow_proxy, :allow_cache
                            )
                            """
                        ),
                        {
                            "service_id": service_id,
                            "evidence_hash": evidence_hash,
                            "review_hash": review_hash,
                            "supersedes": "b" * 64,
                            "decision": decision,
                            "allow_proxy": allow_proxy,
                            "allow_cache": allow_cache,
                        },
                    )
            assert error.value.orig.sqlstate == "23514"

        lineage_insert = text(
            """
            INSERT INTO reference_license_reviews (
                provider_key, service_id, reviewed_document,
                document_size_bytes, evidence_sha256, review_sha256,
                supersedes_review_sha256, decision, reviewer, reviewed_at,
                license_name, license_terms, allow_proxy, allow_cache
            ) VALUES (
                'siur', :service_id, CAST('{}' AS BYTEA), 2,
                :evidence_hash, :review_hash, :supersedes, 'approved',
                :reviewer, now(), 'Synthetic lineage license',
                'Synthetic lineage constraint test only.', false, false
            )
            """
        )
        with pytest.raises(DBAPIError) as duplicate_genesis:
            with engine.begin() as connection:
                connection.execute(
                    lineage_insert,
                    {
                        "service_id": service_id,
                        "evidence_hash": "5" * 64,
                        "review_hash": "6" * 64,
                        "supersedes": None,
                        "reviewer": "Duplicate genesis test",
                    },
                )
        assert duplicate_genesis.value.orig.sqlstate == "23505"

        with engine.begin() as connection:
            connection.execute(
                lineage_insert,
                {
                    "service_id": service_id,
                    "evidence_hash": "7" * 64,
                    "review_hash": "8" * 64,
                    "supersedes": "b" * 64,
                    "reviewer": "First successor test",
                },
            )
        with pytest.raises(DBAPIError) as forked_successor:
            with engine.begin() as connection:
                connection.execute(
                    lineage_insert,
                    {
                        "service_id": service_id,
                        "evidence_hash": "9" * 64,
                        "review_hash": "a" * 64,
                        "supersedes": "b" * 64,
                        "reviewer": "Forked successor test",
                    },
                )
        assert forked_successor.value.orig.sqlstate == "23505"

        mutations = (
            (
                "UPDATE reference_wms_capabilities_snapshots "
                "SET wms_version = '1.1.1' WHERE id = :row_id",
                capabilities_id,
            ),
            (
                "DELETE FROM reference_wms_capabilities_snapshots "
                "WHERE id = :row_id",
                capabilities_id,
            ),
            (
                "UPDATE reference_license_reviews "
                "SET reviewer = 'tampered' WHERE id = :row_id",
                review_id,
            ),
            (
                "DELETE FROM reference_license_reviews WHERE id = :row_id",
                review_id,
            ),
            (
                "UPDATE reference_delivery_attestations "
                "SET attestation_sha256 = :hash WHERE id = :row_id",
                attestation_id,
            ),
            (
                "DELETE FROM reference_delivery_attestations "
                "WHERE id = :row_id",
                attestation_id,
            ),
        )
        for statement, row_id in mutations:
            with pytest.raises(DBAPIError) as error:
                with engine.begin() as connection:
                    connection.execute(
                        text(statement),
                        {"row_id": row_id, "hash": "0" * 64},
                    )
            assert error.value.orig.sqlstate == "55000"

        refused = run_alembic(
            migration_database_url,
            "downgrade",
            "20260717_0032",
            check=False,
        )
        assert refused.returncode != 0
        assert "immutable reference delivery evidence exists" in refused.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260717_0033"
    finally:
        engine.dispose()


def test_reference_mirror_migration_is_reversible_immutable_and_guarded(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0033")
    engine = create_engine(migration_database_url)

    try:
        assert REFERENCE_MIRROR_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        run_alembic(migration_database_url, "upgrade", "20260717_0034")
        assert_reference_mirror_schema(
            inspect(engine),
            shared_artifact_storage=False,
        )

        run_alembic(migration_database_url, "downgrade", "20260717_0033")
        assert REFERENCE_MIRROR_TABLES.isdisjoint(
            inspect(engine).get_table_names()
        )
        run_alembic(migration_database_url, "upgrade", "head")
        run_alembic(migration_database_url, "check")
        assert_reference_mirror_schema(inspect(engine))

        with engine.begin() as connection:
            snapshot_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_catalog_snapshots (
                        provider_key, source_url, content_sha256,
                        definition_sha256, raw_catalog_json,
                        normalized_definition_json, retrieved_at,
                        service_count, group_count, layer_count,
                        unresolved_count, status, is_current
                    ) VALUES (
                        'siur', 'https://example.test/settings.json', :content,
                        :definition, CAST('{}' AS JSON), CAST('{}' AS JSON),
                        now(), 1, 0, 1, 0, 'applied', true
                    ) RETURNING id
                    """
                ),
                {"content": "a" * 64, "definition": "b" * 64},
            ).scalar_one()
            service_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_services (
                        last_seen_snapshot_id, provider_key, source_key, title,
                        upstream_protocol, base_url, license_status,
                        cache_policy, status
                    ) VALUES (
                        :snapshot_id, 'siur', 'service:mirror-test',
                        'Mirror test WMS', 'wms',
                        'https://example.test/geoserver/wms',
                        'pending', 'mirror', 'active'
                    ) RETURNING id
                    """
                ),
                {"snapshot_id": snapshot_id},
            ).scalar_one()
            layer_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_layers (
                        last_seen_snapshot_id, service_id, provider_key,
                        source_key, node_type, title, remote_name, role,
                        renderer, delivery_mode, sort_order, default_visible,
                        default_opacity, queryable, downloadable, status
                    ) VALUES (
                        :snapshot_id, :service_id, 'siur',
                        'layer:mirror-test', 'layer', 'Mirror test layer',
                        'test:layer', 'overlay', 'raster_tile', 'mirror', 0,
                        false, 1, true, true, 'active'
                    ) RETURNING id
                    """
                ),
                {"snapshot_id": snapshot_id, "service_id": service_id},
            ).scalar_one()
            source_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_layer_sources (
                        provider_key, layer_id, source_key, protocol,
                        target_kind, endpoint_url, remote_name,
                        sync_strategy, config_json, definition_sha256,
                        enabled, is_primary
                    ) VALUES (
                        'siur', :layer_id, 'source:wfs', 'wfs', 'vector',
                        'https://example.test/geoserver/wfs', 'test:layer',
                        'paged_snapshot', CAST('{}' AS JSON), :source_hash,
                        true, true
                    ) RETURNING id
                    """
                ),
                {"layer_id": layer_id, "source_hash": "c" * 64},
            ).scalar_one()
            run_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_sync_runs (
                        source_id, source_definition_json,
                        source_definition_sha256, trigger_kind, check_mode,
                        status, started_at, finished_at
                    ) VALUES (
                        :source_id, CAST('{}' AS JSON), :source_hash,
                        'manual', 'full', 'succeeded', now(), now()
                    ) RETURNING id
                    """
                ),
                {"source_id": source_id, "source_hash": "c" * 64},
            ).scalar_one()
            artifact_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_source_artifacts (
                        source_id, artifact_kind, source_url, final_url,
                        source_version, media_type, storage_backend,
                        storage_key, size_bytes, sha256, metadata_json,
                        retrieved_at
                    ) VALUES (
                        :source_id, 'dataset',
                        'https://example.test/download.zip',
                        'https://example.test/download.zip', '2026-07-22',
                        'application/zip', 'filesystem',
                        'reference/sha256/dd/dataset.zip', 42, :artifact_hash,
                        CAST('{}' AS JSON), now()
                    ) RETURNING id
                    """
                ),
                {"source_id": source_id, "artifact_hash": "d" * 64},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO reference_sync_run_artifacts (
                        source_id, run_id, artifact_id, role
                    ) VALUES (:source_id, :run_id, :artifact_id, 'input')
                    """
                ),
                {
                    "source_id": source_id,
                    "run_id": run_id,
                    "artifact_id": artifact_id,
                },
            )
            version_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_delivery_versions (
                        provider_key, layer_id, source_id, sync_run_id,
                        catalog_snapshot_id, catalog_definition_sha256,
                        sequence_number, delivery_kind, source_version,
                        content_sha256, manifest_sha256, validation_sha256,
                        reference_at, crs, bounds_json, feature_count,
                        validation_json
                    ) VALUES (
                        'siur', :layer_id, :source_id, :run_id, :snapshot_id,
                        :definition, 1, 'vector', '2026-07-22', :content,
                        :manifest, :validation, now(), 'EPSG:3857',
                        CAST(:bounds AS JSON), 1, CAST('{"passed": true}' AS JSON)
                    ) RETURNING id
                    """
                ),
                {
                    "layer_id": layer_id,
                    "source_id": source_id,
                    "run_id": run_id,
                    "snapshot_id": snapshot_id,
                    "definition": "b" * 64,
                    "content": "e" * 64,
                    "manifest": "f" * 64,
                    "validation": "1" * 64,
                    "bounds": json.dumps(
                        {"west": -7, "south": 40, "east": -1, "north": 44}
                    ),
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO reference_delivery_version_artifacts (
                        source_id, version_id, artifact_id, role
                    ) VALUES (:source_id, :version_id, :artifact_id, 'input')
                    """
                ),
                {
                    "source_id": source_id,
                    "version_id": version_id,
                    "artifact_id": artifact_id,
                },
            )
            asset_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_delivery_assets (
                        version_id, asset_key, asset_kind, is_primary,
                        storage_backend, storage_key, media_type, sha256,
                        metadata_json
                    ) VALUES (
                        :version_id, 'primary', 'vector_table', true,
                        'postgres', 'reference_data.layer_test_v1',
                        'application/x-postgis-table', :asset_hash,
                        CAST('{}' AS JSON)
                    ) RETURNING id
                    """
                ),
                {"version_id": version_id, "asset_hash": "2" * 64},
            ).scalar_one()
            promotion_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_delivery_promotions (
                        provider_key, layer_id, sequence_number, action,
                        from_version_id, to_version_id, run_id, reason,
                        previous_event_id, previous_event_sha256, event_sha256
                    ) VALUES (
                        'siur', :layer_id, 1, 'promote', NULL, :version_id,
                        :run_id, 'Initial validated local mirror', NULL, NULL,
                        :event_hash
                    ) RETURNING id
                    """
                ),
                {
                    "layer_id": layer_id,
                    "version_id": version_id,
                    "run_id": run_id,
                    "event_hash": "3" * 64,
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO reference_layer_delivery_state (
                        provider_key, layer_id, status, active_version_id,
                        generation, last_promotion_id
                    ) VALUES (
                        'siur', :layer_id, 'active', :version_id, 1,
                        :promotion_id
                    )
                    """
                ),
                {
                    "layer_id": layer_id,
                    "version_id": version_id,
                    "promotion_id": promotion_id,
                },
            )

        immutable_mutations = (
            (
                "UPDATE reference_source_artifacts SET media_type = "
                "'text/plain' WHERE id = :row_id",
                "DELETE FROM reference_source_artifacts WHERE id = :row_id",
                artifact_id,
            ),
            (
                "UPDATE reference_sync_run_artifacts SET role = 'metadata' "
                "WHERE run_id = :row_id",
                "DELETE FROM reference_sync_run_artifacts "
                "WHERE run_id = :row_id",
                run_id,
            ),
            (
                "UPDATE reference_delivery_versions SET crs = 'EPSG:4326' "
                "WHERE id = :row_id",
                "DELETE FROM reference_delivery_versions WHERE id = :row_id",
                version_id,
            ),
            (
                "UPDATE reference_delivery_version_artifacts "
                "SET role = 'metadata' WHERE version_id = :row_id",
                "DELETE FROM reference_delivery_version_artifacts "
                "WHERE version_id = :row_id",
                version_id,
            ),
            (
                "UPDATE reference_delivery_assets SET asset_key = 'tampered' "
                "WHERE id = :row_id",
                "DELETE FROM reference_delivery_assets WHERE id = :row_id",
                asset_id,
            ),
            (
                "UPDATE reference_delivery_promotions SET reason = 'tampered' "
                "WHERE id = :row_id",
                "DELETE FROM reference_delivery_promotions WHERE id = :row_id",
                promotion_id,
            ),
        )
        for update_statement, delete_statement, row_id in immutable_mutations:
            for statement in (update_statement, delete_statement):
                with pytest.raises(DBAPIError) as error:
                    with engine.begin() as connection:
                        connection.execute(
                            text(statement),
                            {"row_id": row_id},
                        )
                assert error.value.orig.sqlstate == "55000"

        refused = run_alembic(
            migration_database_url,
            "downgrade",
            "20260717_0033",
            check=False,
        )
        assert refused.returncode != 0
        assert "versioned reference mirror data exists" in refused.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == HEAD_REVISION
    finally:
        engine.dispose()


def test_reference_delivery_evidence_downgrade_waits_for_concurrent_insert(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0033")
    engine = create_engine(migration_database_url)
    writer = engine.connect()
    transaction = writer.begin()
    migration_process: subprocess.Popen[str] | None = None

    try:
        snapshot_id = writer.execute(
            text(
                """
                INSERT INTO reference_catalog_snapshots (
                    provider_key, source_url, content_sha256,
                    definition_sha256, raw_catalog_json,
                    normalized_definition_json, retrieved_at,
                    service_count, group_count, layer_count,
                    unresolved_count, status, is_current
                ) VALUES (
                    'siur', 'https://example.test/settings.json', :content,
                    :definition, CAST('{}' AS JSON), CAST('{}' AS JSON),
                    now(), 1, 0, 1, 0, 'applied', true
                ) RETURNING id
                """
            ),
            {"content": "1" * 64, "definition": "2" * 64},
        ).scalar_one()
        service_id = writer.execute(
            text(
                """
                INSERT INTO reference_services (
                    last_seen_snapshot_id, provider_key, source_key, title,
                    upstream_protocol, base_url, version, license_status,
                    cache_policy, status
                ) VALUES (
                    :snapshot_id, 'siur', 'service:concurrent',
                    'Concurrent WMS', 'wms',
                    'https://idecyl.jcyl.es/geoserver/test/wms',
                    '1.3.0', 'pending', 'none', 'active'
                ) RETURNING id
                """
            ),
            {"snapshot_id": snapshot_id},
        ).scalar_one()
        capabilities_id = writer.execute(
            text(
                """
                INSERT INTO reference_wms_capabilities_snapshots (
                    provider_key, service_id, raw_xml, raw_size_bytes,
                    raw_sha256, normalized_sha256, normalization_version,
                    wms_version, get_map_endpoint, get_legend_endpoint,
                    get_feature_info_endpoint, get_map_formats_json,
                    get_legend_formats_json,
                    get_feature_info_formats_json, layer_manifest_json
                ) VALUES (
                    'siur', :service_id, :raw_xml, :raw_size,
                    :raw_hash, :normalized_hash,
                    'siur-wms-capabilities-v1', '1.3.0',
                    'https://idecyl.jcyl.es/geoserver/test/wms', NULL, NULL,
                    CAST('["image/png"]' AS JSON), CAST('[]' AS JSON),
                    CAST('[]' AS JSON), CAST(:manifest AS JSON)
                ) RETURNING id
                """
            ),
            {
                "service_id": service_id,
                "raw_xml": b"<WMS_Capabilities/>",
                "raw_size": len(b"<WMS_Capabilities/>"),
                "raw_hash": "3" * 64,
                "normalized_hash": "4" * 64,
                "manifest": json.dumps(
                    [
                        {
                            "name": "test:layer",
                            "crs": ["EPSG:3857"],
                            "queryable": False,
                            "styles": [""],
                        }
                    ]
                ),
            },
        ).scalar_one()

        migration_process = start_alembic(
            migration_database_url,
            "downgrade",
            "20260717_0032",
        )
        wait_for_exclusive_lock(
            engine,
            "reference_wms_capabilities_snapshots",
            migration_process,
        )
        assert migration_process.poll() is None

        transaction.commit()
        stdout, stderr = migration_process.communicate(timeout=15)
        assert migration_process.returncode != 0, stdout
        assert "immutable reference delivery evidence exists" in stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260717_0033"
            assert connection.execute(
                text(
                    "SELECT count(*) FROM "
                    "reference_wms_capabilities_snapshots WHERE id = :id"
                ),
                {"id": capabilities_id},
            ).scalar_one() == 1
    finally:
        if migration_process is not None and migration_process.poll() is None:
            migration_process.kill()
            migration_process.communicate()
        if transaction.is_active:
            transaction.rollback()
        writer.close()
        engine.dispose()


def test_reference_delivery_evidence_downgrade_preserves_exact_style_names(
    migration_database_url: str,
) -> None:
    run_alembic(migration_database_url, "upgrade", "20260717_0033")
    engine = create_engine(migration_database_url)

    try:
        with engine.begin() as connection:
            snapshot_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_catalog_snapshots (
                        provider_key, source_url, content_sha256,
                        definition_sha256, raw_catalog_json,
                        normalized_definition_json, retrieved_at,
                        service_count, group_count, layer_count,
                        unresolved_count, status, is_current
                    ) VALUES (
                        'siur', 'https://example.test/settings.json', :content,
                        :definition, CAST('{}' AS JSON), CAST('{}' AS JSON),
                        now(), 1, 0, 1, 0, 'applied', true
                    ) RETURNING id
                    """
                ),
                {"content": "5" * 64, "definition": "6" * 64},
            ).scalar_one()
            service_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_services (
                        last_seen_snapshot_id, provider_key, source_key, title,
                        upstream_protocol, base_url, version, license_status,
                        cache_policy, status
                    ) VALUES (
                        :snapshot_id, 'siur', 'service:style', 'Style WMS',
                        'wms',
                        'https://idecyl.jcyl.es/geoserver/test/wms',
                        '1.3.0', 'pending', 'none', 'active'
                    ) RETURNING id
                    """
                ),
                {"snapshot_id": snapshot_id},
            ).scalar_one()
            layer_id = connection.execute(
                text(
                    """
                    INSERT INTO reference_layers (
                        last_seen_snapshot_id, service_id, provider_key,
                        source_key, node_type, title, remote_name, role,
                        renderer, delivery_mode, sort_order, default_visible,
                        default_opacity, queryable, downloadable, status
                    ) VALUES (
                        :snapshot_id, :service_id, 'siur', 'layer:style',
                        'layer', 'Style layer', 'test:layer', 'overlay',
                        'raster_tile', 'proxy', 0, false, 1, false, false,
                        'active'
                    ) RETURNING id
                    """
                ),
                {"snapshot_id": snapshot_id, "service_id": service_id},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO reference_layer_styles (
                        last_seen_snapshot_id, layer_id, provider_key,
                        source_key, remote_name, title, sort_order,
                        is_default, status
                    ) VALUES (
                        :snapshot_id, :layer_id, 'siur', 'mixed:style',
                        'Mixed:Style', 'Mixed style', 0, true, 'active'
                    )
                    """
                ),
                {"snapshot_id": snapshot_id, "layer_id": layer_id},
            )

        refused = run_alembic(
            migration_database_url,
            "downgrade",
            "20260717_0032",
            check=False,
        )
        assert refused.returncode != 0
        assert "exact remote style names would be lost" in refused.stderr
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "20260717_0033"
            assert connection.execute(
                text(
                    "SELECT remote_name FROM reference_layer_styles "
                    "WHERE id = (SELECT max(id) FROM reference_layer_styles)"
                )
            ).scalar_one() == "Mixed:Style"
    finally:
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
        assert_maintenance_trigger(engine)
        assert_spatial_extensions(engine)
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
        assert_spatial_extensions(engine)
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
