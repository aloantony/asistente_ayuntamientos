"""Canonical, version-bound metadata for fully local SIUR deliveries.

The public document deliberately contains no acquisition endpoints, storage
keys, source configuration or reviewed origins.  It is created before the
delivery version, stored in the same filesystem CAS as the other immutable
assets, and later checked against the active database projection without
performing any upstream request.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import stat
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.delivery_builder import (
    DeliveryBuildError,
    LocalMetadataPrecommitContext,
    LocalMetadataVerification,
    PreparedDelivery,
    PreparedDeliveryAsset,
    canonical_json_bytes,
    canonical_json_sha256,
    require_complete_delivery_style_plan,
)
from app.reference_layers.local_metadata_contract import (
    LOCAL_METADATA_ASSET_KEY,
    LOCAL_METADATA_ASSET_SCHEMA,
    LOCAL_METADATA_DOCUMENT_SCHEMA,
    local_metadata_asset_descriptor,
    local_metadata_binding,
    local_metadata_gate_matches,
)
from app.reference_layers.local_delivery import (
    LocalDeliveryError,
    resolve_local_delivery,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_bound_sync_run_authorization,
    require_version_local_service_authorization,
    stored_mirror_authorization_review_is_valid,
)
from app.reference_layers.mirror_lifecycle import (
    LocalMetadataTransitionVerification,
    SyncRunLease,
    catalog_snapshot_contains_active_layer,
    stored_catalog_snapshot_is_valid,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
    ReferenceDeliveryVersionArtifact,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceMirrorAuthorizationReview,
    ReferenceService,
    ReferenceSourceArtifact,
    ReferenceStyleParityPlan,
    ReferenceStyleParityPlanItem,
    ReferenceStyleParityPlanResource,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.wms_delivery import LayerDeliveryAvailability

MAX_LOCAL_METADATA_BYTES = 4 * 1024 * 1024
_FINAL_VALIDATION_ONLY_KEYS = frozenset(
    {
        "continuity_gate",
        "style_parity_gate",
        "local_metadata_gate",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "binding",
        "catalog",
        "source",
        "styles",
        "provenance",
        "delivery",
        "authorization",
    }
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "allowed_origins",
        "base_url",
        "canonical_origin",
        "capabilities_url",
        "config",
        "config_json",
        "endpoint",
        "endpoint_url",
        "final_url",
        "metadata_url",
        "origin",
        "origins",
        "remote_name",
        "resolved_url",
        "reviewed_document",
        "source_url",
        "storage_key",
        "url_template",
    }
)


class SessionFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Session]: ...


class LocalMetadataError(RuntimeError):
    """Local metadata is absent, stale or fails an immutable invariant."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "local_metadata_invalid",
    ) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LocalMetadataDocument:
    body: bytes
    sha256: str
    size_bytes: int
    version_id: int


def attach_local_metadata_asset(
    session_factory: SessionFactory,
    store: ReferenceBlobStore,
    lease: SyncRunLease,
    prepared: PreparedDelivery,
) -> PreparedDelivery:
    """Write one deterministic metadata document before version creation."""

    if any(asset.asset_kind == "metadata" for asset in prepared.assets):
        raise LocalMetadataError(
            "materialized delivery already contains a metadata asset"
        )
    with session_factory() as db:
        source, run, layer, service, snapshot, review = (
            _load_generation_identity(db, lease, prepared)
        )
        artifact_links = _prepared_artifact_links(prepared)
        artifacts = _load_generation_artifacts(
            db,
            source=source,
            run=run,
            artifact_links=artifact_links,
        )
        plan, plan_items = require_complete_delivery_style_plan(
            db,
            source=source,
            run=run,
            snapshot=snapshot,
            delivery_kind=prepared.delivery_kind,
            artifact_links=set(artifact_links),
        )
        styles = tuple(
            db.scalars(
                select(ReferenceLayerStyle)
                .where(
                    ReferenceLayerStyle.provider_key
                    == source.provider_key,
                    ReferenceLayerStyle.layer_id == source.layer_id,
                )
                .order_by(
                    ReferenceLayerStyle.source_key,
                    ReferenceLayerStyle.id,
                )
            )
        )
        document = _assemble_document(
            prepared=prepared,
            source=source,
            run=run,
            layer=layer,
            service=service,
            snapshot=snapshot,
            review=review,
            plan=plan,
            plan_items=plan_items,
            styles=styles,
            artifacts=artifacts,
            artifact_links=artifact_links,
        )

    body = canonical_json_bytes(document)
    digest = hashlib.sha256(body).hexdigest()
    try:
        blob = store.put_stream(
            io.BytesIO(body),
            max_bytes=min(
                MAX_LOCAL_METADATA_BYTES,
                store.max_blob_bytes,
            ),
            expected_sha256=digest,
            expected_size=len(body),
        )
    except ReferenceBlobStoreError as error:
        raise LocalMetadataError(
            "could not store canonical local metadata",
            code="local_metadata_storage_failed",
        ) from error
    descriptor = local_metadata_asset_descriptor(
        document_sha256=blob.sha256,
        document_size_bytes=blob.size_bytes,
        binding=document["binding"],
    )
    metadata_asset = PreparedDeliveryAsset(
        asset_key=LOCAL_METADATA_ASSET_KEY,
        asset_kind="metadata",
        is_primary=False,
        storage_backend=blob.storage_backend,
        storage_key=blob.storage_key,
        media_type="application/json",
        sha256=blob.sha256,
        size_bytes=blob.size_bytes,
        metadata_json=descriptor,
    )
    return replace(
        prepared,
        assets=(*prepared.assets, metadata_asset),
    )


def verify_local_metadata_precommit(
    store: ReferenceBlobStore,
    context: LocalMetadataPrecommitContext,
) -> LocalMetadataVerification:
    """Rebuild and verify the exact CAS body inside version creation."""

    service = context.db.scalar(
        select(ReferenceService).where(
            ReferenceService.id == context.layer.service_id,
            ReferenceService.provider_key
            == context.source.provider_key,
            ReferenceService.last_seen_snapshot_id
            == context.snapshot.id,
        )
    )
    if service is None:
        raise LocalMetadataError(
            "local metadata service changed before version creation"
        )
    styles = tuple(
        context.db.scalars(
            select(ReferenceLayerStyle)
            .where(
                ReferenceLayerStyle.provider_key
                == context.source.provider_key,
                ReferenceLayerStyle.layer_id
                == context.source.layer_id,
            )
            .order_by(
                ReferenceLayerStyle.source_key,
                ReferenceLayerStyle.id,
            )
        )
    )
    artifacts = {
        artifact.id: artifact
        for artifact in context.artifacts
    }
    expected_artifact_ids = {
        artifact_id
        for artifact_id, _role in context.artifact_links
    }
    if (
        set(artifacts) != expected_artifact_ids
        or any(
            artifact.source_id != context.source.id
            for artifact in artifacts.values()
        )
    ):
        raise LocalMetadataError(
            "local metadata provenance changed before version creation"
        )
    document = _assemble_document(
        prepared=context.prepared,
        source=context.source,
        run=context.run,
        layer=context.layer,
        service=service,
        snapshot=context.snapshot,
        review=context.authorization,
        plan=context.plan,
        plan_items=context.plan_items,
        styles=styles,
        artifacts=artifacts,
        artifact_links=context.artifact_links,
    )
    expected_body = canonical_json_bytes(document)
    expected_sha256 = hashlib.sha256(expected_body).hexdigest()
    candidates = [
        asset
        for asset in context.prepared.assets
        if asset.asset_kind == "metadata"
    ]
    if len(candidates) != 1:
        raise LocalMetadataError(
            "prepared delivery has no unique metadata asset"
        )
    asset = candidates[0]
    expected_descriptor = local_metadata_asset_descriptor(
        document_sha256=expected_sha256,
        document_size_bytes=len(expected_body),
        binding=document["binding"],
    )
    if (
        asset.sha256 != expected_sha256
        or asset.size_bytes != len(expected_body)
        or asset.metadata_json != expected_descriptor
    ):
        raise LocalMetadataError(
            "prepared metadata does not match locked delivery rows"
        )
    body, parsed = _read_metadata_asset(store, asset)
    if body != expected_body or parsed != document:
        raise LocalMetadataError(
            "prepared metadata blob does not match locked delivery rows"
        )
    return LocalMetadataVerification(
        document_sha256=expected_sha256,
        document_size_bytes=len(expected_body),
    )


