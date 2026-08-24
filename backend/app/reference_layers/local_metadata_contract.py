"""Small, dependency-free contract for canonical local metadata evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any

LOCAL_METADATA_DOCUMENT_SCHEMA = "siur-local-delivery-metadata/v1"
LOCAL_METADATA_ASSET_SCHEMA = "reference-local-metadata-asset/v1"
LOCAL_METADATA_GATE_SCHEMA = (
    "reference-local-metadata-precommit-gate/v1"
)
LOCAL_METADATA_ASSET_KEY = "metadata"
LOCAL_METADATA_BINDING_KEYS = frozenset(
    {
        "provider_key",
        "layer_id",
        "source_id",
        "sync_run_id",
        "catalog_snapshot_id",
        "catalog_definition_sha256",
        "source_definition_sha256",
        "authorization_review_id",
        "authorization_review_sha256",
        "authorization_document_sha256",
        "delivery_kind",
        "content_sha256",
        "prepared_validation_sha256",
    }
)
LOCAL_METADATA_DESCRIPTOR_KEYS = frozenset(
    {
        "schema_version",
        "document_schema_version",
        "document_sha256",
        "document_size_bytes",
        "binding",
    }
)
LOCAL_METADATA_GATE_KEYS = frozenset(
    {
        "schema_version",
        "passed",
        "asset_key",
        "document_sha256",
        "document_size_bytes",
        "descriptor_sha256",
        "binding_sha256",
    }
)


def local_metadata_binding(
    *,
    provider_key: str,
    layer_id: int,
    source_id: int,
    sync_run_id: int,
    catalog_snapshot_id: int,
    catalog_definition_sha256: str,
    source_definition_sha256: str,
    authorization_review_id: int,
    authorization_review_sha256: str,
    authorization_document_sha256: str,
    delivery_kind: str,
    content_sha256: str,
    prepared_validation_sha256: str,
) -> dict[str, Any]:
    """Return the exact non-recursive version identity."""

    return {
        "provider_key": provider_key,
        "layer_id": layer_id,
        "source_id": source_id,
        "sync_run_id": sync_run_id,
        "catalog_snapshot_id": catalog_snapshot_id,
        "catalog_definition_sha256": catalog_definition_sha256,
        "source_definition_sha256": source_definition_sha256,
        "authorization_review_id": authorization_review_id,
        "authorization_review_sha256": authorization_review_sha256,
        "authorization_document_sha256": authorization_document_sha256,
        "delivery_kind": delivery_kind,
        "content_sha256": content_sha256,
        "prepared_validation_sha256": prepared_validation_sha256,
    }


def local_metadata_asset_descriptor(
    *,
    document_sha256: str,
    document_size_bytes: int,
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact immutable asset descriptor."""

    return {
        "schema_version": LOCAL_METADATA_ASSET_SCHEMA,
        "document_schema_version": LOCAL_METADATA_DOCUMENT_SCHEMA,
        "document_sha256": document_sha256,
        "document_size_bytes": document_size_bytes,
        "binding": binding,
    }


def local_metadata_precommit_gate(
    *,
    document_sha256: str,
    document_size_bytes: int,
    descriptor: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Return the promotion-attested result of precommit blob verification."""

    return {
        "schema_version": LOCAL_METADATA_GATE_SCHEMA,
        "passed": True,
        "asset_key": LOCAL_METADATA_ASSET_KEY,
        "document_sha256": document_sha256,
        "document_size_bytes": document_size_bytes,
        "descriptor_sha256": _canonical_sha256(descriptor),
        "binding_sha256": _canonical_sha256(binding),
    }


def local_metadata_gate_matches(
    validation_json: Any,
    *,
    document_sha256: Any,
    document_size_bytes: Any,
    descriptor: Any,
    binding: Any,
) -> bool:
    """Check the exact persisted gate without reading the metadata body."""

    if (
        not isinstance(validation_json, dict)
        or not isinstance(document_sha256, str)
        or len(document_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in document_sha256
        )
        or isinstance(document_size_bytes, bool)
        or not isinstance(document_size_bytes, int)
        or document_size_bytes < 1
        or not isinstance(descriptor, dict)
        or set(descriptor) != LOCAL_METADATA_DESCRIPTOR_KEYS
        or not isinstance(binding, dict)
        or set(binding) != LOCAL_METADATA_BINDING_KEYS
    ):
        return False
    try:
        expected = local_metadata_precommit_gate(
            document_sha256=document_sha256,
            document_size_bytes=document_size_bytes,
            descriptor=descriptor,
            binding=binding,
        )
    except (TypeError, ValueError, OverflowError):
        return False
    gate = validation_json.get("local_metadata_gate")
    return bool(
        isinstance(gate, dict)
        and set(gate) == LOCAL_METADATA_GATE_KEYS
        and gate == expected
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
