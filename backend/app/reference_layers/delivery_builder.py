"""Create immutable delivery versions from already validated local assets.

Acquisition, GDAL and GeoServer work happens outside a database transaction.
This module is the short transactional bridge between those physical results
and the promotion lifecycle.  It revalidates the worker lease and every
cross-table identity so an obsolete worker cannot attach assets to a new run.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import re
from typing import Any, Protocol

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.reference_layers.mirror_lifecycle import (
    MirrorLeaseLostError,
    MirrorLifecycleError,
    SyncRunLease,
    stored_source_definition_is_valid,
    sync_run_source_definition_is_valid,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_bound_sync_run_authorization,
)
from app.reference_layers.local_metadata_contract import (
    LOCAL_METADATA_ASSET_KEY,
    LOCAL_METADATA_ASSET_SCHEMA,
    LOCAL_METADATA_BINDING_KEYS,
    LOCAL_METADATA_DESCRIPTOR_KEYS,
    LOCAL_METADATA_DOCUMENT_SCHEMA,
    local_metadata_asset_descriptor,
    local_metadata_binding,
    local_metadata_precommit_gate,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryStyleParity,
    ReferenceDeliveryStyleResource,
    ReferenceDeliveryVersion,
    ReferenceDeliveryVersionArtifact,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceSourceArtifact,
    ReferenceStyleParityPlan,
    ReferenceStyleParityPlanItem,
    ReferenceStyleParityPlanResource,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.style_parity import (
    IMPLICIT_DEFAULT_STYLE_KEY,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_CRS_RE = re.compile(r"^EPSG:[1-9][0-9]{2,6}$", re.ASCII)
_ASSET_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$", re.ASCII)
_RESOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$", re.ASCII)
_POSTGIS_KEY_RE = re.compile(
    r"^reference_data\.[a-z][a-z0-9_]{0,62}$",
    re.ASCII,
)
_BLOB_KEY_RE = re.compile(
    r"^blobs/sha256/(?P<prefix>[0-9a-f]{2})/(?P<sha>[0-9a-f]{64})$",
    re.ASCII,
)
_LOCK_DOMAIN = b"asistente/reference-mirror-layer/v1\0"
_MAX_SOURCE_VERSION_LENGTH = 2_048
_VALIDATION_SCHEMA = "reference-delivery-validation/v1"
_CONTINUITY_SCHEMA = "reference-delivery-continuity/v1"
_MIN_FEATURE_BASELINE = 100
_MIN_FEATURE_RETAINED_RATIO = 0.10
_MAX_FEATURE_GROWTH_RATIO = 10.0
_FEATURE_GROWTH_ALLOWANCE = 1_000
_MIN_BOUNDS_OVERLAP_RATIO = 0.80
_MIN_BOUNDS_AREA_RATIO = 0.50
_MAX_BOUNDS_AREA_RATIO = 2.00
_DATA_SCHEMA_BY_KIND = {
    "vector": "reference-vector-schema/v1",
    "raster": "reference-raster-schema/v1",
    "tiles": "reference-tiles-schema/v1",
}


class DeliveryBuildError(MirrorLifecycleError):
    """A prepared delivery cannot safely become an immutable version."""


class DeliveryContinuityError(DeliveryBuildError):
    """A candidate is structurally incompatible with the active version."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        canonical_json_sha256(evidence)
        failed = evidence.get("failed_checks")
        labels = (
            ", ".join(failed)
            if isinstance(failed, list)
            and all(isinstance(item, str) for item in failed)
            else "unknown"
        )
        super().__init__(
            f"delivery continuity check rejected: {labels}"
        )
        self.evidence = evidence


@dataclass(frozen=True)
class PreparedDeliveryAsset:
    asset_key: str
    asset_kind: str
    is_primary: bool
    storage_backend: str
    storage_key: str
    media_type: str
    sha256: str
    size_bytes: int | None
    metadata_json: dict[str, Any]


@dataclass(frozen=True)
class PreparedDelivery:
    delivery_kind: str
    source_version: str | None
    content_sha256: str
    reference_at: datetime | None
    crs: str
    bounds_json: dict[str, Any]
    feature_count: int | None
    validation_json: dict[str, Any]
    input_artifact_ids: tuple[int, ...]
    assets: tuple[PreparedDeliveryAsset, ...]
    supporting_artifacts: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class LocalMetadataPrecommitContext:
    db: Session
    prepared: PreparedDelivery
    source: ReferenceLayerSource
    run: ReferenceSyncRun
    layer: ReferenceLayer
    snapshot: ReferenceCatalogSnapshot
    authorization: ReferenceMirrorAuthorizationReview
    plan: ReferenceStyleParityPlan
    plan_items: tuple[ReferenceStyleParityPlanItem, ...]
    artifacts: tuple[ReferenceSourceArtifact, ...]
    artifact_links: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class LocalMetadataVerification:
    document_sha256: str
    document_size_bytes: int


class LocalMetadataPrecommitVerifier(Protocol):
    def __call__(
        self,
        context: LocalMetadataPrecommitContext,
    ) -> LocalMetadataVerification: ...


@dataclass(frozen=True)
class BuiltDeliveryVersion:
    version_id: int
    sequence_number: int
    manifest_sha256: str
    validation_sha256: str


