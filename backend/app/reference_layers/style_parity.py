"""Durable, fail-closed evidence for local SIUR style reproduction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSourceArtifact,
    ReferenceStyleParityPlan,
    ReferenceStyleParityPlanItem,
    ReferenceStyleParityPlanResource,
    ReferenceSyncRun,
)
from app.reference_layers.source_probes import SourceProbe


IMPLICIT_DEFAULT_STYLE_KEY = "__implicit_default__"


class StyleParityError(RuntimeError):
    """Style evidence is absent, partial, ambiguous, or internally invalid."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PersistedArtifactLike(Protocol):
    artifact_id: int
    artifact_kind: str
    roles: frozenset[str]
    media_type: str
    storage_backend: str
    storage_key: str
    size_bytes: int
    sha256: str
    metadata_json: dict[str, Any]


@dataclass(frozen=True)
class StyleParityPlanResult:
    plan_id: int
    complete: bool
    required_style_count: int
    missing_style_count: int
    missing_reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class _RequiredStyle:
    style_id: int | None
    source_key: str
    remote_name: str
    is_default: bool


@dataclass(frozen=True)
class _ResourceEvidence:
    artifact_id: int
    original_href: str
    resolved_url: str
    local_path: str
    media_type: str
    sha256: str

    def document(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "original_href": self.original_href,
            "resolved_url": self.resolved_url,
            "local_path": self.local_path,
            "media_type": self.media_type,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class _ItemEvidence:
    required: _RequiredStyle
    parity_kind: str
    verified: bool
    style_artifact_id: int | None
    package_artifact_id: int | None
    resources: tuple[_ResourceEvidence, ...]
    reason_code: str | None
    evidence: dict[str, Any]


def persist_style_parity_plan(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    catalog_snapshot_id: int,
    source_id: int,
    sync_run_id: int,
    delivery_kind: str,
    styles: Sequence[ReferenceLayerStyle],
    artifacts: Sequence[PersistedArtifactLike],
    probe: SourceProbe | None,
    now: datetime | None = None,
) -> StyleParityPlanResult:
    """Append the exact style decision for a run before materialization.

    Incomplete plans are committed by the caller and intentionally remain
    queryable after the worker rejects the run.
    """

    if delivery_kind not in {"vector", "raster", "tiles"}:
        raise StyleParityError(
            "style parity delivery kind is unsupported",
            code="style_parity_kind_invalid",
        )
    layer, snapshot, source, run = _validate_context(
        db,
        provider_key=provider_key,
        layer_id=layer_id,
        catalog_snapshot_id=catalog_snapshot_id,
        source_id=source_id,
        sync_run_id=sync_run_id,
        delivery_kind=delivery_kind,
    )
    required = _required_styles(styles)
    items = _evaluate_items(
        required,
        delivery_kind=delivery_kind,
        artifacts=artifacts,
        probe=probe,
    )
    missing_codes = tuple(
        sorted(
            {
                item.reason_code
                for item in items
                if item.reason_code is not None
            }
        )
    )
    evidence = {
        "schema_version": "reference-style-parity-plan/v1",
        "provider_key": provider_key,
        "layer_id": layer.id,
        "catalog_snapshot_id": snapshot.id,
        "catalog_definition_sha256": snapshot.definition_sha256,
        "source_id": source.id,
        "sync_run_id": run.id,
        "delivery_kind": delivery_kind,
        "required_style_count": len(items),
        "missing_style_count": sum(
            item.parity_kind == "missing" for item in items
        ),
        "items": [item.evidence for item in items],
    }
    evidence_sha256 = canonical_json_sha256(evidence)
    existing = db.scalar(
        select(ReferenceStyleParityPlan).where(
            ReferenceStyleParityPlan.source_id == source.id,
            ReferenceStyleParityPlan.sync_run_id == run.id,
        )
    )
    if existing is not None:
        if existing.evidence_sha256 != evidence_sha256:
            raise StyleParityError(
                "sync run already has different immutable style evidence",
                code="style_parity_plan_conflict",
            )
        return StyleParityPlanResult(
            plan_id=existing.id,
            complete=existing.complete,
            required_style_count=existing.required_style_count,
            missing_style_count=existing.missing_style_count,
            missing_reason_codes=missing_codes,
        )

    moment = _aware(now or datetime.now(timezone.utc))
    missing_count = sum(item.parity_kind == "missing" for item in items)
    plan = ReferenceStyleParityPlan(
        provider_key=provider_key,
        layer_id=layer.id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        source_id=source.id,
        sync_run_id=run.id,
        delivery_kind=delivery_kind,
        required_style_count=len(items),
        missing_style_count=missing_count,
        complete=missing_count == 0,
        evidence_json=evidence,
        evidence_sha256=evidence_sha256,
        created_at=moment,
    )
    db.add(plan)
    db.flush()
    for item in items:
        row = ReferenceStyleParityPlanItem(
            plan_id=plan.id,
            source_id=source.id,
            style_id=item.required.style_id,
            style_source_key=item.required.source_key,
            remote_name=item.required.remote_name,
            is_default=item.required.is_default,
            parity_kind=item.parity_kind,
            verified=item.verified,
            source_style_artifact_id=item.style_artifact_id,
            source_package_artifact_id=item.package_artifact_id,
            resource_count=len(item.resources),
            reason_code=item.reason_code,
            evidence_json=item.evidence,
            evidence_sha256=canonical_json_sha256(item.evidence),
            created_at=moment,
        )
        db.add(row)
        db.flush()
        for resource in item.resources:
            resource_evidence = resource.document()
            db.add(
                ReferenceStyleParityPlanResource(
                    plan_item_id=row.id,
                    source_id=source.id,
                    artifact_id=resource.artifact_id,
                    original_href=resource.original_href,
                    resolved_url=resource.resolved_url,
                    local_path=resource.local_path,
                    media_type=resource.media_type,
                    sha256=resource.sha256,
                    evidence_sha256=canonical_json_sha256(
                        resource_evidence
                    ),
                    created_at=moment,
                )
            )
    db.flush()
    return StyleParityPlanResult(
        plan_id=plan.id,
        complete=plan.complete,
        required_style_count=plan.required_style_count,
        missing_style_count=plan.missing_style_count,
        missing_reason_codes=missing_codes,
    )


def require_complete_style_parity(plan: StyleParityPlanResult) -> None:
    if not plan.complete:
        reasons = ", ".join(plan.missing_reason_codes) or "unknown"
        raise StyleParityError(
            f"local style parity is incomplete: {reasons}",
            code="style_parity_incomplete",
        )


def _validate_context(
    db: Session,
    *,
    provider_key: str,
    layer_id: int,
    catalog_snapshot_id: int,
    source_id: int,
    sync_run_id: int,
    delivery_kind: str,
) -> tuple[
    ReferenceLayer,
    ReferenceCatalogSnapshot,
    ReferenceLayerSource,
    ReferenceSyncRun,
]:
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.id == layer_id,
            ReferenceLayer.node_type == "layer",
            ReferenceLayer.status.in_(("active", "degraded")),
            ReferenceLayer.last_seen_snapshot_id == catalog_snapshot_id,
        )
    )
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.id == catalog_snapshot_id,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    source = db.scalar(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.id == source_id,
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.layer_id == layer_id,
            ReferenceLayerSource.target_kind == delivery_kind,
            ReferenceLayerSource.enabled.is_(True),
        )
    )
    run = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.id == sync_run_id,
            ReferenceSyncRun.source_id == source_id,
            ReferenceSyncRun.provider_key == provider_key,
            ReferenceSyncRun.layer_id == layer_id,
            ReferenceSyncRun.status == "running",
        )
    )
    if layer is None or snapshot is None or source is None or run is None:
        raise StyleParityError(
            "style parity context is no longer current",
            code="style_parity_context_invalid",
        )
    return layer, snapshot, source, run


