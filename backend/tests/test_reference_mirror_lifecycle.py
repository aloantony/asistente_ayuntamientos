from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from threading import Barrier
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.reference_layers import delivery_builder as reference_delivery_builder
from app.reference_layers import mirror_lifecycle as reference_mirror_lifecycle
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.mirror_lifecycle import (
    DeliveryPhysicalTransitionVerification,
    MirrorLeaseLostError,
    MirrorLifecycleError,
    MirrorPlanChangedError,
    MirrorPromotionConflict,
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
    claim_next_sync_run,
    deactivate_delivery,
    enqueue_due_sources as _enqueue_due_sources,
    enqueue_fallback_source,
    enqueue_manual_sync_run,
    finish_sync_run,
    heartbeat_sync_run,
    preview_manual_sync_run,
    promote_delivery_version,
    reactivate_delivery,
    rollback_delivery_version,
    stored_promotion_hash_is_valid,
)
from app.reference_layers.local_metadata import (
    _assemble_document,
    verify_local_metadata_transition,
)
from app.reference_layers.local_metadata_contract import (
    local_metadata_asset_descriptor,
    local_metadata_precommit_gate,
)
from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceDeliveryVersionArtifact,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerMirrorStrategy,
    ReferenceLayerMirrorStrategyDependency,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceService,
    ReferenceSourceArtifact,
    ReferenceStyleParityPlan,
    ReferenceStyleParityPlanItem,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.source_probes import SourceProbe
from app.reference_layers.reviewed_ortho_evidence import (
    CATALOG_ENDPOINT_URL,
    reviewed_ign_ortho_expected_source_definition,
    reviewed_ign_ortho_substitution,
)
from app.reference_layers.style_parity import persist_style_parity_plan
from app.users.models import User
from support_reference_mirror_authorization import (
    bind_run_authorization,
    ensure_authorized_mirror_source,
    supersede_mirror_authorization,
)

NOW = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)


@pytest.fixture
def metadata_store(tmp_path):
    with ReferenceBlobStore(tmp_path / "reference-data") as store:
        yield store


def _transition_verifier(store):
    return lambda transition_db, version: (
        verify_local_metadata_transition(
            store,
            transition_db,
            version,
        )
    )


def _physical_transition_verifier(db, version):
    assets = tuple(
        db.scalars(
            select(ReferenceDeliveryAsset)
            .where(ReferenceDeliveryAsset.version_id == version.id)
            .order_by(ReferenceDeliveryAsset.id)
        )
    )
    [primary] = [item for item in assets if item.is_primary]
    filesystem_ids = tuple(
        item.id
        for item in assets
        if item.storage_backend == "filesystem"
    )
    if version.delivery_kind == "tiles":
        renderer = "tile_archive"
        render_transport = "local_tile_archive"
        resource_name = None
        style_names = ()
        tile_ids = tuple(
            item.id for item in assets if item.asset_kind == "tile_archive"
        )
    else:
        renderer = "geoserver"
        render_transport = "direct_geoserver_wms"
        resource_name = primary.metadata_json["layer_name"]
        styles = set(primary.metadata_json["styles"].values())
        style_names = tuple(sorted(styles)) if styles else (None,)
        tile_ids = ()
    return DeliveryPhysicalTransitionVerification(
        version_id=version.id,
        primary_asset_id=primary.id,
        primary_sha256=primary.sha256,
        renderer=renderer,
        render_transport=render_transport,
        resource_name=resource_name,
        verified_filesystem_asset_ids=filesystem_ids,
        rendered_style_names=style_names,
        rendered_tile_asset_ids=tile_ids,
    )


def _missing_physical_transition_verifier(_db, _version):
    raise FileNotFoundError("published physical delivery is absent")


def enqueue_due_sources(db, **kwargs):
    """Exercise queue lifecycle independently from production filtering."""

    return _enqueue_due_sources(
        db,
        require_authorization=False,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("catalog_layer", "validation"),
    [
        ("Ortofoto_2021", {"passed": True}),
        ("Ortofoto_2020", {"passed": True}),
    ],
)
def test_lifecycle_fences_2021_and_legacy_ortho_versions(
    db,
    catalog_layer,
    validation,
) -> None:
    provider_key = f"ortho-lifecycle-{catalog_layer.casefold()}"
    apply_catalog_definition(
        db,
        ReferenceCatalogDefinition(
            provider_key=provider_key,
            source_url="https://example.test/ortho.json",
            raw_catalog={"revision": 1},
            services=(
                ReferenceServiceDefinition(
                    source_key="ortho",
                    title="Ortofotos",
                    upstream_protocol="wms",
                    base_url=CATALOG_ENDPOINT_URL,
                ),
            ),
            layers=(
                ReferenceLayerDefinition(
                    source_key="ortho-layer",
                    node_type="layer",
                    title=catalog_layer,
                    service_key="ortho",
                    remote_name=catalog_layer,
                    role="overlay",
                    renderer="raster_tile",
                    delivery_mode="mirror",
                ),
            ),
            retrieved_at=NOW,
        ),
    )
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.remote_name == catalog_layer,
        )
    )
    assert layer is not None
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        catalog_layer,
    )
    assert reviewed is not None
    source_definition = reviewed_ign_ortho_expected_source_definition(
        reviewed
    )

    with pytest.raises(MirrorPromotionConflict):
        reference_mirror_lifecycle._validate_reviewed_ortho_version(
            db,
            version=SimpleNamespace(
                provider_key=provider_key,
                layer_id=layer.id,
                validation_json=validation,
                content_sha256="a" * 64,
            ),
            run=SimpleNamespace(
                source_definition_json=source_definition,
            ),
            layer=layer,
        )


@pytest.fixture
def committed_reference_providers(engine):
    """Remove rows committed by concurrency tests using independent sessions."""

    provider_keys: list[str] = []
    yield provider_keys
    if not provider_keys:
        return
    with Session(engine) as cleanup:
        source_ids = select(ReferenceLayerSource.id).where(
            ReferenceLayerSource.provider_key.in_(provider_keys)
        )
        cleanup.execute(
            delete(ReferenceSyncRun).where(
                ReferenceSyncRun.source_id.in_(source_ids)
            )
        )
        strategy_ids = select(ReferenceLayerMirrorStrategy.id).where(
            ReferenceLayerMirrorStrategy.provider_key.in_(provider_keys)
        )
        cleanup.execute(
            delete(ReferenceLayerMirrorStrategyDependency).where(
                ReferenceLayerMirrorStrategyDependency.strategy_id.in_(strategy_ids)
            )
        )
        cleanup.execute(
            delete(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceLayerStyle).where(
                ReferenceLayerStyle.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceLayer).where(
                ReferenceLayer.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceService).where(
                ReferenceService.provider_key.in_(provider_keys)
            )
        )
        cleanup.execute(
            delete(ReferenceCatalogSnapshot).where(
                ReferenceCatalogSnapshot.provider_key.in_(provider_keys)
            )
        )
        cleanup.commit()


def _definition(
    *,
    provider_key: str = "mirror-lifecycle-test",
    remote_name: str = "planning:zones",
) -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://example.test/catalog.json",
        raw_catalog={"revision": remote_name},
        services=(
            ReferenceServiceDefinition(
                source_key="service:planning",
                title="Planning service",
                upstream_protocol="wms",
                base_url="https://example.test/geoserver/planning/wms",
                default_format="image/png",
                license_status="approved",
                cache_policy="mirror",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:zones",
                node_type="layer",
                title="Planning zones",
                service_key="service:planning",
                remote_name=remote_name,
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                image_format="image/png",
                bounds={
                    "west": -7.1,
                    "south": 40.0,
                    "east": -1.7,
                    "north": 43.3,
                },
                min_zoom=6,
                max_zoom=18,
                style_name="style:default",
                styles=(
                    ReferenceLayerStyleDefinition(
                        source_key="style:default",
                        title="Default",
                        remote_name="planning:default",
                        is_default=True,
                    ),
                ),
            ),
        ),
        retrieved_at=NOW,
    )


def _seed_bootstrap(db, *, provider_key: str = "mirror-lifecycle-test"):
    definition = _definition(provider_key=provider_key)
    apply_catalog_definition(db, definition)
    plan = build_mirror_bootstrap_plan(db, provider_key=provider_key)
    applied = apply_mirror_bootstrap_plan(db, plan)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.source_key == "layer:zones",
        )
    )
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )
    sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.provider_key == provider_key)
            .order_by(ReferenceLayerSource.priority)
        )
    )
    return definition, layer, snapshot, sources, applied


def _idecyl_reviewable_archive_definition(
    *,
    provider_key: str = "idecyl-reviewable-archives",
) -> ReferenceCatalogDefinition:
    return _idecyl_candidate_definition(
        provider_key=provider_key,
        protocol="download",
        expected_count=18,
        revision="reviewable-archives-v3",
    )


def _idecyl_wfs_candidate_definition(
    *,
    provider_key: str = "idecyl-wfs-candidates",
) -> ReferenceCatalogDefinition:
    return _idecyl_candidate_definition(
        provider_key=provider_key,
        protocol="wfs",
        expected_count=10,
        revision="wfs-snapshots-v4",
    )