def create_delivery_version(
    db: Session,
    *,
    lease: SyncRunLease,
    prepared: PreparedDelivery,
    metadata_verifier: LocalMetadataPrecommitVerifier,
    now: datetime | None = None,
) -> BuiltDeliveryVersion:
    """Persist one immutable, unpromoted version for a live leased run."""

    normalized = _validate_prepared(prepared)
    if not callable(metadata_verifier):
        raise DeliveryBuildError(
            "local metadata precommit verifier is required"
        )
    try:
        preliminary_source = db.get(ReferenceLayerSource, lease.source_id)
        if preliminary_source is None:
            raise MirrorLeaseLostError("sync-run lease is absent or expired")
        _lock_layer(
            db,
            preliminary_source.provider_key,
            preliminary_source.layer_id,
        )
        source = db.scalar(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.id == lease.source_id)
            .with_for_update()
        )
        run = db.scalar(
            select(ReferenceSyncRun)
            .where(
                ReferenceSyncRun.id == lease.run_id,
                ReferenceSyncRun.source_id == lease.source_id,
                ReferenceSyncRun.attempt_no == lease.attempt_no,
            )
            .with_for_update()
        )
        state = db.scalar(
            select(ReferenceLayerDeliveryState)
            .where(
                ReferenceLayerDeliveryState.provider_key
                == preliminary_source.provider_key,
                ReferenceLayerDeliveryState.layer_id
                == preliminary_source.layer_id,
            )
            .with_for_update()
        )
        moment = _fresh_lease_moment(db, now)
        if (
            run is None
            or run.status != "running"
            or run.lease_token != lease.token
            or run.lease_expires_at is None
            or run.lease_expires_at <= moment
        ):
            raise MirrorLeaseLostError("sync-run lease is absent or expired")
        if source is None or (
            source.provider_key != preliminary_source.provider_key
            or source.layer_id != preliminary_source.layer_id
        ):
            raise DeliveryBuildError("delivery source identity changed")
        if state is not None and state.status == "disabled":
            raise DeliveryBuildError(
                "delivery layer is administratively disabled"
            )
        if db.scalar(
            select(ReferenceDeliveryVersion.id).where(
                ReferenceDeliveryVersion.sync_run_id == run.id
            )
        ) is not None:
            raise DeliveryBuildError("sync run already has a delivery version")

        if not source.enabled:
            raise DeliveryBuildError("delivery source is absent or disabled")
        if not stored_source_definition_is_valid(source):
            raise DeliveryBuildError("current delivery source definition is invalid")
        if not sync_run_source_definition_is_valid(run):
            raise DeliveryBuildError("sync-run source definition is invalid")
        if source.definition_sha256 != run.source_definition_sha256:
            raise DeliveryBuildError("delivery source changed during the run")
        try:
            authorization = require_bound_sync_run_authorization(
                db,
                run=run,
                source=source,
            )
        except MirrorAuthorizationError as error:
            raise DeliveryBuildError(
                f"mirror authorization rejected version creation: {error.code}"
            ) from error
        if source.target_kind != prepared.delivery_kind:
            raise DeliveryBuildError("prepared delivery kind does not match source")
        active_generation = state.generation if state is not None else 0
        if run.expected_active_generation != active_generation:
            raise DeliveryBuildError(
                "active delivery generation changed during version creation"
            )
        layer = db.scalar(
            select(ReferenceLayer).where(
                ReferenceLayer.id == source.layer_id,
                ReferenceLayer.provider_key == source.provider_key,
                ReferenceLayer.node_type == "layer",
                ReferenceLayer.status.in_(("active", "degraded")),
            )
        )
        if layer is None:
            raise DeliveryBuildError("delivery layer is not current and renderable")
        snapshot = db.scalar(
            select(ReferenceCatalogSnapshot).where(
                ReferenceCatalogSnapshot.id == layer.last_seen_snapshot_id,
                ReferenceCatalogSnapshot.provider_key == source.provider_key,
                ReferenceCatalogSnapshot.is_current.is_(True),
                ReferenceCatalogSnapshot.status == "applied",
            )
        )
        if snapshot is None:
            raise DeliveryBuildError("current catalog snapshot is unavailable")

        artifact_links = {
            (artifact_id, "input")
            for artifact_id in prepared.input_artifact_ids
        }
        artifact_links.update(prepared.supporting_artifacts)
        run_links = set(
            db.execute(
                select(
                    ReferenceSyncRunArtifact.artifact_id,
                    ReferenceSyncRunArtifact.role,
                ).where(
                    ReferenceSyncRunArtifact.source_id == source.id,
                    ReferenceSyncRunArtifact.run_id == run.id,
                )
            )
        )
        if not artifact_links <= run_links:
            raise DeliveryBuildError(
                "prepared provenance is not immutable artifacts of the leased run"
            )
        artifact_ids = {artifact_id for artifact_id, _role in artifact_links}
        artifact_rows = list(
            db.scalars(
                select(ReferenceSourceArtifact).where(
                    ReferenceSourceArtifact.source_id == source.id,
                    ReferenceSourceArtifact.id.in_(artifact_ids),
                )
            )
        )
        if {item.id for item in artifact_rows} != artifact_ids:
            raise DeliveryBuildError(
                "prepared provenance artifact identity is invalid"
            )
        plan, plan_items = require_complete_delivery_style_plan(
            db,
            source=source,
            run=run,
            snapshot=snapshot,
            delivery_kind=prepared.delivery_kind,
            artifact_links=artifact_links,
        )
        _validate_local_metadata_asset_binding(
            prepared,
            source=source,
            run=run,
            snapshot=snapshot,
            authorization=authorization,
            prepared_validation_sha256=normalized[
                "validation_sha256"
            ],
        )

        active_version = _active_delivery_version(
            db,
            state=state,
            provider_key=source.provider_key,
            layer_id=source.layer_id,
        )
        continuity = evaluate_delivery_continuity(
            active_version=active_version,
            candidate=prepared,
        )
        if not continuity["passed"]:
            raise DeliveryContinuityError(continuity)
        verification = metadata_verifier(
            LocalMetadataPrecommitContext(
                db=db,
                prepared=prepared,
                source=source,
                run=run,
                layer=layer,
                snapshot=snapshot,
                authorization=authorization,
                plan=plan,
                plan_items=plan_items,
                artifacts=tuple(
                    sorted(
                        artifact_rows,
                        key=lambda item: item.id,
                    )
                ),
                artifact_links=tuple(
                    sorted(
                        artifact_links,
                        key=lambda item: (item[1], item[0]),
                    )
                ),
            )
        )
        metadata_asset = next(
            asset
            for asset in prepared.assets
            if asset.asset_kind == "metadata"
        )
        if (
            not isinstance(
                verification,
                LocalMetadataVerification,
            )
            or verification.document_sha256
            != metadata_asset.sha256
            or verification.document_size_bytes
            != metadata_asset.size_bytes
        ):
            raise DeliveryBuildError(
                "local metadata precommit verification is invalid"
            )
        if _validate_prepared(prepared) != normalized:
            raise DeliveryBuildError(
                "prepared delivery changed during metadata verification"
            )
        _validate_local_metadata_asset_binding(
            prepared,
            source=source,
            run=run,
            snapshot=snapshot,
            authorization=authorization,
            prepared_validation_sha256=normalized[
                "validation_sha256"
            ],
        )
        stored_validation = deepcopy(prepared.validation_json)
        stored_validation["continuity_gate"] = continuity
        stored_validation["style_parity_gate"] = {
            "schema_version": "reference-delivery-style-parity-gate/v1",
            "passed": True,
            "plan_id": plan.id,
            "plan_evidence_sha256": plan.evidence_sha256,
            "required_style_count": plan.required_style_count,
            "verified_style_count": len(plan_items),
            "missing_style_count": 0,
        }
        metadata_binding = metadata_asset.metadata_json["binding"]
        stored_validation["local_metadata_gate"] = (
            local_metadata_precommit_gate(
                document_sha256=metadata_asset.sha256,
                document_size_bytes=(
                    verification.document_size_bytes
                ),
                descriptor=metadata_asset.metadata_json,
                binding=metadata_binding,
            )
        )
        validation_sha256 = canonical_json_sha256(stored_validation)
        normalized["validation_sha256"] = validation_sha256

        sequence_number = int(
            db.scalar(
                select(func.coalesce(func.max(ReferenceDeliveryVersion.sequence_number), 0))
                .where(
                    ReferenceDeliveryVersion.provider_key == source.provider_key,
                    ReferenceDeliveryVersion.layer_id == source.layer_id,
                )
            )
            or 0
        ) + 1
        manifest = {
            "schema_version": "reference-delivery-manifest-v1",
            "provider_key": source.provider_key,
            "layer_id": source.layer_id,
            "source_id": source.id,
            "run_id": run.id,
            "catalog_snapshot_id": snapshot.id,
            "delivery": normalized,
            "inputs": [
                {
                    "artifact_id": item.id,
                    "artifact_kind": item.artifact_kind,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in sorted(artifact_rows, key=lambda value: value.id)
                if (item.id, "input") in artifact_links
            ],
            "supporting_artifacts": [
                {
                    "artifact_id": item.id,
                    "artifact_kind": item.artifact_kind,
                    "role": role,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in sorted(artifact_rows, key=lambda value: value.id)
                for artifact_id, role in sorted(artifact_links)
                if artifact_id == item.id and role != "input"
            ],
            "style_parity_plan": {
                "plan_id": plan.id,
                "evidence_sha256": plan.evidence_sha256,
            },
            "mirror_authorization": {
                "review_id": authorization.id,
                "review_sha256": authorization.review_sha256,
            },
        }
        manifest_sha256 = canonical_json_sha256(manifest)
        version = ReferenceDeliveryVersion(
            provider_key=source.provider_key,
            layer_id=source.layer_id,
            source_id=source.id,
            sync_run_id=run.id,
            catalog_snapshot_id=snapshot.id,
            catalog_definition_sha256=snapshot.definition_sha256,
            mirror_authorization_review_id=authorization.id,
            mirror_authorization_review_sha256=(
                authorization.review_sha256
            ),
            sequence_number=sequence_number,
            delivery_kind=prepared.delivery_kind,
            source_version=prepared.source_version,
            content_sha256=prepared.content_sha256,
            manifest_sha256=manifest_sha256,
            validation_sha256=validation_sha256,
            reference_at=prepared.reference_at,
            crs=prepared.crs,
            bounds_json=prepared.bounds_json,
            feature_count=prepared.feature_count,
            validation_json=stored_validation,
            created_at=moment,
        )
        db.add(version)
        db.flush()
        db.add_all(
            [
                ReferenceDeliveryVersionArtifact(
                    source_id=source.id,
                    version_id=version.id,
                    artifact_id=artifact_id,
                    role=role,
                )
                for artifact_id, role in sorted(artifact_links)
            ]
        )
        delivery_assets = [
                ReferenceDeliveryAsset(
                    version_id=version.id,
                    asset_key=asset.asset_key,
                    asset_kind=asset.asset_kind,
                    is_primary=asset.is_primary,
                    storage_backend=asset.storage_backend,
                    storage_key=asset.storage_key,
                    media_type=asset.media_type,
                    sha256=asset.sha256,
                    size_bytes=asset.size_bytes,
                    metadata_json=asset.metadata_json,
                    created_at=moment,
                )
                for asset in prepared.assets
        ]
        db.add_all(delivery_assets)
        db.flush()
        _append_delivery_style_parity(
            db,
            version=version,
            plan=plan,
            items=plan_items,
            assets=delivery_assets,
            now=moment,
        )
        db.commit()
        return BuiltDeliveryVersion(
            version_id=version.id,
            sequence_number=sequence_number,
            manifest_sha256=manifest_sha256,
            validation_sha256=validation_sha256,
        )
    except Exception:
        db.rollback()
        raise


def require_complete_delivery_style_plan(
    db: Session,
    *,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
    snapshot: ReferenceCatalogSnapshot,
    delivery_kind: str,
    artifact_links: set[tuple[int, str]],
) -> tuple[
    ReferenceStyleParityPlan,
    tuple[ReferenceStyleParityPlanItem, ...],
]:
    plan = db.scalar(
        select(ReferenceStyleParityPlan).where(
            ReferenceStyleParityPlan.source_id == source.id,
            ReferenceStyleParityPlan.sync_run_id == run.id,
        )
    )
    if (
        plan is None
        or plan.provider_key != source.provider_key
        or plan.layer_id != source.layer_id
        or plan.catalog_snapshot_id != snapshot.id
        or plan.catalog_definition_sha256 != snapshot.definition_sha256
        or plan.delivery_kind != delivery_kind
        or not plan.complete
        or plan.missing_style_count != 0
        or canonical_json_sha256(plan.evidence_json)
        != plan.evidence_sha256
    ):
        raise DeliveryBuildError(
            "delivery has no complete immutable style parity plan"
        )
    items = tuple(
        db.scalars(
            select(ReferenceStyleParityPlanItem)
            .where(ReferenceStyleParityPlanItem.plan_id == plan.id)
            .order_by(ReferenceStyleParityPlanItem.id)
        )
    )
    if (
        len(items) != plan.required_style_count
        or any(
            item.parity_kind == "missing"
            or not item.verified
            or canonical_json_sha256(item.evidence_json)
            != item.evidence_sha256
            for item in items
        )
    ):
        raise DeliveryBuildError(
            "style parity plan is partial or unverified"
        )
    resources = tuple(
        db.scalars(
            select(ReferenceStyleParityPlanResource)
            .join(
                ReferenceStyleParityPlanItem,
                ReferenceStyleParityPlanItem.id
                == ReferenceStyleParityPlanResource.plan_item_id,
            )
            .where(ReferenceStyleParityPlanItem.plan_id == plan.id)
            .order_by(ReferenceStyleParityPlanResource.id)
        )
    )
    resources_by_item: dict[int, list[ReferenceStyleParityPlanResource]] = {}
    for resource in resources:
        resources_by_item.setdefault(resource.plan_item_id, []).append(
            resource
        )
    required_links: set[tuple[int, str]] = set()
    for item in items:
        if item.source_style_artifact_id is not None:
            required_links.add((item.source_style_artifact_id, "style"))
        if item.source_package_artifact_id is not None:
            required_links.add(
                (item.source_package_artifact_id, "style_package")
            )
        item_resources = resources_by_item.get(item.id, [])
        if len(item_resources) != item.resource_count:
            raise DeliveryBuildError(
                "style parity resource count is inconsistent"
            )
        for resource in item_resources:
            required_links.add((resource.artifact_id, "style_resource"))
            resource_evidence = {
                "artifact_id": resource.artifact_id,
                "original_href": resource.original_href,
                "resolved_url": resource.resolved_url,
                "local_path": resource.local_path,
                "media_type": resource.media_type,
                "sha256": resource.sha256,
            }
            if (
                canonical_json_sha256(resource_evidence)
                != resource.evidence_sha256
            ):
                raise DeliveryBuildError(
                    "style parity resource evidence hash is invalid"
                )
    if not required_links <= artifact_links:
        raise DeliveryBuildError(
            "delivery omits style or resource provenance"
        )
    return plan, items


def _append_delivery_style_parity(
    db: Session,
    *,
    version: ReferenceDeliveryVersion,
    plan: ReferenceStyleParityPlan,
    items: tuple[ReferenceStyleParityPlanItem, ...],
    assets: list[ReferenceDeliveryAsset],
    now: datetime,
) -> None:
    for item in items:
        expected_asset_kind = {
            "exact": "style_sld",
            "adapted": "style_package",
            "baked": "tile_archive",
        }.get(item.parity_kind)
        if expected_asset_kind is None:
            raise DeliveryBuildError(
                "missing style parity cannot be delivered"
            )
        candidates = [
            asset
            for asset in assets
            if asset.asset_kind == expected_asset_kind
            and _asset_style_source_key(asset)
            == item.style_source_key
            and (
                expected_asset_kind == "tile_archive"
                or asset.metadata_json.get("effective") is True
            )
        ]
        if len(candidates) != 1:
            raise DeliveryBuildError(
                "delivery asset coverage differs from its style parity plan"
            )
        asset = candidates[0]
        resources = tuple(
            db.scalars(
                select(ReferenceStyleParityPlanResource)
                .where(
                    ReferenceStyleParityPlanResource.plan_item_id == item.id
                )
                .order_by(ReferenceStyleParityPlanResource.id)
            )
        )
        if len(resources) != item.resource_count:
            raise DeliveryBuildError(
                "delivery style resources are partial"
            )
        evidence = {
            "schema_version": "reference-delivery-style-parity/v1",
            "version_id": version.id,
            "plan_id": plan.id,
            "plan_item_id": item.id,
            "style_source_key": item.style_source_key,
            "parity_kind": item.parity_kind,
            "verified": True,
            "delivery_asset_id": asset.id,
            "delivery_asset_sha256": asset.sha256,
            "plan_item_evidence_sha256": item.evidence_sha256,
            "resource_evidence_sha256": [
                resource.evidence_sha256 for resource in resources
            ],
        }
        parity = ReferenceDeliveryStyleParity(
            version_id=version.id,
            plan_item_id=item.id,
            parity_kind=item.parity_kind,
            verified=True,
            delivery_asset_id=asset.id,
            resource_count=len(resources),
            evidence_json=evidence,
            evidence_sha256=canonical_json_sha256(evidence),
            created_at=now,
        )
        db.add(parity)
        db.flush()
        db.add_all(
            [
                ReferenceDeliveryStyleResource(
                    delivery_parity_id=parity.id,
                    plan_resource_id=resource.id,
                    created_at=now,
                )
                for resource in resources
            ]
        )
    db.flush()


def _asset_style_source_key(asset: ReferenceDeliveryAsset) -> str | None:
    value = asset.metadata_json.get("catalog_style_source_key")
    if value is None and asset.asset_kind == "tile_archive":
        return IMPLICIT_DEFAULT_STYLE_KEY
    return value if isinstance(value, str) else None


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError, RecursionError) as error:
        raise DeliveryBuildError("delivery metadata is not canonical JSON") from error
    if len(encoded) > 4 * 1024 * 1024:
        raise DeliveryBuildError("delivery metadata is too large")
    return encoded


def canonical_json_sha256(value: Any) -> str:
    encoded = canonical_json_bytes(value)
    return hashlib.sha256(encoded).hexdigest()


def evaluate_delivery_continuity(
    *,
    active_version: ReferenceDeliveryVersion | None,
    candidate: PreparedDelivery,
) -> dict[str, Any]:
    """Build the canonical, auditable active-to-candidate safety report."""

    candidate_bounds = _bounds(candidate.bounds_json)
    candidate_schema_sha256 = _semantic_schema_sha256(
        candidate.delivery_kind,
        candidate.validation_json,
    )
    candidate_snapshot = {
        "delivery_kind": candidate.delivery_kind,
        "crs": candidate.crs,
        "bounds": candidate_bounds,
        "feature_count": candidate.feature_count,
        "data_schema_sha256": candidate_schema_sha256,
    }
    if active_version is None:
        checks = {
            name: {
                "passed": True,
                "status": "no_active_baseline",
            }
            for name in (
                "delivery_kind",
                "crs",
                "bounds",
                "data_schema",
                "feature_count",
            )
        }
        return {
            "schema_version": _CONTINUITY_SCHEMA,
            "passed": True,
            "baseline_version_id": None,
            "candidate": candidate_snapshot,
            "checks": checks,
            "failed_checks": [],
        }

    active_snapshot, baseline_error = _active_continuity_snapshot(
        active_version
    )
    if baseline_error is not None:
        checks = {
            "active_baseline": {
                "passed": False,
                "reason": baseline_error,
            }
        }
    else:
        assert active_snapshot is not None
        kind_matches = (
            active_snapshot["delivery_kind"]
            == candidate_snapshot["delivery_kind"]
        )
        crs_matches = active_snapshot["crs"] == candidate_snapshot["crs"]
        bounds_check = _bounds_continuity_check(
            delivery_kind=candidate.delivery_kind,
            active=active_snapshot["bounds"],
            candidate=candidate_snapshot["bounds"],
        )
        schema_matches = (
            active_snapshot["data_schema_sha256"]
            == candidate_snapshot["data_schema_sha256"]
        )
        active_count = active_snapshot["feature_count"]
        candidate_count = candidate_snapshot["feature_count"]
        feature_applies = candidate.delivery_kind == "vector"
        retained_ratio = (
            round(candidate_count / active_count, 12)
            if feature_applies
            and isinstance(active_count, int)
            and active_count > 0
            and isinstance(candidate_count, int)
            else None
        )
        minimum_feature_count = (
            1
            if feature_applies
            and isinstance(active_count, int)
            and active_count < _MIN_FEATURE_BASELINE
            else (
                active_count * _MIN_FEATURE_RETAINED_RATIO
                if feature_applies and isinstance(active_count, int)
                else None
            )
        )
        maximum_feature_count = (
            max(
                active_count * _MAX_FEATURE_GROWTH_RATIO,
                active_count + _FEATURE_GROWTH_ALLOWANCE,
            )
            if feature_applies and isinstance(active_count, int)
            else None
        )
        feature_matches = bool(
            not feature_applies
            or (
                isinstance(active_count, int)
                and isinstance(candidate_count, int)
                and minimum_feature_count is not None
                and maximum_feature_count is not None
                and candidate_count >= minimum_feature_count
                and candidate_count <= maximum_feature_count
            )
        )
        checks = {
            "delivery_kind": {
                "passed": kind_matches,
                "active": active_snapshot["delivery_kind"],
                "candidate": candidate_snapshot["delivery_kind"],
            },
            "crs": {
                "passed": crs_matches,
                "active": active_snapshot["crs"],
                "candidate": candidate_snapshot["crs"],
            },
            "bounds": {
                **bounds_check,
            },
            "data_schema": {
                "passed": schema_matches,
                "active_sha256": active_snapshot["data_schema_sha256"],
                "candidate_sha256": candidate_snapshot[
                    "data_schema_sha256"
                ],
            },
            "feature_count": {
                "passed": feature_matches,
                "applies": feature_applies,
                "active": active_count,
                "candidate": candidate_count,
                "minimum_baseline": _MIN_FEATURE_BASELINE,
                "minimum_retained_ratio": _MIN_FEATURE_RETAINED_RATIO,
                "maximum_growth_ratio": _MAX_FEATURE_GROWTH_RATIO,
                "absolute_growth_allowance": _FEATURE_GROWTH_ALLOWANCE,
                "minimum_candidate": minimum_feature_count,
                "maximum_candidate": maximum_feature_count,
                "retained_ratio": retained_ratio,
            },
        }
    failed_checks = sorted(
        name
        for name, check in checks.items()
        if check.get("passed") is not True
    )
    return {
        "schema_version": _CONTINUITY_SCHEMA,
        "passed": not failed_checks,
        "baseline_version_id": active_version.id,
        "candidate": candidate_snapshot,
        "checks": checks,
        "failed_checks": failed_checks,
    }


def _validate_prepared(prepared: PreparedDelivery) -> dict[str, Any]:
    if not isinstance(prepared, PreparedDelivery):
        raise DeliveryBuildError("prepared delivery has an invalid type")
    if prepared.delivery_kind not in {"vector", "raster", "tiles"}:
        raise DeliveryBuildError("prepared delivery kind is unsupported")
    if prepared.source_version is not None and (
        not isinstance(prepared.source_version, str)
        or len(prepared.source_version) > _MAX_SOURCE_VERSION_LENGTH
    ):
        raise DeliveryBuildError("prepared source version is too large")
    _sha(prepared.content_sha256, "content")
    if _CRS_RE.fullmatch(prepared.crs) is None:
        raise DeliveryBuildError("prepared delivery CRS is invalid")
    bounds = _bounds(prepared.bounds_json)
    if prepared.feature_count is not None and (
        isinstance(prepared.feature_count, bool)
        or not isinstance(prepared.feature_count, int)
        or prepared.feature_count < 0
    ):
        raise DeliveryBuildError("prepared feature count is invalid")
    if (
        prepared.delivery_kind == "vector"
        and (
            not isinstance(prepared.feature_count, int)
            or isinstance(prepared.feature_count, bool)
            or prepared.feature_count < 1
        )
    ) or (
        prepared.delivery_kind != "vector"
        and prepared.feature_count is not None
    ):
        raise DeliveryBuildError(
            "prepared feature count is incompatible with delivery kind"
        )
    if prepared.reference_at is not None:
        _aware_moment(prepared.reference_at)
    if (
        not isinstance(prepared.validation_json, dict)
        or prepared.validation_json.get("passed") is not True
    ):
        raise DeliveryBuildError("prepared delivery did not pass validation")
    if "continuity_gate" in prepared.validation_json:
        raise DeliveryBuildError(
            "prepared delivery cannot provide its own continuity evidence"
        )
    _semantic_schema_sha256(
        prepared.delivery_kind,
        prepared.validation_json,
    )
    canonical_json_sha256(prepared.validation_json)
    if (
        not prepared.input_artifact_ids
        or len(prepared.input_artifact_ids) > 256
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in prepared.input_artifact_ids
        )
        or len(set(prepared.input_artifact_ids)) != len(prepared.input_artifact_ids)
    ):
        raise DeliveryBuildError("prepared input artifact ids are invalid")
    if (
        not isinstance(prepared.supporting_artifacts, tuple)
        or len(prepared.supporting_artifacts) > 1024
        or len(set(prepared.supporting_artifacts))
        != len(prepared.supporting_artifacts)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or isinstance(item[0], bool)
            or not isinstance(item[0], int)
            or item[0] < 1
            or item[1]
            not in {"style", "style_package", "style_resource", "metadata"}
            for item in prepared.supporting_artifacts
        )
    ):
        raise DeliveryBuildError(
            "prepared supporting artifact provenance is invalid"
        )
    if not prepared.assets or len(prepared.assets) > 512:
        raise DeliveryBuildError("prepared delivery assets are invalid")
    if len({asset.asset_key for asset in prepared.assets}) != len(prepared.assets):
        raise DeliveryBuildError("prepared delivery asset keys are duplicated")
    primary = [asset for asset in prepared.assets if asset.is_primary]
    if len(primary) != 1:
        raise DeliveryBuildError("prepared delivery requires one primary asset")
    for asset in prepared.assets:
        _validate_asset(asset, prepared.delivery_kind)
    _validate_local_metadata_asset_shape(
        prepared,
        prepared_validation_sha256=canonical_json_sha256(
            prepared.validation_json
        ),
    )
    expected_kind = {
        "vector": "vector_table",
        "raster": "raster_cog",
        "tiles": "tile_archive",
    }[prepared.delivery_kind]
    if primary[0].asset_kind != expected_kind:
        raise DeliveryBuildError("primary asset is incompatible with delivery kind")
    if primary[0].sha256 != prepared.content_sha256:
        raise DeliveryBuildError("primary asset does not match delivery content hash")
    _validate_renderer_metadata(primary[0], prepared.delivery_kind)
    return {
        "delivery_kind": prepared.delivery_kind,
        "source_version": prepared.source_version,
        "content_sha256": prepared.content_sha256,
        "reference_at": (
            prepared.reference_at.isoformat()
            if prepared.reference_at is not None
            else None
        ),
        "crs": prepared.crs,
        "bounds": bounds,
        "feature_count": prepared.feature_count,
        "validation_sha256": canonical_json_sha256(prepared.validation_json),
        "supporting_artifacts": [
            {"artifact_id": artifact_id, "role": role}
            for artifact_id, role in prepared.supporting_artifacts
        ],
        "assets": [
            {
                "asset_key": asset.asset_key,
                "asset_kind": asset.asset_kind,
                "is_primary": asset.is_primary,
                "storage_backend": asset.storage_backend,
                "storage_key": asset.storage_key,
                "media_type": asset.media_type,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
                "metadata_sha256": canonical_json_sha256(asset.metadata_json),
            }
            for asset in prepared.assets
        ],
    }