def verify_local_metadata_transition(
    store: ReferenceBlobStore,
    db: Session,
    version: ReferenceDeliveryVersion,
) -> LocalMetadataTransitionVerification:
    """Rebuild and hash frozen metadata inside a lifecycle transaction."""

    assets = tuple(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    metadata_asset = _unique_metadata_asset(assets)
    expected = _expected_documents_for_versions(
        db,
        versions=(version,),
        assets_by_version={version.id: assets},
    ).get(version.id)
    if expected is None:
        raise LocalMetadataError(
            "delivery metadata transition evidence is incomplete"
        )
    expected_body = canonical_json_bytes(expected)
    expected_binding = expected["binding"]
    expected_descriptor = local_metadata_asset_descriptor(
        document_sha256=metadata_asset.sha256,
        document_size_bytes=metadata_asset.size_bytes or 0,
        binding=expected_binding,
    )
    if (
        metadata_asset.metadata_json != expected_descriptor
        or not local_metadata_gate_matches(
            version.validation_json,
            document_sha256=metadata_asset.sha256,
            document_size_bytes=metadata_asset.size_bytes,
            descriptor=expected_descriptor,
            binding=expected_binding,
        )
    ):
        raise LocalMetadataError(
            "delivery metadata transition gate is invalid"
        )
    body, parsed = _read_metadata_asset(store, metadata_asset)
    if body != expected_body or parsed != expected:
        raise LocalMetadataError(
            "delivery metadata transition body is invalid"
        )
    return LocalMetadataTransitionVerification(
        asset_id=metadata_asset.id,
        document_sha256=metadata_asset.sha256,
        document_size_bytes=len(body),
    )


def resolve_local_metadata(
    db: Session,
    store: ReferenceBlobStore,
    *,
    layer: ReferenceLayer,
) -> LocalMetadataDocument:
    """Resolve and fully verify metadata for the active local version."""

    styles = tuple(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(
                ReferenceLayerStyle.provider_key == layer.provider_key,
                ReferenceLayerStyle.layer_id == layer.id,
                ReferenceLayerStyle.last_seen_snapshot_id
                == layer.last_seen_snapshot_id,
                ReferenceLayerStyle.status.in_(("active", "degraded")),
            )
            .order_by(
                ReferenceLayerStyle.sort_order,
                ReferenceLayerStyle.id,
            )
        )
    )
    default_style = next(
        (style for style in styles if style.is_default),
        None,
    )
    if styles and default_style is None:
        raise LocalMetadataError("active layer has no default local style")
    try:
        selection = resolve_local_delivery(
            db,
            layer=layer,
            style=default_style,
            operation="tile",
        )
    except LocalDeliveryError as error:
        raise LocalMetadataError(
            "active local delivery is not serviceable",
            code=error.blocker,
        ) from error
    if selection is None:
        raise LocalMetadataError(
            "layer has no active local delivery",
            code="local_metadata_unavailable",
        )
    version = db.get(ReferenceDeliveryVersion, selection.version_id)
    if version is None:
        raise LocalMetadataError("active metadata version is missing")
    assets = tuple(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    metadata_asset = _unique_metadata_asset(assets)
    source = db.get(ReferenceLayerSource, version.source_id)
    run = db.get(ReferenceSyncRun, version.sync_run_id)
    if source is None or run is None:
        raise LocalMetadataError(
            "active metadata authorization context is missing"
        )
    try:
        require_version_local_service_authorization(
            db,
            version=version,
            source=source,
            run=run,
        )
    except MirrorAuthorizationError as error:
        raise LocalMetadataError(
            "active local metadata authorization is not valid",
            code=error.code,
        ) from error

    expected = _expected_documents_for_versions(
        db,
        versions=(version,),
        assets_by_version={version.id: assets},
    ).get(version.id)
    if expected is None:
        raise LocalMetadataError(
            "active local metadata evidence is incomplete"
        )
    expected_body = canonical_json_bytes(expected)
    body, parsed = _read_metadata_asset(store, metadata_asset)
    if body != expected_body or parsed != expected:
        raise LocalMetadataError(
            "active local metadata does not match its immutable version"
        )
    expected_descriptor = local_metadata_asset_descriptor(
        document_sha256=metadata_asset.sha256,
        document_size_bytes=metadata_asset.size_bytes or 0,
        binding=expected["binding"],
    )
    if metadata_asset.metadata_json != expected_descriptor:
        raise LocalMetadataError(
            "active local metadata descriptor is invalid"
        )
    return LocalMetadataDocument(
        body=body,
        sha256=metadata_asset.sha256,
        size_bytes=len(body),
        version_id=version.id,
    )


def catalog_local_metadata_availability(
    db: Session,
    store: ReferenceBlobStore,
    *,
    provider_key: str,
    layers: list[ReferenceLayer],
    local_availability: dict[
        int,
        LayerDeliveryAvailability | None,
    ],
) -> dict[int, bool]:
    """Verify active metadata in bounded database queries for a catalog."""

    result = {layer.id: False for layer in layers}
    eligible_ids = [
        layer.id
        for layer in layers
        if layer.node_type == "layer"
        and (
            local_availability.get(layer.id) is not None
            and local_availability[layer.id].delivery_available
        )
    ]
    if not eligible_ids:
        return result
    states = tuple(
        db.scalars(
            select(ReferenceLayerDeliveryState).where(
                ReferenceLayerDeliveryState.provider_key == provider_key,
                ReferenceLayerDeliveryState.layer_id.in_(eligible_ids),
                ReferenceLayerDeliveryState.status == "active",
                ReferenceLayerDeliveryState.active_version_id.is_not(None),
            )
        )
    )
    version_ids = [
        state.active_version_id
        for state in states
        if state.active_version_id is not None
    ]
    if not version_ids:
        return result
    versions = {
        version.id: version
        for version in db.scalars(
            select(ReferenceDeliveryVersion).where(
                ReferenceDeliveryVersion.id.in_(version_ids),
                ReferenceDeliveryVersion.provider_key == provider_key,
            )
        )
    }
    assets_by_version: dict[int, list[ReferenceDeliveryAsset]] = {}
    for asset in db.scalars(
        select(ReferenceDeliveryAsset)
        .where(
            ReferenceDeliveryAsset.version_id.in_(version_ids),
        )
        .order_by(
            ReferenceDeliveryAsset.version_id,
            ReferenceDeliveryAsset.id,
        )
    ):
        assets_by_version.setdefault(asset.version_id, []).append(asset)
    run_ids = {version.sync_run_id for version in versions.values()}
    runs = {
        run.id: run
        for run in db.scalars(
            select(ReferenceSyncRun).where(
                ReferenceSyncRun.id.in_(run_ids)
            )
        )
    }
    review_ids = {
        version.mirror_authorization_review_id
        for version in versions.values()
        if version.mirror_authorization_review_id is not None
    }
    reviews = {
        review.id: review
        for review in db.scalars(
            select(ReferenceMirrorAuthorizationReview).where(
                ReferenceMirrorAuthorizationReview.id.in_(
                    review_ids
                )
            )
        )
    }
    for state in states:
        if state.active_version_id is None:
            continue
        version = versions.get(state.active_version_id)
        version_assets = assets_by_version.get(
            state.active_version_id,
            [],
        )
        candidates = [
            asset
            for asset in version_assets
            if asset.asset_kind == "metadata"
        ]
        if (
            version is None
            or version.layer_id != state.layer_id
            or len(candidates) != 1
        ):
            continue
        asset = candidates[0]
        run = runs.get(version.sync_run_id)
        review = reviews.get(
            version.mirror_authorization_review_id
        )
        try:
            validation_is_valid = (
                canonical_json_sha256(version.validation_json)
                == version.validation_sha256
            )
            prepared_validation = _prepared_validation_from_version(
                version
            )
            prepared_validation_sha256 = canonical_json_sha256(
                prepared_validation
            )
        except (DeliveryBuildError, LocalMetadataError):
            continue
        if (
            run is None
            or review is None
            or run.id != version.sync_run_id
            or run.source_id != version.source_id
            or run.source_definition_sha256
            != review.source_definition_sha256
            or review.id
            != version.mirror_authorization_review_id
            or review.review_sha256
            != version.mirror_authorization_review_sha256
            or review.provider_key != version.provider_key
            or review.layer_id != version.layer_id
            or review.source_id != version.source_id
            or not stored_mirror_authorization_review_is_valid(
                review
            )
            or not validation_is_valid
        ):
            continue
        expected_binding = local_metadata_binding(
            provider_key=version.provider_key,
            layer_id=version.layer_id,
            source_id=version.source_id,
            sync_run_id=version.sync_run_id,
            catalog_snapshot_id=version.catalog_snapshot_id,
            catalog_definition_sha256=(
                version.catalog_definition_sha256
            ),
            source_definition_sha256=(
                run.source_definition_sha256
            ),
            authorization_review_id=review.id,
            authorization_review_sha256=review.review_sha256,
            authorization_document_sha256=(
                review.document_sha256
            ),
            delivery_kind=version.delivery_kind,
            content_sha256=version.content_sha256,
            prepared_validation_sha256=(
                prepared_validation_sha256
            ),
        )
        expected_descriptor = local_metadata_asset_descriptor(
            document_sha256=asset.sha256,
            document_size_bytes=asset.size_bytes or 0,
            binding=expected_binding,
        )
        if (
            asset.metadata_json != expected_descriptor
            or not local_metadata_gate_matches(
                version.validation_json,
                document_sha256=asset.sha256,
                document_size_bytes=asset.size_bytes,
                descriptor=asset.metadata_json,
                binding=expected_binding,
            )
            or not _metadata_asset_exists_without_read(
                store,
                asset,
            )
        ):
            continue
        result[state.layer_id] = True
    return result


def _load_generation_identity(
    db: Session,
    lease: SyncRunLease,
    prepared: PreparedDelivery,
) -> tuple[
    ReferenceLayerSource,
    ReferenceSyncRun,
    ReferenceLayer,
    ReferenceService,
    ReferenceCatalogSnapshot,
    ReferenceMirrorAuthorizationReview,
]:
    source = db.get(ReferenceLayerSource, lease.source_id)
    run = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.id == lease.run_id,
            ReferenceSyncRun.source_id == lease.source_id,
            ReferenceSyncRun.attempt_no == lease.attempt_no,
            ReferenceSyncRun.lease_token == lease.token,
            ReferenceSyncRun.status == "running",
        )
    )
    if source is None or run is None:
        raise LocalMetadataError(
            "leased run is unavailable while preparing metadata",
            code="lease_lost",
        )
    if (
        source.id != run.source_id
        or source.provider_key != run.provider_key
        or source.layer_id != run.layer_id
        or source.target_kind != prepared.delivery_kind
        or source.definition_sha256 != run.source_definition_sha256
    ):
        raise LocalMetadataError(
            "source identity changed while preparing metadata"
        )
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.id == source.layer_id,
            ReferenceLayer.provider_key == source.provider_key,
            ReferenceLayer.node_type == "layer",
            ReferenceLayer.status.in_(("active", "degraded")),
        )
    )
    if layer is None or layer.service_id is None:
        raise LocalMetadataError(
            "current catalog layer is unavailable while preparing metadata"
        )
    service = db.scalar(
        select(ReferenceService).where(
            ReferenceService.id == layer.service_id,
            ReferenceService.provider_key == source.provider_key,
        )
    )
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.id == layer.last_seen_snapshot_id,
            ReferenceCatalogSnapshot.provider_key
            == source.provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if (
        service is None
        or snapshot is None
        or not stored_catalog_snapshot_is_valid(snapshot)
        or not catalog_snapshot_contains_active_layer(snapshot, layer)
    ):
        raise LocalMetadataError(
            "current catalog evidence is invalid while preparing metadata"
        )
    try:
        review = require_bound_sync_run_authorization(
            db,
            run=run,
            source=source,
        )
    except MirrorAuthorizationError as error:
        raise LocalMetadataError(
            "mirror authorization changed while preparing metadata",
            code=error.code,
        ) from error
    if (
        run.mirror_authorization_review_id != review.id
        or run.mirror_authorization_review_sha256
        != review.review_sha256
    ):
        raise LocalMetadataError(
            "mirror authorization changed while preparing metadata"
        )
    return source, run, layer, service, snapshot, review