def _idecyl_candidate_definition(
    *,
    provider_key: str,
    protocol: str,
    expected_count: int,
    revision: str,
) -> ReferenceCatalogDefinition:
    reviewed = [
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id != 39
        and item.local_service_status == "candidate"
        and item.protocol == protocol
    ]
    assert len(reviewed) == expected_count
    return ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://idecyl.jcyl.es/siur/settings.json",
        raw_catalog={"revision": revision},
        services=tuple(
            ReferenceServiceDefinition(
                source_key=f"service:idecyl:{item.audit_layer_id}",
                title=f"IDECyL {item.audit_layer_id}",
                upstream_protocol="wms",
                base_url=item.catalog_endpoint_url,
                default_format="image/png",
                license_status="pending",
                cache_policy="mirror",
            )
            for item in reviewed
        ),
        layers=tuple(
            ReferenceLayerDefinition(
                source_key=item.catalog_layer_source_key,
                node_type="layer",
                title=item.catalog_remote_name,
                service_key=f"service:idecyl:{item.audit_layer_id}",
                remote_name=item.catalog_remote_name,
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                image_format="image/png",
                bounds={
                    "west": -7.1,
                    "south": 40.0,
                    "east": -1.7,
                    "north": 43.3,
                },
                min_zoom=6,
                max_zoom=18,
            )
            for item in reviewed
        ),
        retrieved_at=NOW,
    )


def _only_source_due(db, source, *, at: datetime = NOW):
    for item in db.scalars(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.provider_key == source.provider_key
        )
    ):
        item.enabled = item.id == source.id
        item.is_primary = False
        item.next_check_at = at - timedelta(seconds=1)
    db.flush()
    source.is_primary = True
    db.commit()


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _create_style_parity_plan(
    db,
    *,
    source,
    snapshot,
    lease,
    sequence_number: int,
):
    style = db.scalar(
        select(ReferenceLayerStyle).where(
            ReferenceLayerStyle.provider_key == source.provider_key,
            ReferenceLayerStyle.layer_id == source.layer_id,
            ReferenceLayerStyle.source_key == "style:default",
            ReferenceLayerStyle.last_seen_snapshot_id == snapshot.id,
            ReferenceLayerStyle.status.in_(("active", "degraded")),
        )
    )
    assert style is not None

    artifacts = ()
    style_artifact = None
    probe = None
    if source.target_kind in {"vector", "raster"}:
        digest = hashlib.sha256(
            (
                f"lifecycle-style-{source.id}-{lease.run_id}-"
                f"{sequence_number}"
            ).encode()
        ).hexdigest()
        style_artifact = ReferenceSourceArtifact(
            source_id=source.id,
            artifact_kind="style",
            source_version=f"2026-07-{20 + sequence_number}",
            media_type="application/vnd.ogc.sld+xml",
            storage_backend="filesystem",
            storage_key=f"blobs/sha256/{digest[:2]}/{digest}",
            size_bytes=256,
            sha256=digest,
            metadata_json={
                "catalog_style_source_key": style.source_key,
                "remote_name": style.remote_name,
                "parity_kind": "exact",
                "resource_bindings": [],
                "unresolved_resources": [],
            },
            retrieved_at=NOW + timedelta(seconds=sequence_number),
        )
        db.add(style_artifact)
        db.flush()
        db.add(
            ReferenceSyncRunArtifact(
                source_id=source.id,
                run_id=lease.run_id,
                artifact_id=style_artifact.id,
                role="style",
            )
        )
        db.flush()
        artifacts = (
            SimpleNamespace(
                artifact_id=style_artifact.id,
                artifact_kind=style_artifact.artifact_kind,
                roles=frozenset({"style"}),
                media_type=style_artifact.media_type,
                storage_backend=style_artifact.storage_backend,
                storage_key=style_artifact.storage_key,
                size_bytes=style_artifact.size_bytes,
                sha256=style_artifact.sha256,
                metadata_json=style_artifact.metadata_json,
            ),
        )
    else:
        advertised_styles = (
            [{"name": style.remote_name}]
            if source.protocol == "wmts"
            else [style.remote_name]
        )
        probe = SourceProbe(
            available=True,
            protocol=source.protocol,
            requested_name=source.remote_name or "",
            canonical_name=source.remote_name,
            service_version="1.0.0",
            fingerprint_sha256=hashlib.sha256(
                (
                    f"lifecycle-probe-{source.id}-{lease.run_id}-"
                    f"{sequence_number}"
                ).encode()
            ).hexdigest(),
            fingerprint_quality="strong",
            metadata={"styles": advertised_styles},
        )

    result = persist_style_parity_plan(
        db,
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        catalog_snapshot_id=snapshot.id,
        source_id=source.id,
        sync_run_id=lease.run_id,
        delivery_kind=source.target_kind,
        styles=(style,),
        artifacts=artifacts,
        probe=probe,
        now=NOW + timedelta(seconds=sequence_number),
    )
    assert result.complete is True
    plan = db.get(ReferenceStyleParityPlan, result.plan_id)
    items = tuple(
        db.scalars(
            select(ReferenceStyleParityPlanItem)
            .where(ReferenceStyleParityPlanItem.plan_id == plan.id)
            .order_by(ReferenceStyleParityPlanItem.id)
        )
    )
    assert len(items) == 1
    return style, style_artifact, plan, items


def _create_version(
    db,
    *,
    store,
    source,
    snapshot,
    lease,
    sequence_number: int,
    include_metadata: bool = True,
    include_metadata_gate: bool = True,
    duplicate_metadata: bool = False,
):
    review = ensure_authorized_mirror_source(
        db,
        source,
        reviewed_at=NOW + timedelta(seconds=sequence_number),
    )
    bind_run_authorization(db, source, lease.run_id)
    style, style_artifact, plan, plan_items = _create_style_parity_plan(
        db,
        source=source,
        snapshot=snapshot,
        lease=lease,
        sequence_number=sequence_number,
    )
    prepared_validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": source.target_kind,
        "checks": {
            "data_schema": {
                "schema_version": "reference-test-schema/v1",
            },
        },
    }
    content_sha256 = f"{sequence_number + 1:x}" * 64
    asset_kind = {
        "vector": "vector_table",
        "raster": "raster_cog",
        "tiles": "tile_archive",
    }[source.target_kind]
    storage_backend = (
        "postgres"
        if source.target_kind == "vector"
        else "filesystem"
    )
    prepared_assets = [
        reference_delivery_builder.PreparedDeliveryAsset(
            asset_key="primary",
            asset_kind=asset_kind,
            is_primary=True,
            storage_backend=storage_backend,
            storage_key=f"mirror/{source.id}/v{sequence_number}",
            media_type="application/octet-stream",
            sha256=content_sha256,
            size_bytes=(
                None if storage_backend == "postgres" else 1024
            ),
            metadata_json=(
                {
                    "renderer": "tile_archive",
                    "catalog_style_source_key": style.source_key,
                }
                if source.target_kind == "tiles"
                else {
                    "renderer": "geoserver",
                    "layer_name": (
                        f"siur_layer_{source.layer_id}_v_"
                        f"{content_sha256[:12]}"
                    ),
                    "default_style_name": "siur_style_default_v1",
                    "styles": {
                        str(style.id): "siur_style_default_v1",
                    },
                    "identify_available": source.target_kind == "vector",
                    "legend_available": True,
                }
            ),
        )
    ]
    artifact_links: tuple[tuple[int, str], ...] = ()
    artifacts = {}
    if style_artifact is not None:
        prepared_assets.append(
            reference_delivery_builder.PreparedDeliveryAsset(
                asset_key="style-default",
                asset_kind="style_sld",
                is_primary=False,
                storage_backend=style_artifact.storage_backend,
                storage_key=style_artifact.storage_key,
                media_type=style_artifact.media_type,
                sha256=style_artifact.sha256,
                size_bytes=style_artifact.size_bytes,
                metadata_json={
                    "catalog_style_id": style.id,
                    "catalog_style_source_key": style.source_key,
                    "style_name": "siur_style_default_v1",
                    "parity_kind": "exact",
                    "effective": True,
                },
            )
        )
        artifact_links = ((style_artifact.id, "style"),)
        artifacts = {style_artifact.id: style_artifact}
    prepared = reference_delivery_builder.PreparedDelivery(
        delivery_kind=source.target_kind,
        source_version=f"2026-07-{20 + sequence_number}",
        content_sha256=content_sha256,
        reference_at=NOW,
        crs="EPSG:3857",
        bounds_json={
            "west": -7.1,
            "south": 40.0,
            "east": -1.7,
            "north": 43.3,
        },
        feature_count=10 * sequence_number,
        validation_json=prepared_validation,
        input_artifact_ids=(),
        assets=tuple(prepared_assets),
        supporting_artifacts=artifact_links,
    )
    validation = {
        **prepared_validation,
        "continuity_gate": {"passed": True},
        "style_parity_gate": {
            "schema_version": "reference-delivery-style-parity-gate/v1",
            "passed": True,
            "plan_id": plan.id,
            "plan_evidence_sha256": plan.evidence_sha256,
            "required_style_count": len(plan_items),
            "verified_style_count": len(plan_items),
            "missing_style_count": 0,
        },
    }
    metadata_prepared = None
    if include_metadata:
        layer = db.get(ReferenceLayer, source.layer_id)
        service = db.get(ReferenceService, layer.service_id)
        document = _assemble_document(
            prepared=prepared,
            source=source,
            run=db.get(ReferenceSyncRun, lease.run_id),
            layer=layer,
            service=service,
            snapshot=snapshot,
            review=review,
            plan=plan,
            plan_items=plan_items,
            styles=(style,),
            artifacts=artifacts,
            artifact_links=artifact_links,
        )
        body = reference_delivery_builder.canonical_json_bytes(
            document
        )
        blob = store.put_stream(
            io.BytesIO(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_size=len(body),
        )
        descriptor = local_metadata_asset_descriptor(
            document_sha256=blob.sha256,
            document_size_bytes=blob.size_bytes,
            binding=document["binding"],
        )
        metadata_prepared = (
            reference_delivery_builder.PreparedDeliveryAsset(
                asset_key="metadata",
                asset_kind="metadata",
                is_primary=False,
                storage_backend=blob.storage_backend,
                storage_key=blob.storage_key,
                media_type="application/json",
                sha256=blob.sha256,
                size_bytes=blob.size_bytes,
                metadata_json=descriptor,
            )
        )
        if include_metadata_gate:
            validation["local_metadata_gate"] = (
                local_metadata_precommit_gate(
                    document_sha256=blob.sha256,
                    document_size_bytes=blob.size_bytes,
                    descriptor=descriptor,
                    binding=document["binding"],
                )
            )
    version = ReferenceDeliveryVersion(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        sync_run_id=lease.run_id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        mirror_authorization_review_id=review.id,
        mirror_authorization_review_sha256=review.review_sha256,
        sequence_number=sequence_number,
        delivery_kind=source.target_kind,
        source_version=f"2026-07-{20 + sequence_number}",
        content_sha256=content_sha256,
        manifest_sha256=f"{sequence_number + 3:x}" * 64,
        validation_sha256=_canonical_sha256(validation),
        reference_at=NOW,
        crs="EPSG:3857",
        bounds_json={
            "west": -7.1,
            "south": 40.0,
            "east": -1.7,
            "north": 43.3,
        },
        feature_count=10 * sequence_number,
        validation_json=validation,
        created_at=NOW + timedelta(minutes=sequence_number),
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
            for artifact_id, role in artifact_links
        ]
    )
    stored_assets = [
        *prepared_assets,
        *(
            [metadata_prepared]
            if metadata_prepared is not None
            else []
        ),
        *(
            [
                replace(
                    metadata_prepared,
                    asset_key="metadata-copy",
                )
            ]
            if duplicate_metadata
            and metadata_prepared is not None
            else []
        ),
    ]
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
        )
        for asset in stored_assets
    ]
    db.add_all(delivery_assets)
    db.flush()
    reference_delivery_builder._append_delivery_style_parity(
        db,
        version=version,
        plan=plan,
        items=plan_items,
        assets=delivery_assets,
        now=NOW + timedelta(minutes=sequence_number),
    )
    db.commit()
    return version