def _validate_local_metadata_asset_shape(
    prepared: PreparedDelivery,
    *,
    prepared_validation_sha256: str,
) -> None:
    metadata_assets = [
        asset
        for asset in prepared.assets
        if asset.asset_kind == "metadata"
    ]
    if len(metadata_assets) != 1:
        raise DeliveryBuildError(
            "prepared delivery requires exactly one local metadata asset"
        )
    asset = metadata_assets[0]
    if (
        asset.asset_key != LOCAL_METADATA_ASSET_KEY
        or asset.is_primary
        or asset.storage_backend != "filesystem"
        or asset.media_type != "application/json"
    ):
        raise DeliveryBuildError(
            "prepared local metadata asset shape is invalid"
        )
    descriptor = asset.metadata_json
    if set(descriptor) != LOCAL_METADATA_DESCRIPTOR_KEYS:
        raise DeliveryBuildError(
            "prepared local metadata descriptor is invalid"
        )
    if (
        descriptor.get("schema_version") != LOCAL_METADATA_ASSET_SCHEMA
        or descriptor.get("document_schema_version")
        != LOCAL_METADATA_DOCUMENT_SCHEMA
        or descriptor.get("document_sha256") != asset.sha256
        or descriptor.get("document_size_bytes") != asset.size_bytes
    ):
        raise DeliveryBuildError(
            "prepared local metadata descriptor does not match its asset"
        )
    binding = descriptor.get("binding")
    if (
        not isinstance(binding, dict)
        or set(binding) != LOCAL_METADATA_BINDING_KEYS
    ):
        raise DeliveryBuildError(
            "prepared local metadata binding is invalid"
        )
    for key in (
        "layer_id",
        "source_id",
        "sync_run_id",
        "catalog_snapshot_id",
        "authorization_review_id",
    ):
        value = binding.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
        ):
            raise DeliveryBuildError(
                "prepared local metadata binding is invalid"
            )
    provider_key = binding.get("provider_key")
    if (
        not isinstance(provider_key, str)
        or not 1 <= len(provider_key) <= 64
    ):
        raise DeliveryBuildError(
            "prepared local metadata binding is invalid"
        )
    for key in (
        "catalog_definition_sha256",
        "source_definition_sha256",
        "authorization_review_sha256",
        "authorization_document_sha256",
        "content_sha256",
        "prepared_validation_sha256",
    ):
        _sha(binding.get(key), f"local metadata {key}")
    if (
        binding.get("delivery_kind") != prepared.delivery_kind
        or binding.get("content_sha256") != prepared.content_sha256
        or binding.get("prepared_validation_sha256")
        != prepared_validation_sha256
    ):
        raise DeliveryBuildError(
            "prepared local metadata binding does not match delivery"
        )


