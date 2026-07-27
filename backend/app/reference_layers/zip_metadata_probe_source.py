"""Hash-gated provisioning for disabled SIGPAC metadata-probe sources.

The ZIP metadata probe deliberately cannot use ordinary acquisition sources:
its source must remain disabled, non-primary and manual so that neither the
scheduler nor the mirror worker can turn a metadata review into a dataset
download.  This module provides the missing operator boundary for creating
that exact row without updating or replacing any existing source.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.catalog import lock_catalog_provider
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.idecyl_exact_evidence import (
    ReviewedIDECyLExactSource,
    idecyl_exact_source_inventory,
)
from app.reference_layers.mirror_lifecycle import (
    catalog_snapshot_contains_active_layer,
    stored_catalog_snapshot_is_valid,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceService,
)
from app.reference_layers.zip_metadata_probe import (
    PROBE_SOURCE_CONFIG_SCHEMA,
    ZipMetadataProbeError,
    build_sigpac_zip_metadata_probe_plan,
    metadata_probe_source_definition,
)

PROVISION_PLAN_SCHEMA = "siur-zip-metadata-probe-source-plan/v1"
MAX_LOCAL_INDEX_BYTES = 64 * 1024


class MetadataProbeSourceError(RuntimeError):
    """A metadata-only source cannot be planned or provisioned safely."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MetadataProbeSourcePlan:
    """Exact append-only source transition reviewed by the operator."""

    provider_key: str
    catalog_snapshot_id: int
    catalog_definition_sha256: str
    audit_layer_id: int
    layer_id: int
    layer_source_key: str
    source_key: str
    source_definition: dict[str, Any]
    source_definition_sha256: str
    source_state: dict[str, Any]
    probe_plan_sha256: str
    action: str
    existing_source_id: int | None

    def semantic_document(self) -> dict[str, Any]:
        return {
            "schema": PROVISION_PLAN_SCHEMA,
            "provider_key": self.provider_key,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "catalog_definition_sha256": self.catalog_definition_sha256,
            "audit_layer_id": self.audit_layer_id,
            "layer_id": self.layer_id,
            "layer_source_key": self.layer_source_key,
            "source_key": self.source_key,
            "source_definition": self.source_definition,
            "source_definition_sha256": self.source_definition_sha256,
            "source_state": self.source_state,
            "probe_plan_sha256": self.probe_plan_sha256,
        }

    @property
    def plan_sha256(self) -> str:
        return canonical_json_sha256(self.semantic_document())

    def public_summary(self, *, applied: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "apply" if applied else "dry-run",
            "applied": applied and self.action == "create",
            "already_applied": self.action == "already_exists",
            "plan_sha256": self.plan_sha256,
            **self.semantic_document(),
            "action": self.action,
            "existing_source_id": self.existing_source_id,
            "safety": {
                "network_performed": False,
                "authorization_created": False,
                "dataset_download_allowed": False,
                "delivery_mutation_performed": False,
            },
        }