def _tamper_metadata_blob(db, store, version, tamper_kind):
    metadata = db.scalar(
        select(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.version_id == version.id,
            ReferenceDeliveryAsset.asset_kind == "metadata",
        )
    )
    assert metadata is not None
    path = store.resolve_blob(metadata.storage_key)
    if tamper_kind == "delete":
        path.unlink()
        return
    body = path.read_bytes()
    path.write_bytes(bytes([body[0] ^ 1]) + body[1:])


def _promote_cross_snapshot_versions(db, store):
    _, layer, snapshot_v1, sources, _ = _seed_bootstrap(db)
    source_v1 = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source_v1)
    enqueue_due_sources(db, now=NOW)
    lease_v1 = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "1" * 64,
    )
    version_v1 = _create_version(
        db,
        store=store,
        source=source_v1,
        snapshot=snapshot_v1,
        lease=lease_v1,
        sequence_number=1,
    )
    promote_delivery_version(
        db,
        version_id=version_v1.id,
        lease=lease_v1,
        expected_generation=0,
        reason="catalog v1",
        metadata_verifier=_transition_verifier(store),
        now=NOW + timedelta(seconds=1),
    )

    snapshot_v2, _ = apply_catalog_definition(
        db,
        _definition(remote_name="planning:zones-v2"),
    )
    apply_mirror_bootstrap_plan(
        db,
        build_mirror_bootstrap_plan(
            db,
            provider_key=layer.provider_key,
        ),
    )
    source_v2 = db.scalar(
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.provider_key == layer.provider_key,
            ReferenceLayerSource.target_kind == "vector",
            ReferenceLayerSource.enabled.is_(True),
        )
        .order_by(
            ReferenceLayerSource.is_primary.desc(),
            ReferenceLayerSource.priority,
            ReferenceLayerSource.id,
        )
    )
    assert source_v2 is not None
    _only_source_due(
        db,
        source_v2,
        at=NOW + timedelta(seconds=2),
    )
    enqueue_due_sources(db, now=NOW + timedelta(seconds=2))
    lease_v2 = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "2" * 64,
    )
    version_v2 = _create_version(
        db,
        store=store,
        source=source_v2,
        snapshot=snapshot_v2,
        lease=lease_v2,
        sequence_number=2,
    )
    promote_delivery_version(
        db,
        version_id=version_v2.id,
        lease=lease_v2,
        expected_generation=1,
        reason="catalog v2",
        metadata_verifier=_transition_verifier(store),
        now=NOW + timedelta(seconds=3),
    )
    return (
        layer,
        snapshot_v1,
        snapshot_v2,
        version_v1,
        version_v2,
    )


def test_bootstrap_is_dry_run_idempotent_and_preserves_manual_sources(db) -> None:
    definition = _definition()
    apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == definition.provider_key,
            ReferenceLayer.source_key == "layer:zones",
        )
    )
    manual = ReferenceLayerSource(
        provider_key=definition.provider_key,
        layer_id=layer.id,
        source_key="operator:manual",
        protocol="local",
        target_kind="vector",
        endpoint_url=None,
        remote_name="zones",
        sync_strategy="manual",
        config_json={},
        definition_sha256="a" * 64,
        enabled=False,
        is_primary=False,
        priority=1,
        next_check_at=NOW,
    )
    db.add(manual)
    db.commit()

    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )

    assert len(plan.sources) == 3
    assert len(plan.new_source_keys) == 3
    assert db.scalar(
        select(func.count(ReferenceLayerSource.id)).where(
            ReferenceLayerSource.source_key.like("auto:%")
        )
    ) == 0

    result = apply_mirror_bootstrap_plan(db, plan)
    assert result.created_count == 3
    assert result.updated_count == 0
    assert db.scalar(
        select(func.count(ReferenceLayerMirrorStrategy.id)).where(
            ReferenceLayerMirrorStrategy.provider_key
            == definition.provider_key
        )
    ) == 1
    auto_sources = list(
        db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.source_key.like("auto:%")
            )
        )
    )
    assert all(source.enabled for source in auto_sources)
    primary = [source for source in auto_sources if source.is_primary]
    assert len(primary) == 1
    assert primary[0].id == min(
        auto_sources,
        key=lambda item: (item.priority, item.source_key),
    ).id

    repeated = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    assert repeated.new_source_keys == ()
    assert repeated.updated_source_keys == ()
    assert repeated.deactivated_source_keys == ()
    assert repeated.unchanged_count == 3
    no_op = apply_mirror_bootstrap_plan(db, repeated)
    assert no_op.created_count == no_op.updated_count == 0
    assert db.get(ReferenceLayerSource, manual.id).enabled is False
    strategy_rows = list(
        db.scalars(
            select(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key
                == definition.provider_key
            )
        )
    )
    assert len(strategy_rows) == 1
    assert strategy_rows[0].generation == 1


def test_18_idecyl_archives_persist_enabled_but_cannot_queue_without_review(
    db,
) -> None:
    definition = _idecyl_reviewable_archive_definition()
    apply_catalog_definition(db, definition)
    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )

    assert len(plan.sources) == 18
    assert len(plan.new_source_keys) == 18
    applied = apply_mirror_bootstrap_plan(db, plan)
    assert applied.created_count == 18
    sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(
                ReferenceLayerSource.provider_key
                == definition.provider_key
            )
            .order_by(ReferenceLayerSource.layer_id)
        )
    )
    assert len(sources) == 18
    assert all(
        source.enabled
        and source.is_primary
        and source.protocol == "download"
        and source.target_kind == "vector"
        and source.definition_sha256
        == _canonical_sha256(
            {
                "protocol": source.protocol,
                "target_kind": source.target_kind,
                "endpoint_url": source.endpoint_url,
                "remote_name": source.remote_name,
                "sync_strategy": source.sync_strategy,
                "priority": source.priority,
                "config": source.config_json,
            }
        )
        for source in sources
    )
    due = NOW - timedelta(seconds=1)
    for source in sources:
        source.next_check_at = due
    db.commit()

    assert (
        reference_mirror_lifecycle.enqueue_due_sources(
            db,
            now=NOW,
            require_authorization=True,
        )
        == ()
    )
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.provider_key == definition.provider_key
        )
    ) == 0
    assert all(
        db.get(ReferenceLayerSource, source.id).next_check_at == due
        for source in sources
    )
    strategies = list(
        db.scalars(
            select(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key
                == definition.provider_key
            )
        )
    )
    assert len(strategies) == 18
    assert all(
        strategy.strategy == "vector"
        and strategy.generation == 1
        for strategy in strategies
    )

    repeated = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    assert repeated.new_source_keys == ()
    assert repeated.updated_source_keys == ()
    no_op = apply_mirror_bootstrap_plan(db, repeated)
    assert no_op.created_count == no_op.updated_count == 0
    assert {
        strategy.generation
        for strategy in db.scalars(
            select(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key
                == definition.provider_key
            )
        )
    } == {1}