def _validate_local_metadata_asset_binding(
    prepared: PreparedDelivery,
    *,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
    snapshot: ReferenceCatalogSnapshot,
    authorization: ReferenceMirrorAuthorizationReview,
    prepared_validation_sha256: str,
) -> None:
    asset = next(
        item
        for item in prepared.assets
        if item.asset_kind == "metadata"
    )
    expected = local_metadata_binding(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        sync_run_id=run.id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        source_definition_sha256=run.source_definition_sha256,
        authorization_review_id=authorization.id,
        authorization_review_sha256=authorization.review_sha256,
        authorization_document_sha256=authorization.document_sha256,
        delivery_kind=prepared.delivery_kind,
        content_sha256=prepared.content_sha256,
        prepared_validation_sha256=prepared_validation_sha256,
    )
    if asset.metadata_json.get("binding") != expected:
        raise DeliveryBuildError(
            "prepared local metadata binding changed before version creation"
        )


def _semantic_schema_sha256(
    delivery_kind: str,
    validation: dict[str, Any],
) -> str:
    if (
        not isinstance(validation, dict)
        or validation.get("schema_version") != _VALIDATION_SCHEMA
        or validation.get("kind") != delivery_kind
    ):
        raise DeliveryBuildError(
            "prepared delivery validation schema is invalid"
        )
    checks = validation.get("checks")
    data_schema = checks.get("data_schema") if isinstance(checks, dict) else None
    expected = _DATA_SCHEMA_BY_KIND.get(delivery_kind)
    if (
        not isinstance(data_schema, dict)
        or expected is None
        or data_schema.get("schema_version") != expected
    ):
        raise DeliveryBuildError(
            "prepared delivery data schema is invalid"
        )
    _validate_data_schema(delivery_kind, data_schema)
    return canonical_json_sha256(data_schema)