def plan_metadata_probe_source(
    db: Session,
    *,
    audit_layer_id: int,
    root_index: bytes,
    province_index: bytes,
    provider_key: str = "siur",
    _lock_rows: bool = False,
) -> MetadataProbeSourcePlan:
    """Plan one exact disabled/manual source against the current catalog."""

    if provider_key != "siur":
        raise MetadataProbeSourceError(
            "SIGPAC metadata probe sources are scoped to provider siur",
            code="provider_invalid",
        )
    reviewed = _reviewed_sigpac(audit_layer_id)
    try:
        probe_plan = build_sigpac_zip_metadata_probe_plan(
            reviewed,
            root_index=root_index,
            province_index=province_index,
        )
    except ZipMetadataProbeError as error:
        raise MetadataProbeSourceError(
            str(error),
            code=error.code,
        ) from error

    snapshot_query = select(ReferenceCatalogSnapshot).where(
        ReferenceCatalogSnapshot.provider_key == provider_key,
        ReferenceCatalogSnapshot.is_current.is_(True),
        ReferenceCatalogSnapshot.status == "applied",
    )
    if _lock_rows:
        snapshot_query = snapshot_query.with_for_update()
    snapshot = db.scalar(snapshot_query)
    if snapshot is None:
        raise MetadataProbeSourceError(
            "current applied SIUR catalog is unavailable",
            code="catalog_unavailable",
        )
    layer_query = select(ReferenceLayer).where(
        ReferenceLayer.provider_key == provider_key,
        ReferenceLayer.last_seen_snapshot_id == snapshot.id,
        ReferenceLayer.node_type == "layer",
        ReferenceLayer.status.in_(("active", "degraded")),
        ReferenceLayer.source_key == reviewed.catalog_layer_source_key,
    )
    if _lock_rows:
        layer_query = layer_query.with_for_update()
    layer = db.scalar(layer_query)
    if (
        layer is None
        or layer.remote_name != reviewed.catalog_remote_name
        or layer.service_id is None
    ):
        raise MetadataProbeSourceError(
            "current SIUR layer does not match reviewed SIGPAC evidence",
            code="catalog_identity_changed",
        )
    service_query = select(ReferenceService).where(
        ReferenceService.provider_key == provider_key,
        ReferenceService.last_seen_snapshot_id == snapshot.id,
        ReferenceService.id == layer.service_id,
        ReferenceService.status.in_(("active", "degraded")),
    )
    if _lock_rows:
        service_query = service_query.with_for_update()
    service = db.scalar(service_query)
    if (
        service is None
        or service.upstream_protocol.casefold() != "wms"
        or service.base_url != reviewed.catalog_endpoint_url
    ):
        raise MetadataProbeSourceError(
            "current SIUR service does not match reviewed SIGPAC evidence",
            code="catalog_identity_changed",
        )
    if not stored_catalog_snapshot_is_valid(
        snapshot
    ) or not catalog_snapshot_contains_active_layer(snapshot, layer):
        raise MetadataProbeSourceError(
            "current SIUR catalog evidence is invalid",
            code="catalog_integrity_invalid",
        )

    source_definition = metadata_probe_source_definition(probe_plan)
    source_definition_sha256 = canonical_json_sha256(source_definition)
    source_state = _metadata_probe_source_state()
    source_key = f"probe:{probe_plan.sha256[:32]}"
    sources_query = (
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.layer_id == layer.id,
        )
        .order_by(ReferenceLayerSource.id)
    )
    if _lock_rows:
        sources_query = sources_query.with_for_update()
    layer_sources = list(db.scalars(sources_query))
    probe_sources = [
        source
        for source in layer_sources
        if isinstance(source.config_json, dict)
        and source.config_json.get("schema") == PROBE_SOURCE_CONFIG_SCHEMA
    ]
    existing = next(
        (source for source in probe_sources if source.source_key == source_key),
        None,
    )
    if any(source is not existing for source in probe_sources):
        raise MetadataProbeSourceError(
            "a different metadata-probe source already exists for this layer",
            code="stale_probe_source_exists",
        )
    if existing is not None:
        _require_exact_existing_source(
            existing,
            definition=source_definition,
            definition_sha256=source_definition_sha256,
            source_state=source_state,
        )

    return MetadataProbeSourcePlan(
        provider_key=provider_key,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        audit_layer_id=audit_layer_id,
        layer_id=layer.id,
        layer_source_key=layer.source_key,
        source_key=source_key,
        source_definition=source_definition,
        source_definition_sha256=source_definition_sha256,
        source_state=source_state,
        probe_plan_sha256=probe_plan.sha256,
        action="already_exists" if existing is not None else "create",
        existing_source_id=existing.id if existing is not None else None,
    )


def apply_metadata_probe_source(
    db: Session,
    *,
    audit_layer_id: int,
    root_index: bytes,
    province_index: bytes,
    expected_plan_sha256: str,
    provider_key: str = "siur",
) -> tuple[MetadataProbeSourcePlan, ReferenceLayerSource]:
    """Append the reviewed exact row; never update or replace a source."""

    lock_catalog_provider(db, provider_key)
    # The caller may have planned with this same Session before waiting for
    # the catalog lock. Never let its identity map mask a committed catalog or
    # source change made while the operator reviewed the dry-run.
    db.expire_all()
    plan = plan_metadata_probe_source(
        db,
        audit_layer_id=audit_layer_id,
        root_index=root_index,
        province_index=province_index,
        provider_key=provider_key,
        _lock_rows=True,
    )
    if expected_plan_sha256 != plan.plan_sha256:
        raise MetadataProbeSourceError(
            "metadata-probe source plan changed after operator review",
            code="plan_changed",
        )
    if plan.existing_source_id is not None:
        existing = db.get(ReferenceLayerSource, plan.existing_source_id)
        if existing is None:
            raise MetadataProbeSourceError(
                "metadata-probe source disappeared during apply",
                code="source_changed",
            )
        db.refresh(existing, with_for_update=True)
        _require_exact_existing_source(
            existing,
            definition=plan.source_definition,
            definition_sha256=plan.source_definition_sha256,
            source_state=plan.source_state,
        )
        return plan, existing

    definition = plan.source_definition
    source_state = plan.source_state
    source = ReferenceLayerSource(
        provider_key=plan.provider_key,
        layer_id=plan.layer_id,
        source_key=plan.source_key,
        protocol=definition["protocol"],
        target_kind=definition["target_kind"],
        endpoint_url=definition["endpoint_url"],
        remote_name=definition["remote_name"],
        source_format=source_state["source_format"],
        sync_strategy=definition["sync_strategy"],
        config_json=definition["config"],
        definition_sha256=plan.source_definition_sha256,
        enabled=source_state["enabled"],
        is_primary=source_state["is_primary"],
        priority=definition["priority"],
        check_interval_seconds=source_state["check_interval_seconds"],
        full_refresh_interval_seconds=source_state["full_refresh_interval_seconds"],
    )
    try:
        with db.begin_nested():
            db.add(source)
            db.flush()
    except IntegrityError as error:
        raise MetadataProbeSourceError(
            "metadata-probe source collided with concurrent catalog state",
            code="source_conflict",
        ) from error
    return plan, source