def test_10_idecyl_wfs_sources_persist_but_cannot_queue_without_review(
    db,
) -> None:
    definition = _idecyl_wfs_candidate_definition()
    apply_catalog_definition(db, definition)
    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )

    assert len(plan.sources) == 10
    assert len(plan.new_source_keys) == 10
    applied = apply_mirror_bootstrap_plan(db, plan)
    assert applied.created_count == 10
    sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(
                ReferenceLayerSource.provider_key
                == definition.provider_key
            )
            .order_by(ReferenceLayerSource.layer_id)
        )
    )
    assert len(sources) == 10
    assert all(
        source.enabled
        and source.is_primary
        and source.protocol == "wfs"
        and source.target_kind == "vector"
        and source.sync_strategy == "full_snapshot"
        and source.config_json["reviewed_equivalence"][
            "license_evidence"
        ]["authorization_granted"]
        is False
        and source.config_json["reviewed_equivalence"][
            "license_evidence"
        ]["required_attribution"]
        == "© Junta de Castilla y León"
        for source in sources
    )
    assert {
        source.config_json["wfs_snapshot"]["mode"]
        for source in sources
    } == {"single_response", "paged"}
    assert sum("page_size" in source.config_json for source in sources) == 2

    due = NOW - timedelta(seconds=1)
    for source in sources:
        source.next_check_at = due
    db.commit()

    assert (
        reference_mirror_lifecycle.enqueue_due_sources(
            db,
            now=NOW,
            require_authorization=True,
        )
        == ()
    )
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.provider_key == definition.provider_key
        )
    ) == 0
    assert all(
        db.get(ReferenceLayerSource, source.id).next_check_at == due
        for source in sources
    )


def test_bootstrap_rejects_stale_plan_and_deactivates_superseded_auto_sources(
    db,
) -> None:
    definition, _, _, old_sources, _ = _seed_bootstrap(db)
    stale = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    old_sources[0].next_check_at = NOW + timedelta(days=2)
    db.commit()
    with pytest.raises(MirrorPlanChangedError):
        apply_mirror_bootstrap_plan(db, stale)

    changed = replace(
        definition,
        raw_catalog={"revision": 2},
        layers=(
            replace(
                definition.layers[0],
                remote_name="planning:updated-zones",
            ),
        ),
        retrieved_at=NOW + timedelta(days=1),
    )
    apply_catalog_definition(db, changed)
    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    assert len(plan.new_source_keys) == 3
    assert len(plan.deactivated_source_keys) == 3
    result = apply_mirror_bootstrap_plan(db, plan)
    assert result.created_count == 3
    assert result.deactivated_count == 3
    assert all(
        not db.get(ReferenceLayerSource, item.id).enabled
        for item in old_sources
    )


def test_due_sources_enqueue_once_with_frozen_definition_and_generation(db) -> None:
    _, layer, _, sources, _ = _seed_bootstrap(db)
    for source in sources:
        source.next_check_at = NOW - timedelta(seconds=1)
    db.commit()

    run_ids = enqueue_due_sources(db, now=NOW)
    assert len(run_ids) == 1
    assert enqueue_due_sources(db, now=NOW) == ()
    runs = list(
        db.scalars(
            select(ReferenceSyncRun)
            .where(ReferenceSyncRun.id.in_(run_ids))
            .order_by(ReferenceSyncRun.id)
        )
    )
    assert all(run.status == "queued" for run in runs)
    assert all(run.check_mode == "full" for run in runs)
    assert all(run.expected_active_generation == 0 for run in runs)
    assert all(run.parent_run_id is None for run in runs)
    assert all(run.fallback_depth == 0 for run in runs)
    assert all(
        db.get(ReferenceLayerSource, run.source_id).is_primary
        for run in runs
    )
    assert all(
        _canonical_sha256(run.source_definition_json)
        == run.source_definition_sha256
        for run in runs
    )
    assert db.get(ReferenceLayer, layer.id) is not None


def test_manual_sync_preview_is_read_only_and_apply_is_exact(
    db,
    make_user,
) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.is_primary)
    ensure_authorized_mirror_source(db, source, reviewed_at=NOW)
    actor = make_user()
    actor_id = actor.id
    source_id = source.id
    source_hash = source.definition_sha256
    scheduled_at = source.next_check_at
    arguments = {
        "provider_key": source.provider_key,
        "source_id": source_id,
        "expected_source_definition_sha256": source_hash,
        "expected_generation": 0,
        "check_mode": "full",
        "requested_by_id": actor_id,
        "reason": "representative raster acceptance check",
    }

    preview = preview_manual_sync_run(db, **arguments)

    assert preview.run_id is None
    assert preview.layer_id == source.layer_id
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.source_id == source_id
        )
    ) == 0
    assert db.get(ReferenceLayerSource, source_id).next_check_at == scheduled_at

    applied = enqueue_manual_sync_run(db, now=NOW, **arguments)

    assert applied.run_id is not None
    run = db.get(ReferenceSyncRun, applied.run_id)
    assert run is not None
    assert run.trigger_kind == "manual"
    assert run.check_mode == "full"
    assert run.requested_by_id == actor_id
    assert run.expected_active_generation == 0
    assert run.source_definition_sha256 == source_hash
    assert run.stats_json == {
        "manual_enqueue": {
            "reason": "representative raster acceptance check",
            "requested_by_id": actor_id,
        }
    }
    assert db.get(ReferenceLayerSource, source_id).next_check_at == scheduled_at


def test_manual_sync_rejects_stale_unauthorized_and_open_requests(
    db,
    make_user,
) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.is_primary)
    actor_id = make_user().id
    base = {
        "provider_key": source.provider_key,
        "source_id": source.id,
        "expected_source_definition_sha256": source.definition_sha256,
        "expected_generation": 0,
        "check_mode": "full",
        "requested_by_id": actor_id,
        "reason": "bounded operator request",
    }

    with pytest.raises(
        MirrorLifecycleError,
        match="mirror_authorization_missing",
    ):
        enqueue_manual_sync_run(db, **base)
    ensure_authorized_mirror_source(db, source, reviewed_at=NOW)

    with pytest.raises(MirrorLifecycleError, match="hash is stale"):
        enqueue_manual_sync_run(
            db,
            **{
                **base,
                "expected_source_definition_sha256": "0" * 64,
            },
        )
    with pytest.raises(MirrorPromotionConflict, match="generation is stale"):
        enqueue_manual_sync_run(
            db,
            **{**base, "expected_generation": 1},
        )

    first = enqueue_manual_sync_run(db, now=NOW, **base)
    assert first.run_id is not None
    with pytest.raises(MirrorLifecycleError, match="already has an open run"):
        enqueue_manual_sync_run(db, **base)
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.source_id == source.id
        )
    ) == 1

    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "9" * 64,
    )
    assert lease.run_id == first.run_id
    finish_sync_run(
        db,
        lease,
        outcome="failed",
        error_code="manual_probe_failed",
        stats_json={"probe_phase": "download"},
        now=NOW + timedelta(seconds=1),
    )
    terminal = db.get(ReferenceSyncRun, first.run_id)
    assert terminal.stats_json == {
        "manual_enqueue": {
            "reason": "bounded operator request",
            "requested_by_id": actor_id,
        },
        "probe_phase": "download",
    }
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.parent_run_id == first.run_id
        )
    ) == 0
    assert enqueue_fallback_source(
        db,
        failed_run_id=first.run_id,
        now=NOW + timedelta(seconds=2),
    ) is None


def test_manual_sync_actor_deletion_keeps_durable_audit_and_liveness(
    db,
    make_user,
) -> None:
    _, _, _, sources, _ = _seed_bootstrap(
        db,
        provider_key="manual-sync-deleted-actor-test",
    )
    source = next(item for item in sources if item.is_primary)
    ensure_authorized_mirror_source(db, source, reviewed_at=NOW)
    actor_id = make_user().id
    queued = enqueue_manual_sync_run(
        db,
        provider_key=source.provider_key,
        source_id=source.id,
        expected_source_definition_sha256=source.definition_sha256,
        expected_generation=0,
        check_mode="full",
        requested_by_id=actor_id,
        reason="audit survives actor deletion",
        now=NOW,
    )
    db.execute(delete(User).where(User.id == actor_id))
    db.commit()
    run = db.get(ReferenceSyncRun, queued.run_id)
    assert run.requested_by_id is None

    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "a" * 64,
    )
    finish_sync_run(
        db,
        lease,
        outcome="unchanged",
        stats_json={"result": "not_modified"},
        now=NOW + timedelta(seconds=1),
    )

    terminal = db.get(ReferenceSyncRun, queued.run_id)
    assert terminal.status == "unchanged"
    assert terminal.requested_by_id is None
    assert terminal.stats_json == {
        "manual_enqueue": {
            "reason": "audit survives actor deletion",
            "requested_by_id": actor_id,
        },
        "result": "not_modified",
    }