def _prepared_artifact_links(
    prepared: PreparedDelivery,
) -> tuple[tuple[int, str], ...]:
    links = {
        (artifact_id, "input")
        for artifact_id in prepared.input_artifact_ids
    }
    links.update(prepared.supporting_artifacts)
    return tuple(sorted(links, key=lambda item: (item[1], item[0])))


def _load_generation_artifacts(
    db: Session,
    *,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
    artifact_links: tuple[tuple[int, str], ...],
) -> dict[int, ReferenceSourceArtifact]:
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
    if not set(artifact_links) <= run_links:
        raise LocalMetadataError(
            "metadata provenance is not bound to the leased run"
        )
    ids = {artifact_id for artifact_id, _role in artifact_links}
    artifacts = {
        artifact.id: artifact
        for artifact in db.scalars(
            select(ReferenceSourceArtifact).where(
                ReferenceSourceArtifact.source_id == source.id,
                ReferenceSourceArtifact.id.in_(ids),
            )
        )
    }
    if set(artifacts) != ids:
        raise LocalMetadataError(
            "metadata provenance artifact is unavailable"
        )
    return artifacts


def _assemble_document(
    *,
    prepared: PreparedDelivery,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
    layer: ReferenceLayer,
    service: ReferenceService,
    snapshot: ReferenceCatalogSnapshot,
    review: ReferenceMirrorAuthorizationReview,
    plan: ReferenceStyleParityPlan,
    plan_items: tuple[ReferenceStyleParityPlanItem, ...],
    styles: tuple[ReferenceLayerStyle, ...],
    artifacts: dict[int, ReferenceSourceArtifact],
    artifact_links: tuple[tuple[int, str], ...],
) -> dict[str, Any]:
    run_definition = run.source_definition_json
    if not isinstance(run_definition, dict):
        raise LocalMetadataError(
            "sync-run source definition is unavailable"
        )
    run_config = run_definition.get("config")
    frozen_source_format = (
        run_config.get("format")
        if isinstance(run_config, dict)
        and isinstance(run_config.get("format"), str)
        else None
    )
    prepared_validation_sha256 = canonical_json_sha256(
        prepared.validation_json
    )
    binding = local_metadata_binding(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        sync_run_id=run.id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        source_definition_sha256=run.source_definition_sha256,
        authorization_review_id=review.id,
        authorization_review_sha256=review.review_sha256,
        authorization_document_sha256=review.document_sha256,
        delivery_kind=prepared.delivery_kind,
        content_sha256=prepared.content_sha256,
        prepared_validation_sha256=prepared_validation_sha256,
    )
    catalog = _safe_catalog_projection(
        snapshot=snapshot,
        layer=layer,
        service=service,
    )
    style_rows = {style.source_key: style for style in styles}
    style_definitions = {
        item["source_key"]: item
        for item in catalog.pop("_style_definitions")
    }
    style_items = []
    for item in sorted(
        plan_items,
        key=lambda value: (value.style_source_key, value.id),
    ):
        style_definition = style_definitions.get(item.style_source_key)
        style_row = style_rows.get(item.style_source_key)
        if item.style_id is None:
            if style_definition is not None:
                raise LocalMetadataError(
                    "implicit style unexpectedly exists in the catalog"
                )
            title = "Estilo predeterminado implícito"
        else:
            if (
                style_definition is None
                or style_row is None
                or style_row.id != item.style_id
            ):
                raise LocalMetadataError(
                    "style parity no longer matches the catalog"
                )
            title = _required_text(
                style_definition.get("title"),
                "catalog style title",
                500,
            )
        style_items.append(
            {
                "catalog_style_id": item.style_id,
                "source_key": item.style_source_key,
                "title": title,
                "is_default": item.is_default,
                "parity": {
                    "plan_item_id": item.id,
                    "kind": item.parity_kind,
                    "verified": item.verified,
                    "evidence_sha256": item.evidence_sha256,
                    "resource_count": item.resource_count,
                    "source_style_artifact_id": (
                        item.source_style_artifact_id
                    ),
                    "source_package_artifact_id": (
                        item.source_package_artifact_id
                    ),
                },
            }
        )
    input_descriptors = [
        _artifact_descriptor(artifacts[artifact_id])
        for artifact_id, role in artifact_links
        if role == "input"
    ]
    supporting_descriptors = [
        {
            **_artifact_descriptor(artifacts[artifact_id]),
            "role": role,
        }
        for artifact_id, role in artifact_links
        if role != "input"
    ]
    upstream_metadata = [
        {
            **_artifact_descriptor(artifacts[artifact_id]),
            "role": role,
        }
        for artifact_id, role in artifact_links
        if artifacts[artifact_id].artifact_kind == "metadata"
    ]
    non_metadata_assets = [
        _prepared_asset_descriptor(asset)
        for asset in sorted(
            prepared.assets,
            key=lambda value: value.asset_key,
        )
        if asset.asset_kind != "metadata"
    ]
    checks = prepared.validation_json.get("checks")
    data_schema = (
        checks.get("data_schema")
        if isinstance(checks, dict)
        else None
    )
    if not isinstance(data_schema, dict):
        raise LocalMetadataError(
            "prepared delivery has no canonical data schema"
        )
    document = {
        "schema_version": LOCAL_METADATA_DOCUMENT_SCHEMA,
        "binding": binding,
        "catalog": catalog,
        "source": {
            "id": source.id,
            "source_key": _required_text(
                source.source_key,
                "source key",
                255,
            ),
            "protocol": _required_text(
                run_definition.get("protocol"),
                "source protocol",
                30,
            ),
            "target_kind": _required_text(
                run_definition.get("target_kind"),
                "source target kind",
                16,
            ),
            "source_format": _optional_text(
                frozen_source_format,
                "source format",
                255,
            ),
            "sync_strategy": _required_text(
                run_definition.get("sync_strategy"),
                "source sync strategy",
                30,
            ),
            "definition_sha256": run.source_definition_sha256,
        },
        "styles": {
            "plan_id": plan.id,
            "plan_evidence_sha256": plan.evidence_sha256,
            "items": style_items,
        },
        "provenance": {
            "inputs": sorted(
                input_descriptors,
                key=lambda value: value["artifact_id"],
            ),
            "supporting_artifacts": sorted(
                supporting_descriptors,
                key=lambda value: (
                    value["role"],
                    value["artifact_id"],
                ),
            ),
            "upstream_metadata": sorted(
                upstream_metadata,
                key=lambda value: (
                    value["role"],
                    value["artifact_id"],
                ),
            ),
        },
        "delivery": {
            "kind": prepared.delivery_kind,
            "source_version": prepared.source_version,
            "content_sha256": prepared.content_sha256,
            "reference_at": _utc_isoformat(prepared.reference_at),
            "crs": prepared.crs,
            "bounds": _normalized_bounds(prepared.bounds_json),
            "feature_count": prepared.feature_count,
            "prepared_validation_sha256": (
                prepared_validation_sha256
            ),
            "data_schema_sha256": canonical_json_sha256(data_schema),
            "assets": non_metadata_assets,
        },
        "authorization": _authorization_projection(review),
    }
    _validate_document_shape(document)
    return document