def _required_styles(
    styles: Sequence[ReferenceLayerStyle],
) -> tuple[_RequiredStyle, ...]:
    active = tuple(
        style
        for style in styles
        if style.status in {"active", "degraded"}
    )
    if not active:
        return (
            _RequiredStyle(
                style_id=None,
                source_key=IMPLICIT_DEFAULT_STYLE_KEY,
                remote_name=IMPLICIT_DEFAULT_STYLE_KEY,
                is_default=True,
            ),
        )
    source_keys = [style.source_key for style in active]
    if (
        len(set(source_keys)) != len(source_keys)
        or sum(style.is_default for style in active) != 1
    ):
        raise StyleParityError(
            "catalog style identities or default are ambiguous",
            code="style_catalog_ambiguous",
        )
    return tuple(
        _RequiredStyle(
            style_id=style.id,
            source_key=style.source_key,
            remote_name=style.remote_name,
            is_default=style.is_default,
        )
        for style in active
    )


def _evaluate_items(
    required: tuple[_RequiredStyle, ...],
    *,
    delivery_kind: str,
    artifacts: Sequence[PersistedArtifactLike],
    probe: SourceProbe | None,
) -> tuple[_ItemEvidence, ...]:
    if delivery_kind == "tiles":
        advertised = _advertised_tile_styles(probe)
        descriptor_verified = sum(
            artifact.artifact_kind == "metadata"
            and "input" in artifact.roles
            and artifact.metadata_json.get("schema")
            == "reference-tile-source/v1"
            for artifact in artifacts
        ) == 1
        return tuple(
            _baked_item(
                item,
                advertised=advertised,
                probe_available=bool(
                    (probe is not None and probe.available)
                    or descriptor_verified
                ),
            )
            for item in required
        )
    return _sld_items(required, artifacts)