def test_failed_sources_fall_back_in_priority_order_and_exhaust_once(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    primary = next(source for source in sources if source.is_primary)
    primary.next_check_at = NOW - timedelta(seconds=1)
    db.commit()

    [root_id] = enqueue_due_sources(db, now=NOW)
    root_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "1" * 64,
    )
    finish_sync_run(
        db,
        root_lease,
        outcome="failed",
        error_code="primary_failed",
        now=NOW + timedelta(seconds=1),
    )
    first_child = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.parent_run_id == root_id
        )
    )
    assert first_child is not None
    assert first_child.fallback_depth == 1
    assert db.get(ReferenceLayerSource, first_child.source_id).priority == 30
    assert enqueue_fallback_source(
        db,
        failed_run_id=root_id,
        now=NOW + timedelta(seconds=2),
    ) == first_child.id

    first_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "2" * 64,
    )
    assert first_lease.run_id == first_child.id
    finish_sync_run(
        db,
        first_lease,
        outcome="rejected",
        error_code="fallback_rejected",
        now=NOW + timedelta(seconds=3),
    )
    second_child = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.parent_run_id == first_child.id
        )
    )
    assert second_child is not None
    assert second_child.fallback_depth == 2
    assert db.get(ReferenceLayerSource, second_child.source_id).priority == 90

    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=4),
        token_factory=lambda: "3" * 64,
    )
    assert second_lease.run_id == second_child.id
    finish_sync_run(
        db,
        second_lease,
        outcome="failed",
        error_code="fallback_exhausted",
        now=NOW + timedelta(seconds=5),
    )
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.provider_key == primary.provider_key,
            ReferenceSyncRun.layer_id == primary.layer_id,
        )
    ) == 3
    assert enqueue_fallback_source(
        db,
        failed_run_id=second_child.id,
        now=NOW + timedelta(seconds=6),
    ) is None
    assert next(source for source in sources if source.is_primary).id == primary.id


def test_successful_fallback_keeps_preferred_daily_source(
    db,
    metadata_store,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    primary = next(source for source in sources if source.is_primary)
    primary.next_check_at = NOW - timedelta(seconds=1)
    db.commit()

    enqueue_due_sources(db, now=NOW)
    primary_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "4" * 64,
    )
    finish_sync_run(
        db,
        primary_lease,
        outcome="failed",
        error_code="primary_failed",
        now=NOW + timedelta(seconds=1),
    )
    fallback_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "5" * 64,
    )
    fallback_source = db.get(ReferenceLayerSource, fallback_lease.source_id)
    assert fallback_source.id != primary.id
    fallback_version = _create_version(
        db,
        store=metadata_store,
        source=fallback_source,
        snapshot=snapshot,
        lease=fallback_lease,
        sequence_number=1,
    )
    promote_delivery_version(
        db,
        version_id=fallback_version.id,
        lease=fallback_lease,
        expected_generation=0,
        reason="validated fallback delivery",
        metadata_verifier=_transition_verifier(metadata_store),
        now=NOW + timedelta(seconds=3),
    )

    assert db.get(ReferenceLayerSource, primary.id).is_primary is True
    assert db.get(ReferenceLayerSource, fallback_source.id).is_primary is False
    [daily_id] = enqueue_due_sources(
        db,
        now=NOW + timedelta(days=1, seconds=1),
    )
    daily = db.get(ReferenceSyncRun, daily_id)
    assert daily.source_id == primary.id
    assert daily.parent_run_id is None
    assert daily.fallback_depth == 0
    assert daily.expected_active_generation == 1
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == fallback_version.id


def test_concurrent_fallback_enqueue_is_linear_and_idempotent(
    engine,
    committed_reference_providers,
) -> None:
    provider_key = "mirror-fallback-race-test"
    committed_reference_providers.append(provider_key)
    current = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as setup:
        _, _, _, sources, _ = _seed_bootstrap(
            setup,
            provider_key=provider_key,
        )
        primary = next(source for source in sources if source.is_primary)
        _only_source_due(setup, primary, at=current)
        enqueue_due_sources(setup, now=current)
        lease = claim_next_sync_run(
            setup,
            now=current,
            token_factory=lambda: "6" * 64,
        )
        finish_sync_run(
            setup,
            lease,
            outcome="failed",
            error_code="primary_failed",
            now=current + timedelta(seconds=1),
        )
        assert setup.scalar(
            select(func.count(ReferenceSyncRun.id)).where(
                ReferenceSyncRun.parent_run_id == lease.run_id
            )
        ) == 0
        for source in sources:
            source.enabled = True
        setup.commit()

    barrier = Barrier(2)

    def enqueue_concurrently() -> int | None:
        with Session(engine, expire_on_commit=False) as worker:
            barrier.wait(timeout=5)
            return enqueue_fallback_source(
                worker,
                failed_run_id=lease.run_id,
                now=current + timedelta(seconds=2),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue_concurrently(), range(2)))
    assert results[0] is not None and results[0] == results[1]
    with Session(engine) as check:
        assert check.scalar(
            select(func.count(ReferenceSyncRun.id)).where(
                ReferenceSyncRun.parent_run_id == lease.run_id
            )
        ) == 1
        assert check.scalar(
            select(func.count(ReferenceSyncRun.id)).where(
                ReferenceSyncRun.provider_key == provider_key,
                ReferenceSyncRun.status.in_(("queued", "running")),
            )
        ) == 1


def test_concurrent_schedulers_enqueue_only_one_primary_run_per_layer(
    engine,
    committed_reference_providers,
) -> None:
    provider_key = "mirror-scheduler-race-test"
    committed_reference_providers.append(provider_key)
    current = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as setup:
        _, _, _, sources, _ = _seed_bootstrap(
            setup,
            provider_key=provider_key,
        )
        primary = next(source for source in sources if source.is_primary)
        primary.next_check_at = current - timedelta(seconds=1)
        setup.commit()

    barrier = Barrier(2)

    def schedule_concurrently() -> tuple[int, ...]:
        with Session(engine, expire_on_commit=False) as worker:
            barrier.wait(timeout=5)
            return enqueue_due_sources(worker, now=current)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: schedule_concurrently(), range(2)))
    assert sorted(len(result) for result in results) == [0, 1]
    with Session(engine) as check:
        [run] = list(
            check.scalars(
                select(ReferenceSyncRun).where(
                    ReferenceSyncRun.provider_key == provider_key,
                    ReferenceSyncRun.status.in_(("queued", "running")),
                )
            )
        )
        assert run.source_id == primary.id
        assert run.parent_run_id is None


def test_claim_skips_a_queued_run_locked_by_another_worker(
    engine,
    committed_reference_providers,
) -> None:
    provider_keys = (
        "mirror-skip-locked-test-a",
        "mirror-skip-locked-test-b",
    )
    committed_reference_providers.extend(provider_keys)
    with Session(engine, expire_on_commit=False) as setup:
        for provider_key in provider_keys:
            _seed_bootstrap(
                setup,
                provider_key=provider_key,
            )
        for primary in setup.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key.in_(provider_keys),
                ReferenceLayerSource.is_primary.is_(True),
            )
        ):
            primary.next_check_at = NOW - timedelta(seconds=1)
        setup.commit()
        run_ids = enqueue_due_sources(setup, now=NOW)
        assert len(run_ids) == 2

    with Session(engine, expire_on_commit=False) as locker:
        locked_id = locker.scalar(
            select(ReferenceSyncRun.id)
            .where(ReferenceSyncRun.id.in_(run_ids))
            .order_by(ReferenceSyncRun.id)
            .limit(1)
            .with_for_update()
        )
        with Session(engine, expire_on_commit=False) as worker:
            lease = claim_next_sync_run(
                worker,
                now=NOW,
                token_factory=lambda: "9" * 64,
            )
            assert lease is not None
            assert lease.run_id != locked_id
        locker.rollback()

    with Session(engine, expire_on_commit=False) as cleanup:
        finish_sync_run(
            cleanup,
            lease,
            outcome="unchanged",
            now=NOW + timedelta(seconds=1),
        )
        remaining = claim_next_sync_run(
            cleanup,
            now=NOW + timedelta(seconds=1),
            token_factory=lambda: "a" * 64,
        )
        assert remaining is not None and remaining.run_id == locked_id
        finish_sync_run(
            cleanup,
            remaining,
            outcome="unchanged",
            now=NOW + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    [
        ("unchanged", None),
        ("succeeded", None),
        ("rejected", "validation_failed"),
        ("failed", "upstream_timeout"),
    ],
)
def test_claim_heartbeat_and_all_worker_terminal_outcomes(
    db,
    outcome: str,
    error_code: str | None,
) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    source = sources[0]
    _only_source_due(db, source)
    [run_id] = enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=60,
        token_factory=lambda: "1" * 64,
    )
    assert lease is not None and lease.run_id == run_id
    renewed = heartbeat_sync_run(
        db,
        lease,
        now=NOW + timedelta(seconds=20),
        lease_seconds=60,
    )
    assert renewed.lease_expires_at == NOW + timedelta(seconds=80)

    finished_id = finish_sync_run(
        db,
        renewed,
        outcome=outcome,
        now=NOW + timedelta(seconds=30),
        error_code=error_code,
        error_summary="bounded failure" if error_code else None,
        observed_manifest_sha256="b" * 64,
        stats_json={"items": 10},
    )
    run = db.get(ReferenceSyncRun, finished_id)
    assert run.status == outcome
    assert run.lease_token is None
    assert run.lease_expires_at is None
    assert run.heartbeat_at == NOW + timedelta(seconds=20)