def _safe_catalog_projection(
    *,
    snapshot: ReferenceCatalogSnapshot,
    layer: ReferenceLayer,
    service: ReferenceService,
) -> dict[str, Any]:
    normalized = snapshot.normalized_definition_json
    if not isinstance(normalized, dict):
        raise LocalMetadataError("catalog definition is unavailable")
    raw_layers = normalized.get("layers")
    raw_services = normalized.get("services")
    if not isinstance(raw_layers, list) or not isinstance(
        raw_services,
        list,
    ):
        raise LocalMetadataError("catalog definition is invalid")
    definitions = [
        value
        for value in raw_layers
        if isinstance(value, dict)
        and value.get("source_key") == layer.source_key
    ]
    if len(definitions) != 1:
        raise LocalMetadataError("catalog layer definition is ambiguous")
    layer_definition = definitions[0]
    service_key = layer_definition.get("service_key")
    services = [
        value
        for value in raw_services
        if isinstance(value, dict)
        and value.get("source_key") == service.source_key
        and value.get("source_key") == service_key
    ]
    if len(services) != 1:
        raise LocalMetadataError("catalog service definition is ambiguous")
    service_definition = services[0]
    style_definitions = layer_definition.get("styles", [])
    if not isinstance(style_definitions, list) or any(
        not isinstance(item, dict) for item in style_definitions
    ):
        raise LocalMetadataError("catalog style definitions are invalid")
    return {
        "snapshot": {
            "id": snapshot.id,
            "provider_key": snapshot.provider_key,
            "content_sha256": snapshot.content_sha256,
            "definition_sha256": snapshot.definition_sha256,
            "service_count": snapshot.service_count,
            "group_count": snapshot.group_count,
            "layer_count": snapshot.layer_count,
            "unresolved_count": snapshot.unresolved_count,
        },
        "service": {
            "id": service.id,
            "source_key": _required_text(
                service_definition.get("source_key"),
                "catalog service source key",
                255,
            ),
            "title": _required_text(
                service_definition.get("title"),
                "catalog service title",
                500,
            ),
            "protocol": _required_text(
                service_definition.get("upstream_protocol"),
                "catalog service protocol",
                30,
            ),
            "version": _optional_text(
                service_definition.get("version"),
                "catalog service version",
                30,
            ),
        },
        "layer": {
            "id": layer.id,
            "source_key": _required_text(
                layer_definition.get("source_key"),
                "catalog layer source key",
                255,
            ),
            "title": _required_text(
                layer_definition.get("title"),
                "catalog layer title",
                500,
            ),
            "description": _optional_text(
                layer_definition.get("description"),
                "catalog layer description",
                20_000,
            ),
            "role": _optional_text(
                layer_definition.get("role"),
                "catalog layer role",
                20,
            ),
            "renderer": _optional_text(
                layer_definition.get("renderer"),
                "catalog layer renderer",
                30,
            ),
            "queryable": _required_boolean(
                layer_definition.get("queryable"),
                "catalog layer queryable",
            ),
            "downloadable": _required_boolean(
                layer_definition.get("downloadable"),
                "catalog layer downloadable",
            ),
            "bounds": _optional_bounds(
                layer_definition.get("bounds")
            ),
            "supported_crs": _safe_string_list(
                layer_definition.get("supported_crs"),
                "catalog layer supported CRS",
                maximum=256,
            ),
            "min_zoom": _optional_non_negative_integer(
                layer_definition.get("min_zoom"),
                "catalog layer minimum zoom",
            ),
            "max_zoom": _optional_non_negative_integer(
                layer_definition.get("max_zoom"),
                "catalog layer maximum zoom",
            ),
        },
        "_style_definitions": deepcopy(style_definitions),
    }