def _baked_item(
    required: _RequiredStyle,
    *,
    advertised: set[str] | None,
    probe_available: bool,
) -> _ItemEvidence:
    implicit = required.style_id is None
    verified = implicit and probe_available
    if not implicit:
        verified = advertised is not None and required.remote_name in advertised
    parity_kind = "baked" if verified else "missing"
    reason = None if verified else "tile_style_not_advertised"
    evidence = {
        "schema_version": "reference-style-parity-item/v1",
        "style_id": required.style_id,
        "style_source_key": required.source_key,
        "remote_name": required.remote_name,
        "is_default": required.is_default,
        "parity_kind": parity_kind,
        "verified": verified,
        "verification": (
            "capabilities_and_baked_archive"
            if verified
            else "missing_capabilities_style"
        ),
        "reason_code": reason,
        "style_artifact_id": None,
        "package_artifact_id": None,
        "resources": [],
    }
    return _ItemEvidence(
        required=required,
        parity_kind=parity_kind,
        verified=verified,
        style_artifact_id=None,
        package_artifact_id=None,
        resources=(),
        reason_code=reason,
        evidence=evidence,
    )


def _advertised_tile_styles(probe: SourceProbe | None) -> set[str] | None:
    if probe is None or not probe.available:
        return None
    raw = probe.metadata.get("styles")
    if not isinstance(raw, list):
        return None
    if probe.protocol == "wmts":
        names = [
            item.get("name")
            for item in raw
            if isinstance(item, Mapping)
        ]
    else:
        names = raw
    if len(names) != len(raw) or any(
        not isinstance(item, str) for item in names
    ):
        return None
    return set(names)


def _sld_items(
    required: tuple[_RequiredStyle, ...],
    artifacts: Sequence[PersistedArtifactLike],
) -> tuple[_ItemEvidence, ...]:
    style_artifacts = _artifacts_by_style_key(
        artifacts,
        artifact_kind="style",
        role="style",
    )
    package_artifacts = _artifacts_by_style_key(
        artifacts,
        artifact_kind="style_package",
        role="style_package",
    )
    resources_by_sha = {
        artifact.sha256: artifact
        for artifact in artifacts
        if artifact.artifact_kind == "style_resource"
        and "style_resource" in artifact.roles
    }
    if len(resources_by_sha) != sum(
        artifact.artifact_kind == "style_resource"
        and "style_resource" in artifact.roles
        for artifact in artifacts
    ):
        raise StyleParityError(
            "style resource identities are ambiguous",
            code="style_resource_ambiguous",
        )
    return tuple(
        _sld_item(
            item,
            style_artifacts=style_artifacts,
            package_artifacts=package_artifacts,
            resources_by_sha=resources_by_sha,
        )
        for item in required
    )


def _artifacts_by_style_key(
    artifacts: Sequence[PersistedArtifactLike],
    *,
    artifact_kind: str,
    role: str,
) -> dict[str, PersistedArtifactLike]:
    result: dict[str, PersistedArtifactLike] = {}
    for artifact in artifacts:
        if artifact.artifact_kind != artifact_kind or role not in artifact.roles:
            continue
        source_key = artifact.metadata_json.get(
            "catalog_style_source_key"
        )
        if not isinstance(source_key, str) or source_key in result:
            raise StyleParityError(
                "style artifact identities are ambiguous",
                code="style_artifact_ambiguous",
            )
        result[source_key] = artifact
    return result