def test_failed_outcome_requires_error_code(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    _only_source_due(db, sources[0])
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "2" * 64,
    )
    with pytest.raises(MirrorLifecycleError):
        finish_sync_run(
            db,
            lease,
            outcome="failed",
            now=NOW + timedelta(seconds=1),
        )


def test_expired_worker_is_fenced_from_finish_and_promotion(
    db,
    metadata_store,
) -> None:
    _, _, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    expired = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=30,
        token_factory=lambda: "3" * 64,
    )
    version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=expired,
        sequence_number=1,
    )

    after_expiry = NOW + timedelta(seconds=31)
    with pytest.raises(MirrorLeaseLostError):
        finish_sync_run(
            db,
            expired,
            outcome="succeeded",
            now=after_expiry,
        )
    replacement = claim_next_sync_run(
        db,
        now=after_expiry,
        lease_seconds=60,
        token_factory=lambda: "4" * 64,
    )
    assert replacement.run_id == expired.run_id
    assert replacement.attempt_no == 2

    with pytest.raises(MirrorLeaseLostError):
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=expired,
            expected_generation=0,
            reason="stale worker must not publish",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=after_expiry + timedelta(seconds=1),
        )
    with pytest.raises(MirrorLeaseLostError):
        finish_sync_run(
            db,
            expired,
            outcome="succeeded",
            now=after_expiry + timedelta(seconds=1),
        )

    promoted = promote_delivery_version(
        db,
        version_id=version.id,
        lease=replacement,
        expected_generation=0,
        reason="replacement worker validated version",
        observed_manifest_sha256="a" * 64,
        stats_json={"features": 10},
        metadata_verifier=_transition_verifier(metadata_store),
        now=after_expiry + timedelta(seconds=1),
    )
    assert promoted.generation == 1
    promoted_run = db.get(ReferenceSyncRun, replacement.run_id)
    assert promoted_run.status == "succeeded"
    assert promoted_run.lease_token is None
    assert promoted_run.observed_manifest_sha256 == "a" * 64
    assert promoted_run.stats_json == {"features": 10}
    with pytest.raises(MirrorLeaseLostError):
        finish_sync_run(
            db,
            replacement,
            outcome="succeeded",
            now=after_expiry + timedelta(seconds=2),
        )


def test_reclaimed_attempt_is_fenced_even_if_a_token_is_reused(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    _only_source_due(db, sources[0])
    enqueue_due_sources(db, now=NOW)
    token = "d" * 64
    expired = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=30,
        token_factory=lambda: token,
    )
    replacement = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=31),
        lease_seconds=60,
        token_factory=lambda: token,
    )
    assert expired.attempt_no == 1
    assert replacement.attempt_no == 2

    with pytest.raises(MirrorLeaseLostError):
        heartbeat_sync_run(
            db,
            expired,
            now=NOW + timedelta(seconds=32),
        )
    finish_sync_run(
        db,
        replacement,
        outcome="unchanged",
        now=NOW + timedelta(seconds=32),
    )


def test_finish_rechecks_database_clock_after_waiting_for_the_run_lock(
    engine,
    committed_reference_providers,
) -> None:
    provider_key = "mirror-fresh-clock-fencing-test"
    committed_reference_providers.append(provider_key)
    current = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as setup:
        _, _, _, sources, _ = _seed_bootstrap(
            setup,
            provider_key=provider_key,
        )
        _only_source_due(setup, sources[0], at=current)
        enqueue_due_sources(setup, now=current)
        lease = claim_next_sync_run(
            setup,
            now=current,
            lease_seconds=30,
            token_factory=lambda: "f" * 64,
        )
        setup.execute(
            text(
                "UPDATE reference_sync_runs "
                "SET lease_expires_at = clock_timestamp() + "
                "INTERVAL '300 milliseconds' WHERE id = :run_id"
            ),
            {"run_id": lease.run_id},
        )
        setup.commit()

    def finish_after_lock() -> int:
        with Session(engine, expire_on_commit=False) as worker:
            return finish_sync_run(
                worker,
                lease,
                outcome="unchanged",
                now=None,
            )

    with Session(engine, expire_on_commit=False) as locker:
        locker.scalar(
            select(ReferenceSyncRun)
            .where(ReferenceSyncRun.id == lease.run_id)
            .with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finish_after_lock)
            time.sleep(0.6)
            locker.commit()
            with pytest.raises(MirrorLeaseLostError):
                future.result(timeout=5)


def test_promotion_rejects_corrupt_current_source_definition(
    db,
    metadata_store,
) -> None:
    _, _, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "e" * 64,
    )
    version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=lease,
        sequence_number=1,
    )
    source.endpoint_url = "https://other.example.test/wfs"
    db.commit()

    with pytest.raises(
        MirrorPromotionConflict,
        match="current delivery source-definition hash is invalid",
    ):
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=0,
            reason="corrupt mutable source must not publish",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    (
        "include_metadata",
        "include_metadata_gate",
        "duplicate_metadata",
        "message",
    ),
    (
        (False, False, False, "no unique verified metadata"),
        (True, False, False, "precommit gate is invalid"),
        (True, True, True, "no unique verified metadata"),
    ),
)
def test_promotion_requires_exactly_one_gated_metadata_asset(
    db,
    metadata_store,
    include_metadata,
    include_metadata_gate,
    duplicate_metadata,
    message,
) -> None:
    _, _, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(
        item for item in sources if item.target_kind == "vector"
    )
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "f" * 64,
    )
    version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=lease,
        sequence_number=1,
        include_metadata=include_metadata,
        include_metadata_gate=include_metadata_gate,
        duplicate_metadata=duplicate_metadata,
    )

    with pytest.raises(MirrorPromotionConflict, match=message):
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=0,
            reason="invalid metadata evidence must not publish",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=1),
        )

    assert db.get(
        ReferenceLayerDeliveryState,
        (source.provider_key, source.layer_id),
    ) is None
    assert db.get(ReferenceSyncRun, lease.run_id).status == "running"


@pytest.mark.parametrize("tamper_kind", ("delete", "corrupt"))
def test_promotion_rehashes_metadata_immediately_before_publish(
    db,
    metadata_store,
    tamper_kind,
) -> None:
    _, _, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(
        item for item in sources if item.target_kind == "vector"
    )
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "9" * 64,
    )
    version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=lease,
        sequence_number=1,
    )
    _tamper_metadata_blob(
        db,
        metadata_store,
        version,
        tamper_kind,
    )

    with pytest.raises(
        MirrorPromotionConflict,
        match="failed transition verification",
    ):
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=0,
            reason="metadata changed after version creation",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=1),
        )

    assert db.get(
        ReferenceLayerDeliveryState,
        (source.provider_key, source.layer_id),
    ) is None
    assert db.get(ReferenceSyncRun, lease.run_id).status == "running"


@pytest.mark.parametrize("tamper_kind", ("delete", "corrupt"))
def test_rollback_rehashes_frozen_metadata_before_transition(
    db,
    metadata_store,
    tamper_kind,
) -> None:
    layer, _, _, version_v1, version_v2 = (
        _promote_cross_snapshot_versions(db, metadata_store)
    )
    _tamper_metadata_blob(
        db,
        metadata_store,
        version_v1,
        tamper_kind,
    )

    with pytest.raises(
        MirrorPromotionConflict,
        match="failed transition verification",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=version_v1.id,
            expected_generation=2,
            reason="metadata changed after initial promotion",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=4),
        )

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 2
    assert state.active_version_id == version_v2.id


@pytest.mark.parametrize("tamper_kind", ("delete", "corrupt"))
def test_reactivation_rehashes_frozen_metadata_before_transition(
    db,
    metadata_store,
    tamper_kind,
) -> None:
    layer, _, _, version_v1, _ = (
        _promote_cross_snapshot_versions(db, metadata_store)
    )
    deactivated = deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=2,
        reason="maintenance isolation",
        now=NOW + timedelta(seconds=4),
    )
    _tamper_metadata_blob(
        db,
        metadata_store,
        version_v1,
        tamper_kind,
    )

    with pytest.raises(
        MirrorPromotionConflict,
        match="failed transition verification",
    ):
        reactivate_delivery(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=version_v1.id,
            expected_generation=deactivated.generation,
            reason="metadata changed while disabled",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=5),
        )

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.status == "disabled"
    assert state.generation == deactivated.generation
    assert state.active_version_id is None