def _artifact_descriptor(
    artifact: ReferenceSourceArtifact,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "artifact_kind": artifact.artifact_kind,
        "media_type": _required_text(
            artifact.media_type,
            "artifact media type",
            255,
        ),
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
    }


def _prepared_asset_descriptor(
    asset: PreparedDeliveryAsset,
) -> dict[str, Any]:
    return {
        "asset_key": asset.asset_key,
        "asset_kind": asset.asset_kind,
        "is_primary": asset.is_primary,
        "media_type": asset.media_type,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "metadata_sha256": canonical_json_sha256(
            asset.metadata_json
        ),
    }


def _authorization_projection(
    review: ReferenceMirrorAuthorizationReview,
) -> dict[str, Any]:
    return {
        "review_id": review.id,
        "document_sha256": review.document_sha256,
        "review_sha256": review.review_sha256,
        "source_definition_sha256": (
            review.source_definition_sha256
        ),
        "decision": review.decision,
        "reviewed_at": _utc_isoformat(review.reviewed_at),
        "license": {
            "name": review.license_name,
            "url": review.license_url,
            "terms": review.license_terms,
            "attribution": review.attribution,
        },
        "permissions": {
            "metadata_probe": review.allow_metadata_probe,
            "dataset_download": review.allow_dataset_download,
            "local_storage": review.allow_local_storage,
            "local_service": review.allow_local_service,
            "bulk_tile_seed": review.allow_bulk_tile_seed,
        },
    }