def _sld_item(
    required: _RequiredStyle,
    *,
    style_artifacts: Mapping[str, PersistedArtifactLike],
    package_artifacts: Mapping[str, PersistedArtifactLike],
    resources_by_sha: Mapping[str, PersistedArtifactLike],
) -> _ItemEvidence:
    style = style_artifacts.get(required.source_key)
    package = package_artifacts.get(required.source_key)
    reason: str | None = None
    resources: tuple[_ResourceEvidence, ...] = ()
    parity_kind = "missing"
    verified = False
    if required.style_id is None:
        reason = "implicit_default_sld_missing"
    elif style is None:
        reason = "style_artifact_missing"
    else:
        raw_kind = style.metadata_json.get("parity_kind")
        unresolved = style.metadata_json.get("unresolved_resources", [])
        bindings = style.metadata_json.get("resource_bindings", [])
        if raw_kind == "missing" or unresolved:
            reason = _unresolved_reason(unresolved)
        elif raw_kind == "exact" and not bindings and package is None:
            parity_kind = "exact"
            verified = True
        elif raw_kind == "adapted" and package is not None:
            resources = _resource_evidence(bindings, resources_by_sha)
            if (
                resources
                and package.metadata_json.get("sld_sha256") == style.sha256
            ):
                parity_kind = "adapted"
                verified = True
            else:
                resources = ()
                reason = "style_resource_evidence_invalid"
        else:
            reason = "style_artifact_evidence_invalid"
    evidence = {
        "schema_version": "reference-style-parity-item/v1",
        "style_id": required.style_id,
        "style_source_key": required.source_key,
        "remote_name": required.remote_name,
        "is_default": required.is_default,
        "parity_kind": parity_kind,
        "verified": verified,
        "reason_code": reason,
        "style_artifact_id": style.artifact_id if verified and style else None,
        "package_artifact_id": (
            package.artifact_id
            if verified and parity_kind == "adapted" and package
            else None
        ),
        "resources": [resource.document() for resource in resources],
    }
    return _ItemEvidence(
        required=required,
        parity_kind=parity_kind,
        verified=verified,
        style_artifact_id=(
            style.artifact_id if verified and style is not None else None
        ),
        package_artifact_id=(
            package.artifact_id
            if verified and parity_kind == "adapted" and package is not None
            else None
        ),
        resources=resources,
        reason_code=reason,
        evidence=evidence,
    )


def _unresolved_reason(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                reason = item.get("reason_code")
                if isinstance(reason, str) and reason:
                    return reason[:64]
    return "style_resource_missing"


def _resource_evidence(
    raw_bindings: Any,
    resources_by_sha: Mapping[str, PersistedArtifactLike],
) -> tuple[_ResourceEvidence, ...]:
    if not isinstance(raw_bindings, list) or not raw_bindings:
        return ()
    result: list[_ResourceEvidence] = []
    hrefs: set[str] = set()
    for binding in raw_bindings:
        if not isinstance(binding, Mapping):
            return ()
        original_href = binding.get("original_href")
        resolved_url = binding.get("resolved_url")
        local_path = binding.get("local_path")
        sha256 = binding.get("sha256")
        if (
            not isinstance(original_href, str)
            or not original_href
            or original_href in hrefs
            or not isinstance(resolved_url, str)
            or not resolved_url.startswith("https://")
            or not isinstance(local_path, str)
            or not isinstance(sha256, str)
        ):
            return ()
        artifact = resources_by_sha.get(sha256)
        if (
            artifact is None
            or local_path
            != (
                f"resources/{artifact.sha256}."
                f"{artifact.metadata_json.get('extension')}"
            )
        ):
            return ()
        hrefs.add(original_href)
        result.append(
            _ResourceEvidence(
                artifact_id=artifact.artifact_id,
                original_href=original_href,
                resolved_url=resolved_url,
                local_path=local_path,
                media_type=artifact.media_type,
                sha256=artifact.sha256,
            )
        )
    return tuple(result)


def canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise StyleParityError(
            "style parity evidence is not canonical JSON",
            code="style_parity_evidence_invalid",
        ) from error
    if len(encoded) > 4 * 1024 * 1024:
        raise StyleParityError(
            "style parity evidence is oversized",
            code="style_parity_evidence_oversized",
        )
    return hashlib.sha256(encoded).hexdigest()


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise StyleParityError(
            "style parity timestamp is timezone-naive",
            code="style_parity_timestamp_invalid",
        )
    return value.astimezone(timezone.utc)