@pytest.mark.parametrize("action", ("rollback", "reactivate"))
def test_recovery_transition_is_atomic_when_physical_delivery_is_absent(
    db,
    metadata_store,
    action,
) -> None:
    layer, _, _, version_v1, version_v2 = (
        _promote_cross_snapshot_versions(db, metadata_store)
    )
    expected_generation = 2
    expected_status = "active"
    expected_active_version_id = version_v2.id
    if action == "reactivate":
        deactivated = deactivate_delivery(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            expected_generation=2,
            reason="prepare physical recovery regression",
            now=NOW + timedelta(seconds=4),
        )
        expected_generation = deactivated.generation
        expected_status = "disabled"
        expected_active_version_id = None
    promotion_count = db.scalar(
        select(func.count(ReferenceDeliveryPromotion.id)).where(
            ReferenceDeliveryPromotion.provider_key
            == layer.provider_key,
            ReferenceDeliveryPromotion.layer_id == layer.id,
        )
    )
    transition = (
        rollback_delivery_version
        if action == "rollback"
        else reactivate_delivery
    )

    with pytest.raises(
        MirrorPromotionConflict,
        match="failed physical transition verification",
    ):
        transition(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=version_v1.id,
            expected_generation=expected_generation,
            reason="physical recovery must fail closed",
            metadata_verifier=_transition_verifier(metadata_store),
            physical_verifier=_missing_physical_transition_verifier,
            now=NOW + timedelta(seconds=5),
        )

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.status == expected_status
    assert state.generation == expected_generation
    assert state.active_version_id == expected_active_version_id
    if action == "reactivate":
        assert all(
            not source.enabled and not source.is_primary
            for source in db.scalars(
                select(ReferenceLayerSource).where(
                    ReferenceLayerSource.provider_key
                    == layer.provider_key,
                    ReferenceLayerSource.layer_id == layer.id,
                )
            )
        )
    assert db.scalar(
        select(func.count(ReferenceDeliveryPromotion.id)).where(
            ReferenceDeliveryPromotion.provider_key
            == layer.provider_key,
            ReferenceDeliveryPromotion.layer_id == layer.id,
        )
    ) == promotion_count


def test_legacy_version_without_metadata_cannot_rollback_or_reactivate(
    db,
    metadata_store,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(
        item for item in sources if item.target_kind == "vector"
    )
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    first_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "a" * 64,
    )
    legacy = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=first_lease,
        sequence_number=1,
        include_metadata=False,
        include_metadata_gate=False,
    )
    first_run = db.get(ReferenceSyncRun, first_lease.run_id)
    first_run.status = "succeeded"
    first_run.finished_at = NOW + timedelta(seconds=1)
    first_run.lease_token = None
    first_run.lease_expires_at = None
    reference_mirror_lifecycle._append_promotion(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        action="promote",
        from_version_id=None,
        to_version_id=legacy.id,
        run_id=first_run.id,
        actor_id=None,
        reason="legacy delivery predating metadata gate",
        created_at=NOW + timedelta(seconds=1),
        state=None,
        latest=None,
    )
    db.commit()

    source = db.get(ReferenceLayerSource, source.id)
    source.next_check_at = NOW + timedelta(seconds=2)
    db.commit()
    enqueue_due_sources(db, now=NOW + timedelta(seconds=2))
    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "b" * 64,
    )
    current = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=second_lease,
        sequence_number=2,
    )
    promote_delivery_version(
        db,
        version_id=current.id,
        lease=second_lease,
        expected_generation=1,
        reason="current gated delivery",
        metadata_verifier=_transition_verifier(metadata_store),
        now=NOW + timedelta(seconds=3),
    )

    with pytest.raises(
        MirrorPromotionConflict,
        match="no unique verified metadata",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=legacy.id,
            expected_generation=2,
            reason="legacy rollback must be blocked",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=4),
        )

    deactivated = deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=2,
        reason="prepare explicit reactivation check",
        now=NOW + timedelta(seconds=5),
    )
    with pytest.raises(
        MirrorPromotionConflict,
        match="no unique verified metadata",
    ):
        reactivate_delivery(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=legacy.id,
            expected_generation=deactivated.generation,
            reason="legacy reactivation must be blocked",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=6),
        )


def test_promotion_rollback_and_deactivation_are_generation_fenced_hash_chain(
    db,
    metadata_store,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)

    enqueue_due_sources(db, now=NOW)
    first_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "5" * 64,
    )
    first_version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=first_lease,
        sequence_number=1,
    )
    first = promote_delivery_version(
        db,
        version_id=first_version.id,
        lease=first_lease,
        expected_generation=0,
        reason="initial validated mirror",
        metadata_verifier=_transition_verifier(metadata_store),
        now=NOW + timedelta(seconds=1),
    )
    assert first.generation == 1
    assert db.get(ReferenceSyncRun, first_lease.run_id).status == "succeeded"

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    state.generation = 9
    db.commit()
    with pytest.raises(
        MirrorPromotionConflict,
        match="does not match the promotion-chain head",
    ):
        deactivate_delivery(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            expected_generation=9,
            reason="corrupt state must fail closed",
            now=NOW + timedelta(seconds=2),
        )
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    state.generation = 1
    db.commit()

    source.next_check_at = NOW + timedelta(seconds=3)
    db.commit()
    enqueue_due_sources(db, now=NOW + timedelta(seconds=3))
    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=3),
        token_factory=lambda: "6" * 64,
    )
    second_version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=second_lease,
        sequence_number=2,
    )
    with pytest.raises(MirrorPromotionConflict):
        promote_delivery_version(
            db,
            version_id=second_version.id,
            lease=second_lease,
            expected_generation=0,
            reason="stale generation",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=4),
        )
    with pytest.raises(
        MirrorPromotionConflict,
        match="rollback target was never an active delivery",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=second_version.id,
            expected_generation=1,
            reason="unpublished versions cannot bypass promotion",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=4),
        )
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 1

    second = promote_delivery_version(
        db,
        version_id=second_version.id,
        lease=second_lease,
        expected_generation=1,
        reason="second validated mirror",
        metadata_verifier=_transition_verifier(metadata_store),
        now=NOW + timedelta(seconds=4),
    )
    assert second.from_version_id == first_version.id
    assert second.generation == 2
    assert db.get(ReferenceSyncRun, second_lease.run_id).status == "succeeded"

    rollback = rollback_delivery_version(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        to_version_id=first_version.id,
        expected_generation=2,
        reason="rollback after smoke-test regression",
        metadata_verifier=_transition_verifier(metadata_store),
        physical_verifier=_physical_transition_verifier,
        now=NOW + timedelta(seconds=6),
    )
    assert rollback.generation == 3
    assert rollback.to_version_id == first_version.id
    deactivated = deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=3,
        reason="operator disabled delivery",
        now=NOW + timedelta(seconds=7),
    )
    assert deactivated.generation == 4
    state = db.get(ReferenceLayerDeliveryState, (layer.provider_key, layer.id))
    assert state.status == "disabled"
    assert state.active_version_id is None
    assert state.last_promotion_id == deactivated.promotion_id

    events = list(
        db.scalars(
            select(ReferenceDeliveryPromotion)
            .where(
                ReferenceDeliveryPromotion.provider_key == layer.provider_key,
                ReferenceDeliveryPromotion.layer_id == layer.id,
            )
            .order_by(ReferenceDeliveryPromotion.sequence_number)
        )
    )
    assert [item.action for item in events] == [
        "promote",
        "promote",
        "rollback",
        "deactivate",
    ]
    assert all(stored_promotion_hash_is_valid(item) for item in events)
    assert all(
        events[index].previous_event_id == events[index - 1].id
        and events[index].previous_event_sha256
        == events[index - 1].event_sha256
        for index in range(1, len(events))
    )


def test_rollback_is_blocked_after_current_mirror_authorization_revocation(
    db,
    metadata_store,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(
        db,
        provider_key="mirror-authorization-rollback-test",
    )
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    versions = []
    for sequence, token in ((1, "c" * 64), (2, "d" * 64)):
        source.next_check_at = NOW + timedelta(seconds=sequence)
        db.commit()
        enqueue_due_sources(db, now=NOW + timedelta(seconds=sequence))
        lease = claim_next_sync_run(
            db,
            now=NOW + timedelta(seconds=sequence),
            token_factory=lambda token=token: token,
        )
        version = _create_version(
            db,
            store=metadata_store,
            source=source,
            snapshot=snapshot,
            lease=lease,
            sequence_number=sequence,
        )
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=sequence - 1,
            reason=f"authorized version {sequence}",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=sequence, milliseconds=100),
        )
        versions.append(version)

    current_review = ensure_authorized_mirror_source(db, source)
    supersede_mirror_authorization(
        db,
        source,
        current_review,
        decision="restricted",
    )

    with pytest.raises(
        MirrorPromotionConflict,
        match="mirror_authorization_restricted",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=versions[0].id,
            expected_generation=2,
            reason="revoked evidence must block rollback",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=4),
        )