def _expected_documents_for_versions(
    db: Session,
    *,
    versions: tuple[ReferenceDeliveryVersion, ...],
    assets_by_version: dict[
        int,
        tuple[ReferenceDeliveryAsset, ...],
    ],
) -> dict[int, dict[str, Any]]:
    """Reconstruct exact documents with a fixed number of database queries."""

    versions_by_id = {version.id: version for version in versions}
    if not versions_by_id:
        return {}

    source_ids = {version.source_id for version in versions}
    run_ids = {version.sync_run_id for version in versions}
    snapshot_ids = {
        version.catalog_snapshot_id for version in versions
    }
    layer_ids = {version.layer_id for version in versions}
    review_ids = {
        version.mirror_authorization_review_id
        for version in versions
        if version.mirror_authorization_review_id is not None
    }
    sources = {
        row.id: row
        for row in db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.id.in_(source_ids)
            )
        )
    }
    runs = {
        row.id: row
        for row in db.scalars(
            select(ReferenceSyncRun).where(
                ReferenceSyncRun.id.in_(run_ids)
            )
        )
    }
    snapshots = {
        row.id: row
        for row in db.scalars(
            select(ReferenceCatalogSnapshot).where(
                ReferenceCatalogSnapshot.id.in_(snapshot_ids)
            )
        )
    }
    layers = {
        row.id: row
        for row in db.scalars(
            select(ReferenceLayer).where(
                ReferenceLayer.id.in_(layer_ids)
            )
        )
    }
    reviews = {
        row.id: row
        for row in db.scalars(
            select(ReferenceMirrorAuthorizationReview).where(
                ReferenceMirrorAuthorizationReview.id.in_(review_ids)
            )
        )
    }
    service_ids = {review.service_id for review in reviews.values()}
    services = {
        row.id: row
        for row in db.scalars(
            select(ReferenceService).where(
                ReferenceService.id.in_(service_ids)
            )
        )
    }

    version_links: dict[int, list[tuple[int, str]]] = {}
    for link in db.scalars(
        select(ReferenceDeliveryVersionArtifact).where(
            ReferenceDeliveryVersionArtifact.version_id.in_(
                versions_by_id
            )
        )
    ):
        version_links.setdefault(link.version_id, []).append(
            (link.artifact_id, link.role)
        )
    run_links: dict[
        tuple[int, int],
        set[tuple[int, str]],
    ] = {}
    for link in db.scalars(
        select(ReferenceSyncRunArtifact).where(
            ReferenceSyncRunArtifact.run_id.in_(run_ids),
            ReferenceSyncRunArtifact.source_id.in_(source_ids),
        )
    ):
        run_links.setdefault(
            (link.source_id, link.run_id),
            set(),
        ).add((link.artifact_id, link.role))

    artifact_ids = {
        artifact_id
        for links in version_links.values()
        for artifact_id, _role in links
    }
    artifacts = {
        row.id: row
        for row in db.scalars(
            select(ReferenceSourceArtifact).where(
                ReferenceSourceArtifact.id.in_(artifact_ids)
            )
        )
    }

    plans_by_identity: dict[
        tuple[int, int],
        list[ReferenceStyleParityPlan],
    ] = {}
    plans = tuple(
        db.scalars(
            select(ReferenceStyleParityPlan).where(
                ReferenceStyleParityPlan.source_id.in_(source_ids),
                ReferenceStyleParityPlan.sync_run_id.in_(run_ids),
            )
        )
    )
    for plan in plans:
        plans_by_identity.setdefault(
            (plan.source_id, plan.sync_run_id),
            [],
        ).append(plan)
    plan_ids = {plan.id for plan in plans}
    items_by_plan: dict[
        int,
        list[ReferenceStyleParityPlanItem],
    ] = {}
    items = tuple(
        db.scalars(
            select(ReferenceStyleParityPlanItem)
            .where(ReferenceStyleParityPlanItem.plan_id.in_(plan_ids))
            .order_by(ReferenceStyleParityPlanItem.id)
        )
    )
    for item in items:
        items_by_plan.setdefault(item.plan_id, []).append(item)
    item_ids = {item.id for item in items}
    resources_by_item: dict[
        int,
        list[ReferenceStyleParityPlanResource],
    ] = {}
    for resource in db.scalars(
        select(ReferenceStyleParityPlanResource)
        .where(
            ReferenceStyleParityPlanResource.plan_item_id.in_(
                item_ids
            )
        )
        .order_by(ReferenceStyleParityPlanResource.id)
    ):
        resources_by_item.setdefault(
            resource.plan_item_id,
            [],
        ).append(resource)

    providers = {version.provider_key for version in versions}
    styles_by_layer: dict[
        tuple[str, int],
        list[ReferenceLayerStyle],
    ] = {}
    for style in db.scalars(
        select(ReferenceLayerStyle)
        .where(
            ReferenceLayerStyle.provider_key.in_(providers),
            ReferenceLayerStyle.layer_id.in_(layer_ids),
        )
        .order_by(
            ReferenceLayerStyle.source_key,
            ReferenceLayerStyle.id,
        )
    ):
        styles_by_layer.setdefault(
            (style.provider_key, style.layer_id),
            [],
        ).append(style)

    expected: dict[int, dict[str, Any]] = {}
    for version in versions_by_id.values():
        try:
            source = sources.get(version.source_id)
            run = runs.get(version.sync_run_id)
            snapshot = snapshots.get(version.catalog_snapshot_id)
            layer = layers.get(version.layer_id)
            review = reviews.get(
                version.mirror_authorization_review_id
            )
            service = (
                services.get(review.service_id)
                if review is not None
                else None
            )
            if (
                source is None
                or run is None
                or snapshot is None
                or layer is None
                or review is None
                or service is None
                or source.provider_key != version.provider_key
                or source.layer_id != version.layer_id
                or run.source_id != source.id
                or run.provider_key != version.provider_key
                or run.layer_id != version.layer_id
                or not isinstance(
                    run.source_definition_json,
                    dict,
                )
                or run.source_definition_json.get("target_kind")
                != version.delivery_kind
                or canonical_json_sha256(
                    run.source_definition_json
                )
                != run.source_definition_sha256
                or run.mirror_authorization_review_id != review.id
                or run.mirror_authorization_review_sha256
                != review.review_sha256
                or snapshot.provider_key != version.provider_key
                or snapshot.definition_sha256
                != version.catalog_definition_sha256
                or layer.provider_key != version.provider_key
                or service.provider_key != version.provider_key
                or review.id
                != version.mirror_authorization_review_id
                or review.review_sha256
                != version.mirror_authorization_review_sha256
                or review.provider_key != version.provider_key
                or review.source_id != source.id
                or review.layer_id != layer.id
                or review.service_id != service.id
                or review.source_definition_sha256
                != run.source_definition_sha256
                or not stored_mirror_authorization_review_is_valid(
                    review
                )
                or not stored_catalog_snapshot_is_valid(snapshot)
                or not catalog_snapshot_contains_active_layer(
                    snapshot,
                    layer,
                )
            ):
                raise LocalMetadataError(
                    "version metadata identity is incomplete or invalid"
                )
            if (
                canonical_json_sha256(version.validation_json)
                != version.validation_sha256
            ):
                raise LocalMetadataError(
                    "version validation evidence is invalid"
                )
            prepared_validation = _prepared_validation_from_version(
                version
            )
            links = tuple(
                sorted(
                    version_links.get(version.id, []),
                    key=lambda item: (item[1], item[0]),
                )
            )
            if (
                any(
                    artifacts.get(artifact_id) is None
                    or artifacts[artifact_id].source_id != source.id
                    for artifact_id, _role in links
                )
                or not set(links)
                <= run_links.get((source.id, run.id), set())
            ):
                raise LocalMetadataError(
                    "version metadata provenance is incomplete"
                )
            plan_rows = plans_by_identity.get(
                (source.id, run.id),
                [],
            )
            if len(plan_rows) != 1:
                raise LocalMetadataError(
                    "version style parity plan is unavailable"
                )
            plan = plan_rows[0]
            plan_items = tuple(items_by_plan.get(plan.id, []))
            _validate_loaded_style_plan(
                plan=plan,
                items=plan_items,
                resources_by_item=resources_by_item,
                source=source,
                run=run,
                snapshot=snapshot,
                delivery_kind=version.delivery_kind,
                artifact_links=set(links),
            )
            assets = assets_by_version.get(version.id, ())
            prepared_assets = tuple(
                PreparedDeliveryAsset(
                    asset_key=asset.asset_key,
                    asset_kind=asset.asset_kind,
                    is_primary=asset.is_primary,
                    storage_backend=asset.storage_backend,
                    storage_key=asset.storage_key,
                    media_type=asset.media_type,
                    sha256=asset.sha256,
                    size_bytes=asset.size_bytes,
                    metadata_json=asset.metadata_json,
                )
                for asset in assets
                if asset.asset_kind != "metadata"
            )
            prepared = PreparedDelivery(
                delivery_kind=version.delivery_kind,
                source_version=version.source_version,
                content_sha256=version.content_sha256,
                reference_at=version.reference_at,
                crs=version.crs,
                bounds_json=version.bounds_json,
                feature_count=version.feature_count,
                validation_json=prepared_validation,
                input_artifact_ids=tuple(
                    sorted(
                        artifact_id
                        for artifact_id, role in links
                        if role == "input"
                    )
                ),
                assets=prepared_assets,
                supporting_artifacts=tuple(
                    (
                        artifact_id,
                        role,
                    )
                    for artifact_id, role in links
                    if role != "input"
                ),
            )
            expected[version.id] = _assemble_document(
                prepared=prepared,
                source=source,
                run=run,
                layer=layer,
                service=service,
                snapshot=snapshot,
                review=review,
                plan=plan,
                plan_items=plan_items,
                styles=tuple(
                    styles_by_layer.get(
                        (version.provider_key, version.layer_id),
                        [],
                    )
                ),
                artifacts={
                    artifact_id: artifacts[artifact_id]
                    for artifact_id, _role in links
                },
                artifact_links=links,
            )
        except (DeliveryBuildError, LocalMetadataError):
            continue
    return expected