def _reviewed_sigpac(audit_layer_id: int) -> ReviewedIDECyLExactSource:
    if isinstance(audit_layer_id, bool) or audit_layer_id not in {123, 166}:
        raise MetadataProbeSourceError(
            "audit-layer-id must identify SIGPAC 2024 or SIGPAC 2022",
            code="layer_invalid",
        )
    reviewed = next(
        (
            item
            for item in idecyl_exact_source_inventory()
            if item.audit_layer_id == audit_layer_id
        ),
        None,
    )
    if reviewed is None or reviewed.local_service_status != "restricted":
        raise MetadataProbeSourceError(
            "reviewed SIGPAC restriction evidence is unavailable",
            code="classification_changed",
        )
    return reviewed


def _require_exact_existing_source(
    source: ReferenceLayerSource,
    *,
    definition: dict[str, Any],
    definition_sha256: str,
    source_state: dict[str, Any],
) -> None:
    stored = {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": source.priority,
        "config": source.config_json,
    }
    stored_state = {
        "enabled": source.enabled,
        "is_primary": source.is_primary,
        "source_format": source.source_format,
        "check_interval_seconds": source.check_interval_seconds,
        "full_refresh_interval_seconds": source.full_refresh_interval_seconds,
    }
    if (
        stored_state != source_state
        or stored != definition
        or source.definition_sha256 != definition_sha256
    ):
        raise MetadataProbeSourceError(
            "stored metadata-probe source differs from the reviewed plan",
            code="source_changed",
        )


def _metadata_probe_source_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "is_primary": False,
        "source_format": None,
        "check_interval_seconds": 86_400,
        "full_refresh_interval_seconds": 2_592_000,
    }


def _read_index(path_value: str) -> bytes:
    path = Path(path_value).absolute()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise MetadataProbeSourceError(
                "metadata index is not a regular file",
                code="operator_input_invalid",
            )
        if not 1 <= opened.st_size <= MAX_LOCAL_INDEX_BYTES:
            raise MetadataProbeSourceError(
                "metadata index size is outside the accepted bound",
                code="operator_input_invalid",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            body = stream.read(MAX_LOCAL_INDEX_BYTES + 1)
            final = os.fstat(stream.fileno())
    except MetadataProbeSourceError:
        raise
    except OSError as error:
        raise MetadataProbeSourceError(
            "metadata index cannot be read",
            code="operator_input_invalid",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(body) != opened.st_size or (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    ) != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
        final.st_ctime_ns,
    ):
        raise MetadataProbeSourceError(
            "metadata index changed while it was read",
            code="operator_input_changed",
        )
    return body


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Plan or append one disabled, metadata-only SIGPAC probe source")
    )
    parser.add_argument("--audit-layer-id", type=int, required=True)
    parser.add_argument("--root-index", required=True)
    parser.add_argument("--province-index", required=True)
    parser.add_argument("--provider-key", default="siur")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.apply and not arguments.expected_plan_sha256:
            raise MetadataProbeSourceError(
                "--apply requires --expected-plan-sha256",
                code="operator_confirmation_missing",
            )
        if not arguments.apply and arguments.expected_plan_sha256 is not None:
            raise MetadataProbeSourceError(
                "--expected-plan-sha256 is only valid with --apply",
                code="operator_input_invalid",
            )
        root_index = _read_index(arguments.root_index)
        province_index = _read_index(arguments.province_index)
        register_all_models()
        with SessionLocal() as db:
            if arguments.apply:
                plan, source = apply_metadata_probe_source(
                    db,
                    audit_layer_id=arguments.audit_layer_id,
                    root_index=root_index,
                    province_index=province_index,
                    expected_plan_sha256=arguments.expected_plan_sha256,
                    provider_key=arguments.provider_key,
                )
                db.commit()
                result = plan.public_summary(applied=True)
                result["source_id"] = source.id
            else:
                plan = plan_metadata_probe_source(
                    db,
                    audit_layer_id=arguments.audit_layer_id,
                    root_index=root_index,
                    province_index=province_index,
                    provider_key=arguments.provider_key,
                )
                result = plan.public_summary(applied=False)
                result["source_id"] = plan.existing_source_id
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except MetadataProbeSourceError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": error.code,
                    "error_summary": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except Exception as error:  # noqa: BLE001  # pragma: no cover
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "metadata_probe_source_unavailable",
                    "error_summary": type(error).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = [
    "MetadataProbeSourceError",
    "MetadataProbeSourcePlan",
    "apply_metadata_probe_source",
    "main",
    "plan_metadata_probe_source",
]