def test_deactivation_stops_scheduling_and_requires_explicit_reactivation(
    db,
    metadata_store,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)
    enqueue_due_sources(db, now=NOW)
    first_lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "7" * 64,
    )
    first_version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=first_lease,
        sequence_number=1,
    )
    promote_delivery_version(
        db,
        version_id=first_version.id,
        lease=first_lease,
        expected_generation=0,
        reason="initial version",
        metadata_verifier=_transition_verifier(metadata_store),
        now=NOW + timedelta(seconds=1),
    )

    source.next_check_at = NOW + timedelta(seconds=2)
    db.commit()
    enqueue_due_sources(db, now=NOW + timedelta(seconds=2))
    second_lease = claim_next_sync_run(
        db,
        now=NOW + timedelta(seconds=2),
        token_factory=lambda: "8" * 64,
    )
    second_version = _create_version(
        db,
        store=metadata_store,
        source=source,
        snapshot=snapshot,
        lease=second_lease,
        sequence_number=2,
    )
    deactivated = deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=1,
        reason="administrative stop",
        now=NOW + timedelta(seconds=3),
    )
    run_count = db.scalar(select(func.count(ReferenceSyncRun.id)))
    assert all(
        not item.enabled and not item.is_primary
        for item in db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == layer.provider_key,
                ReferenceLayerSource.layer_id == layer.id,
            )
        )
    )
    assert enqueue_due_sources(db, now=NOW + timedelta(days=2)) == ()
    assert db.scalar(select(func.count(ReferenceSyncRun.id))) == run_count
    disabled_plan = build_mirror_bootstrap_plan(
        db,
        provider_key=layer.provider_key,
    )
    apply_mirror_bootstrap_plan(db, disabled_plan)
    assert all(
        not item.enabled
        for item in db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == layer.provider_key,
                ReferenceLayerSource.layer_id == layer.id,
            )
        )
    )
    with pytest.raises(
        MirrorPromotionConflict,
        match="administratively disabled",
    ):
        promote_delivery_version(
            db,
            version_id=second_version.id,
            lease=second_lease,
            expected_generation=1,
            reason="implicit reactivation is forbidden",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=4),
        )

    reactivated = reactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        to_version_id=first_version.id,
        expected_generation=deactivated.generation,
        reason="operator explicitly restored the last known good version",
        metadata_verifier=_transition_verifier(metadata_store),
        physical_verifier=_physical_transition_verifier,
        now=NOW + timedelta(seconds=5),
    )
    assert reactivated.action == "reactivate"
    assert reactivated.generation == 3
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.status == "active"
    assert state.active_version_id == first_version.id
    assert db.get(ReferenceLayerSource, source.id).enabled is True
    finish_sync_run(
        db,
        second_lease,
        outcome="rejected",
        error_code="superseded_by_operator",
        now=NOW + timedelta(seconds=6),
    )
    assert db.scalar(
        select(func.count(ReferenceSyncRun.id)).where(
            ReferenceSyncRun.parent_run_id == second_lease.run_id
        )
    ) == 0
    [queued_id] = enqueue_due_sources(
        db,
        now=NOW + timedelta(seconds=6),
    )
    assert db.get(ReferenceSyncRun, queued_id).expected_active_generation == 3


def test_rollback_revalidates_the_original_run_and_current_source(
    db,
    metadata_store,
) -> None:
    _, layer, snapshot, sources, _ = _seed_bootstrap(db)
    source = next(item for item in sources if item.target_kind == "vector")
    _only_source_due(db, source)

    versions = []
    for sequence_number, token in ((1, "a" * 64), (2, "b" * 64)):
        source.next_check_at = NOW + timedelta(seconds=sequence_number)
        db.commit()
        enqueue_due_sources(db, now=NOW + timedelta(seconds=sequence_number))
        lease = claim_next_sync_run(
            db,
            now=NOW + timedelta(seconds=sequence_number),
            token_factory=lambda token=token: token,
        )
        version = _create_version(
            db,
            store=metadata_store,
            source=source,
            snapshot=snapshot,
            lease=lease,
            sequence_number=sequence_number,
        )
        promote_delivery_version(
            db,
            version_id=version.id,
            lease=lease,
            expected_generation=sequence_number - 1,
            reason=f"version {sequence_number}",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            now=NOW + timedelta(seconds=sequence_number, milliseconds=100),
        )
        versions.append(version)

    original_run = db.get(ReferenceSyncRun, versions[0].sync_run_id)
    original_run.status = "failed"
    original_run.error_code = "post_publish_integrity_failure"
    original_run.error_summary = "operator marked the source evidence invalid"
    db.commit()
    with pytest.raises(
        MirrorPromotionConflict,
        match="did not finish successfully",
    ):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=versions[0].id,
            expected_generation=2,
            reason="must not revive invalid evidence",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=5),
        )


def test_rollback_can_select_a_valid_version_from_a_previous_catalog_snapshot(
    db,
    metadata_store,
) -> None:
    (
        layer,
        snapshot_v1,
        snapshot_v2,
        version_v1,
        version_v2,
    ) = _promote_cross_snapshot_versions(db, metadata_store)
    run_v1 = db.get(ReferenceSyncRun, version_v1.sync_run_id)
    run_v2 = db.get(ReferenceSyncRun, version_v2.sync_run_id)

    rolled_back = rollback_delivery_version(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        to_version_id=version_v1.id,
        expected_generation=2,
        reason="catalog v2 renderer regression",
        metadata_verifier=_transition_verifier(metadata_store),
        physical_verifier=_physical_transition_verifier,
        now=NOW + timedelta(seconds=4),
    )

    assert snapshot_v1.id != snapshot_v2.id
    assert snapshot_v1.is_current is False
    assert snapshot_v2.is_current is True
    assert run_v1.source_definition_sha256 != run_v2.source_definition_sha256
    assert rolled_back.action == "rollback"
    assert rolled_back.from_version_id == version_v2.id
    assert rolled_back.to_version_id == version_v1.id
    assert rolled_back.generation == 3
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == version_v1.id


@pytest.mark.parametrize(
    "corruption",
    ("run_definition", "catalog_snapshot"),
)
def test_cross_snapshot_rollback_fails_closed_for_corrupt_frozen_evidence(
    db,
    metadata_store,
    corruption: str,
) -> None:
    (
        layer,
        snapshot_v1,
        _,
        version_v1,
        version_v2,
    ) = _promote_cross_snapshot_versions(db, metadata_store)
    if corruption == "run_definition":
        run = db.get(ReferenceSyncRun, version_v1.sync_run_id)
        run.source_definition_json = {
            **run.source_definition_json,
            "remote_name": "tampered:layer",
        }
    elif corruption == "catalog_snapshot":
        snapshot_v1.normalized_definition_json = {
            **snapshot_v1.normalized_definition_json,
            "unresolved_count": 99,
        }
    db.commit()

    with pytest.raises(MirrorPromotionConflict):
        rollback_delivery_version(
            db,
            provider_key=layer.provider_key,
            layer_id=layer.id,
            to_version_id=version_v1.id,
            expected_generation=2,
            reason="corrupt historical evidence must never reactivate",
            metadata_verifier=_transition_verifier(
                metadata_store
            ),
            physical_verifier=_physical_transition_verifier,
            now=NOW + timedelta(seconds=4),
        )

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 2
    assert state.active_version_id == version_v2.id


def test_published_asset_cannot_be_corrupted_before_cross_snapshot_rollback(
    db,
    metadata_store,
) -> None:
    (
        layer,
        _,
        _,
        version_v1,
        version_v2,
    ) = _promote_cross_snapshot_versions(db, metadata_store)
    asset = db.scalar(
        select(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.version_id == version_v1.id,
            ReferenceDeliveryAsset.is_primary.is_(True),
        )
    )
    asset.sha256 = "f" * 64

    with pytest.raises(
        OperationalError,
        match="reference mirror records are immutable",
    ):
        db.commit()
    db.rollback()

    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.generation == 2
    assert state.active_version_id == version_v2.id
    assert db.get(ReferenceDeliveryAsset, asset.id).sha256 == (
        version_v1.content_sha256
    )


def test_worker_observation_metadata_is_bounded(db) -> None:
    _, _, _, sources, _ = _seed_bootstrap(db)
    _only_source_due(db, sources[0])
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        token_factory=lambda: "c" * 64,
    )
    invalid_arguments = (
        {"observed_etag": "e" * 4_097},
        {"observed_version": "v" * 2_049},
        {
            "outcome": "failed",
            "error_code": "failed",
            "error_summary": "x" * 4_097,
        },
        {"stats_json": {"payload": "x" * 1_048_576}},
    )
    for arguments in invalid_arguments:
        with pytest.raises(MirrorLifecycleError):
            finish_sync_run(
                db,
                lease,
                outcome=arguments.pop("outcome", "unchanged"),
                now=NOW + timedelta(seconds=1),
                **arguments,
            )
    finish_sync_run(
        db,
        lease,
        outcome="unchanged",
        now=NOW + timedelta(seconds=1),
    )