def _validate_loaded_style_plan(
    *,
    plan: ReferenceStyleParityPlan,
    items: tuple[ReferenceStyleParityPlanItem, ...],
    resources_by_item: dict[
        int,
        list[ReferenceStyleParityPlanResource],
    ],
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
    snapshot: ReferenceCatalogSnapshot,
    delivery_kind: str,
    artifact_links: set[tuple[int, str]],
) -> None:
    if (
        plan.provider_key != source.provider_key
        or plan.layer_id != source.layer_id
        or plan.source_id != source.id
        or plan.sync_run_id != run.id
        or plan.catalog_snapshot_id != snapshot.id
        or plan.catalog_definition_sha256 != snapshot.definition_sha256
        or plan.delivery_kind != delivery_kind
        or not plan.complete
        or plan.missing_style_count != 0
        or canonical_json_sha256(plan.evidence_json)
        != plan.evidence_sha256
        or len(items) != plan.required_style_count
    ):
        raise LocalMetadataError(
            "version style parity plan is invalid"
        )
    required_links: set[tuple[int, str]] = set()
    for item in items:
        if (
            item.plan_id != plan.id
            or item.source_id != source.id
            or item.parity_kind == "missing"
            or not item.verified
            or canonical_json_sha256(item.evidence_json)
            != item.evidence_sha256
        ):
            raise LocalMetadataError(
                "version style parity item is invalid"
            )
        if item.source_style_artifact_id is not None:
            required_links.add(
                (item.source_style_artifact_id, "style")
            )
        if item.source_package_artifact_id is not None:
            required_links.add(
                (item.source_package_artifact_id, "style_package")
            )
        resources = resources_by_item.get(item.id, [])
        if len(resources) != item.resource_count:
            raise LocalMetadataError(
                "version style resource count is invalid"
            )
        for resource in resources:
            if (
                resource.source_id != source.id
                or canonical_json_sha256(
                    {
                        "artifact_id": resource.artifact_id,
                        "original_href": resource.original_href,
                        "resolved_url": resource.resolved_url,
                        "local_path": resource.local_path,
                        "media_type": resource.media_type,
                        "sha256": resource.sha256,
                    }
                )
                != resource.evidence_sha256
            ):
                raise LocalMetadataError(
                    "version style resource evidence is invalid"
                )
            required_links.add(
                (resource.artifact_id, "style_resource")
            )
    if not required_links <= artifact_links:
        raise LocalMetadataError(
            "version style provenance is incomplete"
        )


def _prepared_validation_from_version(
    version: ReferenceDeliveryVersion,
) -> dict[str, Any]:
    if not isinstance(version.validation_json, dict):
        raise LocalMetadataError("version validation is invalid")
    validation = deepcopy(version.validation_json)
    if any(key not in validation for key in _FINAL_VALIDATION_ONLY_KEYS):
        raise LocalMetadataError(
            "version validation gates are incomplete"
        )
    for key in _FINAL_VALIDATION_ONLY_KEYS:
        validation.pop(key)
    return validation


def _unique_metadata_asset(
    assets: tuple[ReferenceDeliveryAsset, ...],
) -> ReferenceDeliveryAsset:
    candidates = [
        asset for asset in assets if asset.asset_kind == "metadata"
    ]
    if len(candidates) != 1:
        raise LocalMetadataError(
            "active version has no unique metadata asset"
        )
    return candidates[0]


def _read_metadata_asset(
    store: ReferenceBlobStore,
    asset: ReferenceDeliveryAsset | PreparedDeliveryAsset,
) -> tuple[bytes, dict[str, Any]]:
    if not _metadata_asset_shape_is_valid(asset):
        raise LocalMetadataError("local metadata asset shape is invalid")
    try:
        with store.open_blob(asset.storage_key) as stream:
            descriptor = stream.fileno()
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != asset.size_bytes
            ):
                raise LocalMetadataError(
                    "local metadata file size is invalid"
                )
            body = stream.read(MAX_LOCAL_METADATA_BYTES + 1)
            after = os.fstat(descriptor)
    except LocalMetadataError:
        raise
    except (OSError, ReferenceBlobStoreError) as error:
        raise LocalMetadataError(
            "local metadata file is unavailable"
        ) from error
    if (
        len(body) != asset.size_bytes
        or len(body) > MAX_LOCAL_METADATA_BYTES
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or hashlib.sha256(body).hexdigest() != asset.sha256
    ):
        raise LocalMetadataError(
            "local metadata file failed integrity validation"
        )
    document = _parse_canonical_document(body)
    binding = document.get("binding")
    expected_descriptor = (
        local_metadata_asset_descriptor(
            document_sha256=asset.sha256,
            document_size_bytes=asset.size_bytes,
            binding=binding,
        )
        if isinstance(binding, dict)
        else None
    )
    if asset.metadata_json != expected_descriptor:
        raise LocalMetadataError(
            "local metadata manifest descriptor is invalid"
        )
    return body, document


def _metadata_asset_shape_is_valid(
    asset: ReferenceDeliveryAsset | PreparedDeliveryAsset,
) -> bool:
    return bool(
        asset.asset_key == LOCAL_METADATA_ASSET_KEY
        and asset.asset_kind == "metadata"
        and not asset.is_primary
        and asset.storage_backend == "filesystem"
        and isinstance(asset.sha256, str)
        and len(asset.sha256) == 64
        and not any(
            character not in "0123456789abcdef"
            for character in asset.sha256
        )
        and asset.storage_key
        == (
            f"blobs/sha256/{asset.sha256[:2]}/"
            f"{asset.sha256}"
        )
        and asset.media_type == "application/json"
        and isinstance(asset.size_bytes, int)
        and not isinstance(asset.size_bytes, bool)
        and 1 <= asset.size_bytes <= MAX_LOCAL_METADATA_BYTES
    )


def _metadata_asset_exists_without_read(
    store: ReferenceBlobStore,
    asset: ReferenceDeliveryAsset,
) -> bool:
    if not _metadata_asset_shape_is_valid(asset):
        return False
    try:
        metadata = store.resolve_blob(asset.storage_key).lstat()
    except (OSError, ReferenceBlobStoreError):
        return False
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_size == asset.size_bytes
    )