def _active_delivery_version(
    db: Session,
    *,
    state: ReferenceLayerDeliveryState | None,
    provider_key: str,
    layer_id: int,
) -> ReferenceDeliveryVersion | None:
    if state is None:
        return None
    if state.status != "active" or state.active_version_id is None:
        raise DeliveryBuildError("active delivery state is invalid")
    version = db.scalar(
        select(ReferenceDeliveryVersion).where(
            ReferenceDeliveryVersion.id == state.active_version_id,
            ReferenceDeliveryVersion.provider_key == provider_key,
            ReferenceDeliveryVersion.layer_id == layer_id,
        )
    )
    if version is None:
        raise DeliveryBuildError("active delivery version is unavailable")
    return version


def _active_continuity_snapshot(
    version: ReferenceDeliveryVersion,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if (
            not isinstance(version.validation_sha256, str)
            or canonical_json_sha256(version.validation_json)
            != version.validation_sha256
        ):
            return None, "active_validation_hash_invalid"
        bounds = _bounds(version.bounds_json)
        schema_sha256 = _semantic_schema_sha256(
            version.delivery_kind,
            version.validation_json,
        )
        if _CRS_RE.fullmatch(version.crs) is None:
            return None, "active_crs_invalid"
        if version.delivery_kind == "vector":
            if (
                isinstance(version.feature_count, bool)
                or not isinstance(version.feature_count, int)
                or version.feature_count < 1
            ):
                return None, "active_feature_count_invalid"
        elif version.feature_count is not None:
            return None, "active_feature_count_invalid"
    except (DeliveryBuildError, TypeError, ValueError):
        return None, "active_validation_schema_invalid"
    return (
        {
            "delivery_kind": version.delivery_kind,
            "crs": version.crs,
            "bounds": bounds,
            "feature_count": version.feature_count,
            "data_schema_sha256": schema_sha256,
        },
        None,
    )


def _bounds_continuity_check(
    *,
    delivery_kind: str,
    active: dict[str, float],
    candidate: dict[str, float],
) -> dict[str, Any]:
    if delivery_kind == "tiles":
        return {
            "passed": active == candidate,
            "mode": "exact_tile_coverage",
            "active": active,
            "candidate": candidate,
        }

    active_area = (
        (active["east"] - active["west"])
        * (active["north"] - active["south"])
    )
    candidate_area = (
        (candidate["east"] - candidate["west"])
        * (candidate["north"] - candidate["south"])
    )
    intersection_width = max(
        0.0,
        min(active["east"], candidate["east"])
        - max(active["west"], candidate["west"]),
    )
    intersection_height = max(
        0.0,
        min(active["north"], candidate["north"])
        - max(active["south"], candidate["south"]),
    )
    intersection_area = intersection_width * intersection_height
    overlap_ratio = intersection_area / min(active_area, candidate_area)
    area_ratio = candidate_area / active_area
    passed = (
        overlap_ratio >= _MIN_BOUNDS_OVERLAP_RATIO
        and _MIN_BOUNDS_AREA_RATIO
        <= area_ratio
        <= _MAX_BOUNDS_AREA_RATIO
    )
    return {
        "passed": passed,
        "mode": "overlap_and_area_ratio",
        "active": active,
        "candidate": candidate,
        "intersection_area": round(intersection_area, 12),
        "active_area": round(active_area, 12),
        "candidate_area": round(candidate_area, 12),
        "overlap_ratio": round(overlap_ratio, 12),
        "area_ratio": round(area_ratio, 12),
        "minimum_overlap_ratio": _MIN_BOUNDS_OVERLAP_RATIO,
        "minimum_area_ratio": _MIN_BOUNDS_AREA_RATIO,
        "maximum_area_ratio": _MAX_BOUNDS_AREA_RATIO,
    }


def _validate_data_schema(
    delivery_kind: str,
    data_schema: dict[str, Any],
) -> None:
    if delivery_kind == "vector":
        if set(data_schema) != {"schema_version", "columns"}:
            raise DeliveryBuildError(
                "prepared vector data schema is invalid"
            )
        columns = data_schema["columns"]
        if (
            not isinstance(columns, list)
            or not 2 <= len(columns) <= 512
        ):
            raise DeliveryBuildError(
                "prepared vector data schema is invalid"
            )
        for column in columns:
            if (
                not isinstance(column, dict)
                or set(column) != {"name", "data_type", "not_null"}
                or not isinstance(column["name"], str)
                or not 1 <= len(column["name"]) <= 255
                or not isinstance(column["data_type"], str)
                or not 1 <= len(column["data_type"]) <= 255
                or not isinstance(column["not_null"], bool)
            ):
                raise DeliveryBuildError(
                    "prepared vector data schema is invalid"
                )
        if len({column["name"] for column in columns}) != len(columns):
            raise DeliveryBuildError(
                "prepared vector data schema is invalid"
            )
        return
    if delivery_kind == "raster":
        if set(data_schema) != {
            "schema_version",
            "driver",
            "band_types",
            "nodata_values",
            "pixel_size",
        }:
            raise DeliveryBuildError(
                "prepared raster data schema is invalid"
            )
        driver = data_schema["driver"]
        band_types = data_schema["band_types"]
        nodata_values = data_schema["nodata_values"]
        pixel_size = data_schema["pixel_size"]
        if (
            not isinstance(driver, str)
            or not 1 <= len(driver) <= 64
            or not isinstance(band_types, list)
            or not 1 <= len(band_types) <= 64
            or any(
                not isinstance(item, str) or not 1 <= len(item) <= 64
                for item in band_types
            )
            or not isinstance(nodata_values, list)
            or len(nodata_values) != len(band_types)
            or any(
                item is not None
                and (
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not isfinite(float(item))
                )
                for item in nodata_values
            )
            or not isinstance(pixel_size, dict)
            or set(pixel_size) != {"x", "y"}
            or any(
                isinstance(pixel_size[axis], bool)
                or not isinstance(pixel_size[axis], (int, float))
                or not isfinite(float(pixel_size[axis]))
                or float(pixel_size[axis]) <= 0
                for axis in ("x", "y")
            )
        ):
            raise DeliveryBuildError(
                "prepared raster data schema is invalid"
            )
        return
    if delivery_kind == "tiles":
        if set(data_schema) != {"schema_version", "archives"}:
            raise DeliveryBuildError(
                "prepared tile data schema is invalid"
            )
        archives = data_schema["archives"]
        if (
            not isinstance(archives, list)
            or not 1 <= len(archives) <= 512
        ):
            raise DeliveryBuildError(
                "prepared tile data schema is invalid"
            )
        style_keys: set[str | None] = set()
        for archive in archives:
            if (
                not isinstance(archive, dict)
                or set(archive)
                != {
                    "catalog_style_source_key",
                    "image_format",
                    "min_zoom",
                    "max_zoom",
                }
            ):
                raise DeliveryBuildError(
                    "prepared tile data schema is invalid"
                )
            style_key = archive["catalog_style_source_key"]
            image_format = archive["image_format"]
            min_zoom = archive["min_zoom"]
            max_zoom = archive["max_zoom"]
            if (
                (
                    style_key is not None
                    and (
                        not isinstance(style_key, str)
                        or not 1 <= len(style_key) <= 500
                    )
                )
                or style_key in style_keys
                or image_format not in {"png", "jpg"}
                or isinstance(min_zoom, bool)
                or not isinstance(min_zoom, int)
                or isinstance(max_zoom, bool)
                or not isinstance(max_zoom, int)
                or not 0 <= min_zoom <= max_zoom <= 22
            ):
                raise DeliveryBuildError(
                    "prepared tile data schema is invalid"
                )
            style_keys.add(style_key)
        return
    raise DeliveryBuildError("prepared delivery data schema is invalid")


def _validate_asset(asset: PreparedDeliveryAsset, delivery_kind: str) -> None:
    if not isinstance(asset, PreparedDeliveryAsset):
        raise DeliveryBuildError("prepared asset has an invalid type")
    if _ASSET_KEY_RE.fullmatch(asset.asset_key) is None:
        raise DeliveryBuildError("prepared asset key is invalid")
    if asset.asset_kind not in {
        "vector_table",
        "raster_cog",
        "tile_archive",
        "tile_prefix",
        "style_sld",
        "style_package",
        "legend",
        "metadata",
    }:
        raise DeliveryBuildError("prepared asset kind is invalid")
    if asset.storage_backend not in {"postgres", "filesystem", "s3"}:
        raise DeliveryBuildError("prepared asset storage backend is invalid")
    _sha(asset.sha256, "asset")
    if not isinstance(asset.media_type, str) or not 1 <= len(asset.media_type) <= 255:
        raise DeliveryBuildError("prepared asset media type is invalid")
    if not isinstance(asset.metadata_json, dict):
        raise DeliveryBuildError("prepared asset metadata is invalid")
    canonical_json_sha256(asset.metadata_json)
    if asset.storage_backend == "postgres":
        if asset.asset_kind != "vector_table" or delivery_kind != "vector":
            raise DeliveryBuildError("only vector tables may use PostgreSQL storage")
        if _POSTGIS_KEY_RE.fullmatch(asset.storage_key) is None:
            raise DeliveryBuildError("prepared PostGIS storage key is invalid")
        if asset.size_bytes is not None:
            raise DeliveryBuildError("PostGIS assets must not declare file bytes")
        return
    if asset.storage_backend != "filesystem":
        raise DeliveryBuildError("S3 delivery is not enabled in this deployment")
    match = _BLOB_KEY_RE.fullmatch(asset.storage_key)
    if (
        match is None
        or match.group("sha") != asset.sha256
        or match.group("prefix") != asset.sha256[:2]
    ):
        raise DeliveryBuildError("filesystem asset is not content-addressed")
    if (
        isinstance(asset.size_bytes, bool)
        or not isinstance(asset.size_bytes, int)
        or asset.size_bytes <= 0
    ):
        raise DeliveryBuildError("filesystem asset size is invalid")


def _validate_renderer_metadata(
    asset: PreparedDeliveryAsset,
    delivery_kind: str,
) -> None:
    metadata = asset.metadata_json
    if delivery_kind == "tiles":
        if metadata.get("renderer") != "tile_archive":
            raise DeliveryBuildError("tile archive renderer metadata is missing")
        return
    if metadata.get("renderer") != "geoserver":
        raise DeliveryBuildError("GeoServer renderer metadata is missing")
    if _RESOURCE_RE.fullmatch(str(metadata.get("layer_name", ""))) is None:
        raise DeliveryBuildError("GeoServer layer name is invalid")
    for key in ("identify_available", "legend_available"):
        if not isinstance(metadata.get(key), bool):
            raise DeliveryBuildError(f"GeoServer {key} flag is invalid")
    styles = metadata.get("styles", {})
    if not isinstance(styles, dict) or len(styles) > 256:
        raise DeliveryBuildError("GeoServer style map is invalid")
    for style_id, resource_name in styles.items():
        if (
            not isinstance(style_id, str)
            or not style_id.isascii()
            or not style_id.isdecimal()
            or int(style_id) < 1
            or _RESOURCE_RE.fullmatch(str(resource_name)) is None
        ):
            raise DeliveryBuildError("GeoServer style mapping is invalid")


def _bounds(value: dict[str, Any]) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"west", "south", "east", "north"}:
        raise DeliveryBuildError("prepared bounds are invalid")
    result: dict[str, float] = {}
    for key in ("west", "south", "east", "north"):
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise DeliveryBuildError("prepared bounds are invalid")
        number = float(raw)
        if not isfinite(number):
            raise DeliveryBuildError("prepared bounds are invalid")
        result[key] = number
    if (
        not -180 <= result["west"] < result["east"] <= 180
        or not -90 <= result["south"] < result["north"] <= 90
    ):
        raise DeliveryBuildError("prepared bounds are outside WGS84")
    return result


def _sha(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DeliveryBuildError(f"prepared {label} hash is invalid")


def _aware_moment(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise DeliveryBuildError("delivery timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


def _fresh_lease_moment(
    db: Session,
    injected: datetime | None,
) -> datetime:
    if injected is not None:
        return _aware_moment(injected)
    database_now = db.scalar(select(func.clock_timestamp()))
    if not isinstance(database_now, datetime):
        raise DeliveryBuildError("database clock is unavailable")
    return _aware_moment(database_now)


def _lock_layer(db: Session, provider_key: str, layer_id: int) -> None:
    identity = f"{provider_key}\0{layer_id}".encode("utf-8")
    digest = hashlib.sha256(_LOCK_DOMAIN + identity).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