def _parse_canonical_document(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise LocalMetadataError(
            "local metadata is not strict JSON"
        ) from error
    _bounded_json_tree(value)
    if not isinstance(value, dict):
        raise LocalMetadataError(
            "local metadata root is not an object"
        )
    try:
        canonical = canonical_json_bytes(value)
    except DeliveryBuildError as error:
        raise LocalMetadataError(
            "local metadata is not canonical JSON"
        ) from error
    if canonical != body:
        raise LocalMetadataError(
            "local metadata bytes are not canonical"
        )
    _validate_document_shape(value)
    return value


def _validate_document_shape(document: dict[str, Any]) -> None:
    if (
        set(document) != _TOP_LEVEL_KEYS
        or document.get("schema_version")
        != LOCAL_METADATA_DOCUMENT_SCHEMA
    ):
        raise LocalMetadataError("local metadata schema is invalid")
    _exact_keys(
        document.get("binding"),
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
        },
        "binding",
    )
    catalog = _exact_keys(
        document.get("catalog"),
        {"snapshot", "service", "layer"},
        "catalog",
    )
    _exact_keys(
        catalog["snapshot"],
        {
            "id",
            "provider_key",
            "content_sha256",
            "definition_sha256",
            "service_count",
            "group_count",
            "layer_count",
            "unresolved_count",
        },
        "catalog.snapshot",
    )
    _exact_keys(
        catalog["service"],
        {"id", "source_key", "title", "protocol", "version"},
        "catalog.service",
    )
    _exact_keys(
        catalog["layer"],
        {
            "id",
            "source_key",
            "title",
            "description",
            "role",
            "renderer",
            "queryable",
            "downloadable",
            "bounds",
            "supported_crs",
            "min_zoom",
            "max_zoom",
        },
        "catalog.layer",
    )
    _exact_keys(
        document.get("source"),
        {
            "id",
            "source_key",
            "protocol",
            "target_kind",
            "source_format",
            "sync_strategy",
            "definition_sha256",
        },
        "source",
    )
    styles = _exact_keys(
        document.get("styles"),
        {"plan_id", "plan_evidence_sha256", "items"},
        "styles",
    )
    if not isinstance(styles["items"], list):
        raise LocalMetadataError("local metadata styles are invalid")
    for item in styles["items"]:
        style = _exact_keys(
            item,
            {
                "catalog_style_id",
                "source_key",
                "title",
                "is_default",
                "parity",
            },
            "styles.items",
        )
        _exact_keys(
            style["parity"],
            {
                "plan_item_id",
                "kind",
                "verified",
                "evidence_sha256",
                "resource_count",
                "source_style_artifact_id",
                "source_package_artifact_id",
            },
            "styles.items.parity",
        )
    provenance = _exact_keys(
        document.get("provenance"),
        {"inputs", "supporting_artifacts", "upstream_metadata"},
        "provenance",
    )
    for key in ("inputs", "supporting_artifacts", "upstream_metadata"):
        values = provenance[key]
        if not isinstance(values, list):
            raise LocalMetadataError(
                "local metadata provenance is invalid"
            )
        for value in values:
            keys = {
                "artifact_id",
                "artifact_kind",
                "media_type",
                "sha256",
                "size_bytes",
            }
            if key != "inputs":
                keys.add("role")
            _exact_keys(value, keys, f"provenance.{key}")
    delivery = _exact_keys(
        document.get("delivery"),
        {
            "kind",
            "source_version",
            "content_sha256",
            "reference_at",
            "crs",
            "bounds",
            "feature_count",
            "prepared_validation_sha256",
            "data_schema_sha256",
            "assets",
        },
        "delivery",
    )
    if not isinstance(delivery["assets"], list):
        raise LocalMetadataError("local metadata assets are invalid")
    for asset in delivery["assets"]:
        _exact_keys(
            asset,
            {
                "asset_key",
                "asset_kind",
                "is_primary",
                "media_type",
                "sha256",
                "size_bytes",
                "metadata_sha256",
            },
            "delivery.assets",
        )
        if asset.get("asset_kind") == "metadata":
            raise LocalMetadataError(
                "local metadata document references itself"
            )
    authorization = _exact_keys(
        document.get("authorization"),
        {
            "review_id",
            "document_sha256",
            "review_sha256",
            "source_definition_sha256",
            "decision",
            "reviewed_at",
            "license",
            "permissions",
        },
        "authorization",
    )
    license_value = _exact_keys(
        authorization["license"],
        {"name", "url", "terms", "attribution"},
        "authorization.license",
    )
    parsed_license = urlsplit(str(license_value["url"]))
    if (
        parsed_license.scheme != "https"
        or not parsed_license.hostname
        or parsed_license.username is not None
        or parsed_license.password is not None
    ):
        raise LocalMetadataError(
            "local metadata license URL is invalid"
        )
    _exact_keys(
        authorization["permissions"],
        {
            "metadata_probe",
            "dataset_download",
            "local_storage",
            "local_service",
            "bulk_tile_seed",
        },
        "authorization.permissions",
    )
    _reject_forbidden_public_keys(document)


def _reject_forbidden_public_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_PUBLIC_KEYS:
                raise LocalMetadataError(
                    "local metadata exposes an internal field"
                )
            _reject_forbidden_public_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_public_keys(item)


def _exact_keys(
    value: Any,
    keys: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise LocalMetadataError(
            f"local metadata {label} schema is invalid"
        )
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _bounded_json_tree(value: Any) -> None:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 100_000 or depth > 32:
            raise LocalMetadataError(
                "local metadata JSON structure is too large"
            )
        if isinstance(current, dict):
            if len(current) > 1_024:
                raise LocalMetadataError(
                    "local metadata JSON object is too large"
                )
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            if len(current) > 8_192:
                raise LocalMetadataError(
                    "local metadata JSON array is too large"
                )
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > 262_144:
            raise LocalMetadataError(
                "local metadata JSON string is too large"
            )
        elif current is not None and not isinstance(
            current,
            (bool, int, float, str),
        ):
            raise LocalMetadataError(
                "local metadata JSON value is invalid"
            )


def _required_text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
    ):
        raise LocalMetadataError(f"{label} is invalid")
    return value


def _optional_text(
    value: Any,
    label: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum)


def _required_boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise LocalMetadataError(f"{label} is invalid")
    return value


def _optional_non_negative_integer(
    value: Any,
    label: str,
) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise LocalMetadataError(f"{label} is invalid")
    return value


def _safe_string_list(
    value: Any,
    label: str,
    *,
    maximum: int,
) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 255
            for item in value
        )
    ):
        raise LocalMetadataError(f"{label} is invalid")
    return list(value)


def _normalized_bounds(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {
        "west",
        "south",
        "east",
        "north",
    }:
        raise LocalMetadataError("local metadata bounds are invalid")
    try:
        bounds = {
            key: float(value[key])
            for key in ("west", "south", "east", "north")
        }
    except (TypeError, ValueError, OverflowError) as error:
        raise LocalMetadataError(
            "local metadata bounds are invalid"
        ) from error
    if (
        not bounds["west"] < bounds["east"]
        or not bounds["south"] < bounds["north"]
    ):
        raise LocalMetadataError("local metadata bounds are invalid")
    return bounds


def _optional_bounds(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    return _normalized_bounds(value)


def _utc_isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise LocalMetadataError(
            "local metadata date is not timezone-aware"
        )
    return (
        value.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
