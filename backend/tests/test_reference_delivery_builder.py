from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import headers_for
from sqlalchemy import event, select

from app.reference_layers import (
    local_metadata as reference_local_metadata,
)
from app.reference_layers import routes as reference_layer_routes
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.delivery_builder import (
    DeliveryBuildError,
    DeliveryContinuityError,
    PreparedDelivery,
    PreparedDeliveryAsset,
    canonical_json_bytes,
    canonical_json_sha256,
    create_delivery_version,
    evaluate_delivery_continuity,
    local_metadata_asset_descriptor,
)
from app.reference_layers.local_metadata import (
    LocalMetadataError,
    _source_metadata_projection,
    attach_local_metadata_asset,
    catalog_local_metadata_availability,
    resolve_local_metadata,
    verify_local_metadata_precommit,
    verify_local_metadata_transition,
)
from app.reference_layers.local_delivery import resolve_local_delivery
from app.reference_layers.mirror_lifecycle import (
    MirrorLeaseLostError,
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
    claim_next_sync_run,
    enqueue_due_sources,
    promote_delivery_version,
)
from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
    ReferenceLayerSource,
    ReferenceLayer,
    ReferenceLayerStyle,
    ReferenceMirrorAuthorizationReview,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.reviewed_ortho_evidence import (
    CATALOG_ENDPOINT_URL,
    reviewed_ign_ortho_expected_source_definition,
    reviewed_ign_ortho_substitution,
)
from app.reference_layers.style_parity import persist_style_parity_plan
from app.reference_layers.wms_delivery import (
    LayerDeliveryAvailability,
)
from support_reference_mirror_authorization import (
    authorize_mirror_source,
    bind_run_authorization,
    supersede_mirror_authorization,
)

NOW = datetime(2026, 7, 23, 8, tzinfo=timezone.utc)


def test_web_reference_blob_store_is_strictly_read_only(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def build_store(root, **kwargs):
        captured["root"] = root
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        reference_layer_routes,
        "ReferenceBlobStore",
        build_store,
    )

    assert reference_layer_routes._reference_blob_store() is sentinel
    assert captured["read_only"] is True


def _lease_and_input(db, storage_root):
    definition = ReferenceCatalogDefinition(
        provider_key="delivery-builder-test",
        source_url="https://example.test/catalog.json",
        raw_catalog={"revision": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service:wms",
                title="WMS",
                upstream_protocol="wms",
                base_url="https://example.test/geoserver/planning/wms",
                license_status="approved",
                cache_policy="mirror",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:planning",
                node_type="layer",
                title="Planning",
                service_key="service:wms",
                remote_name="planning:zones",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                bounds={
                    "west": -7.1,
                    "south": 40,
                    "east": -1.7,
                    "north": 43.3,
                },
                style_name="style:default",
                styles=(
                    ReferenceLayerStyleDefinition(
                        source_key="style:default",
                        remote_name="planning:default",
                        title="Default",
                        is_default=True,
                    ),
                ),
            ),
        ),
        retrieved_at=NOW,
    )
    apply_catalog_definition(db, definition)
    apply_mirror_bootstrap_plan(
        db,
        build_mirror_bootstrap_plan(db, provider_key=definition.provider_key),
    )
    source = db.scalar(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.provider_key == definition.provider_key,
            ReferenceLayerSource.target_kind == "vector",
        )
    )
    for item in db.scalars(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.provider_key == definition.provider_key
        )
    ):
        item.enabled = item.id == source.id
        item.is_primary = item.id == source.id
        item.next_check_at = NOW - timedelta(seconds=1)
    db.commit()
    review = authorize_mirror_source(db, source, reviewed_at=NOW)
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=300,
        token_factory=lambda: "7" * 64,
    )
    bind_run_authorization(db, source, lease.run_id)
    artifact = ReferenceSourceArtifact(
        source_id=source.id,
        artifact_kind="dataset",
        source_url="https://example.test/zones.geojson",
        final_url="https://example.test/zones.geojson",
        source_version="2026-07-23",
        media_type="application/geo+json",
        storage_backend="filesystem",
        storage_key=f"blobs/sha256/{'a' * 2}/{'a' * 64}",
        size_bytes=512,
        sha256="a" * 64,
        metadata_json={},
        retrieved_at=NOW,
    )
    db.add(artifact)
    db.flush()
    db.add(
        ReferenceSyncRunArtifact(
            source_id=source.id,
            run_id=lease.run_id,
            artifact_id=artifact.id,
            role="input",
        )
    )
    db.flush()
    fixture = _persist_builder_style_plan(
        db,
        source=source,
        lease=lease,
        input_artifact=artifact,
        now=NOW,
    )
    layer = db.get(ReferenceLayer, source.layer_id)
    fixture.source = source
    fixture.lease = lease
    fixture.snapshot = layer.last_seen_snapshot
    fixture.review = review
    fixture.definition = definition
    fixture.db = db
    fixture.store = ReferenceBlobStore(Path(storage_root))
    db.commit()
    return source, lease, fixture


def _persist_builder_style_plan(
    db,
    *,
    source,
    lease,
    input_artifact,
    now,
):
    layer = db.get(ReferenceLayer, source.layer_id)
    style = db.scalar(
        select(ReferenceLayerStyle).where(
            ReferenceLayerStyle.layer_id == layer.id,
            ReferenceLayerStyle.source_key == "style:default",
        )
    )
    digest = hashlib.sha256(
        f"builder-style-{lease.run_id}".encode()
    ).hexdigest()
    style_artifact = ReferenceSourceArtifact(
        source_id=source.id,
        artifact_kind="style",
        source_version="1.0.0",
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
        retrieved_at=now,
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
    persist_style_parity_plan(
        db,
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        catalog_snapshot_id=layer.last_seen_snapshot_id,
        source_id=source.id,
        sync_run_id=lease.run_id,
        delivery_kind=source.target_kind,
        styles=(style,),
        artifacts=(
            SimpleNamespace(
                artifact_id=input_artifact.id,
                artifact_kind=input_artifact.artifact_kind,
                roles=frozenset({"input"}),
                media_type=input_artifact.media_type,
                storage_backend=input_artifact.storage_backend,
                storage_key=input_artifact.storage_key,
                size_bytes=input_artifact.size_bytes,
                sha256=input_artifact.sha256,
                metadata_json=input_artifact.metadata_json,
            ),
            SimpleNamespace(
                artifact_id=style_artifact.id,
                artifact_kind="style",
                roles=frozenset({"style"}),
                media_type=style_artifact.media_type,
                storage_backend=style_artifact.storage_backend,
                storage_key=style_artifact.storage_key,
                size_bytes=style_artifact.size_bytes,
                sha256=style_artifact.sha256,
                metadata_json=style_artifact.metadata_json,
            ),
        ),
        probe=None,
        now=now,
    )
    return SimpleNamespace(
        id=input_artifact.id,
        style_artifact_id=style_artifact.id,
        style_sha256=style_artifact.sha256,
        style_storage_key=style_artifact.storage_key,
        style_id=style.id,
        style_source_key=style.source_key,
    )


def _prepared(fixture, *, input_artifact_id: int | None = None) -> PreparedDelivery:
    artifact_id = fixture.id if input_artifact_id is None else input_artifact_id
    validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "vector",
        "checks": {
            "data_schema": {
                "schema_version": "reference-vector-schema/v1",
                "columns": [
                    {
                        "name": "source_fid",
                        "data_type": "bigint",
                        "not_null": True,
                    },
                    {
                        "name": "geom",
                        "data_type": "geometry(MultiPolygon,3857)",
                        "not_null": True,
                    },
                ],
            },
            "geometry_type": "MULTIPOLYGON",
            "srid": 3857,
        },
    }
    prepared = PreparedDelivery(
        delivery_kind="vector",
        source_version="2026-07-23",
        content_sha256="b" * 64,
        reference_at=NOW,
        crs="EPSG:3857",
        bounds_json={
            "west": -7.1,
            "south": 40.0,
            "east": -1.7,
            "north": 43.3,
        },
        feature_count=123,
        validation_json=validation,
        input_artifact_ids=(artifact_id,),
        assets=(
            PreparedDeliveryAsset(
                asset_key="primary",
                asset_kind="vector_table",
                is_primary=True,
                storage_backend="postgres",
                storage_key="reference_data.siur_layer_1_run_1",
                media_type="application/x-postgis-table",
                sha256="b" * 64,
                size_bytes=None,
                metadata_json={
                    "renderer": "geoserver",
                    "layer_name": "siur_layer_1_run_1",
                    "default_style_name": "siur_style_default_v1",
                    "styles": {
                        str(fixture.style_id): "siur_style_default_v1"
                    },
                    "identify_available": True,
                    "legend_available": True,
                },
            ),
            PreparedDeliveryAsset(
                asset_key="style-default",
                asset_kind="style_sld",
                is_primary=False,
                storage_backend="filesystem",
                storage_key=fixture.style_storage_key,
                media_type="application/vnd.ogc.sld+xml",
                sha256=fixture.style_sha256,
                size_bytes=256,
                metadata_json={
                    "catalog_style_id": fixture.style_id,
                    "catalog_style_source_key": fixture.style_source_key,
                    "style_name": "siur_style_default_v1",
                    "parity_kind": "exact",
                    "effective": True,
                },
            ),
        ),
        supporting_artifacts=((fixture.style_artifact_id, "style"),),
    )
    return _with_metadata_asset(prepared, fixture)


def _with_metadata_asset(
    prepared: PreparedDelivery,
    fixture,
) -> PreparedDelivery:
    without_metadata = PreparedDelivery(
        **{
            **prepared.__dict__,
            "assets": tuple(
                asset
                for asset in prepared.assets
                if asset.asset_kind != "metadata"
            ),
        }
    )
    return attach_local_metadata_asset(
        lambda: nullcontext(fixture.db),
        fixture.store,
        fixture.lease,
        without_metadata,
    )


def _metadata_verifier(fixture):
    return lambda context: verify_local_metadata_precommit(
        fixture.store,
        context,
    )


def _transition_verifier(fixture):
    return lambda transition_db, version: (
        verify_local_metadata_transition(
            fixture.store,
            transition_db,
            version,
        )
    )


def _promoted_local_metadata_delivery(db, storage_root):
    source, lease, fixture = _lease_and_input(
        db,
        storage_root,
    )
    prepared = _prepared(fixture)
    built = create_delivery_version(
        db,
        lease=lease,
        prepared=prepared,
        metadata_verifier=_metadata_verifier(fixture),
        now=NOW + timedelta(seconds=1),
    )
    promote_delivery_version(
        db,
        version_id=built.version_id,
        lease=lease,
        expected_generation=0,
        reason="Publish canonical local metadata",
        metadata_verifier=_transition_verifier(fixture),
        now=NOW + timedelta(seconds=2),
    )
    return SimpleNamespace(
        source=source,
        store=fixture.store,
        layer=db.get(ReferenceLayer, source.layer_id),
        review=fixture.review,
        definition=fixture.definition,
        version=db.get(ReferenceDeliveryVersion, built.version_id),
        metadata_asset=db.scalar(
            select(ReferenceDeliveryAsset).where(
                ReferenceDeliveryAsset.version_id == built.version_id,
                ReferenceDeliveryAsset.asset_kind == "metadata",
            )
        ),
    )


def _metadata_route(layer_id: int, organization_id: int) -> str:
    return (
        f"/organizations/{organization_id}/reference-layers/"
        f"{layer_id}/metadata.json"
    )


def test_create_delivery_version_links_inputs_and_assets(
    db,
    tmp_path,
) -> None:
    source, lease, artifact = _lease_and_input(db, tmp_path)
    prepared = _prepared(artifact)

    built = create_delivery_version(
        db,
        lease=lease,
        prepared=prepared,
        metadata_verifier=_metadata_verifier(artifact),
        now=NOW + timedelta(seconds=1),
    )

    version = db.get(ReferenceDeliveryVersion, built.version_id)
    asset = db.scalar(
        select(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.version_id == version.id,
            ReferenceDeliveryAsset.is_primary.is_(True),
        )
    )
    assert version.source_id == source.id
    assert version.sequence_number == 1
    assert version.validation_sha256 == canonical_json_sha256(
        version.validation_json
    )
    assert version.validation_json["continuity_gate"]["passed"] is True
    assert version.validation_json["local_metadata_gate"][
        "passed"
    ] is True
    assert (
        version.validation_json["continuity_gate"]["baseline_version_id"]
        is None
    )
    assert built.manifest_sha256 == version.manifest_sha256
    assert asset.storage_key == "reference_data.siur_layer_1_run_1"
    assert asset.metadata_json["renderer"] == "geoserver"


def test_builder_requires_one_exact_version_bound_metadata_asset(
    db,
    tmp_path,
) -> None:
    _, lease, fixture = _lease_and_input(db, tmp_path)
    prepared = _prepared(fixture)
    with pytest.raises(TypeError, match="metadata_verifier"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=prepared,
            now=NOW + timedelta(seconds=1),
        )
    without_metadata = PreparedDelivery(
        **{
            **prepared.__dict__,
            "assets": tuple(
                asset
                for asset in prepared.assets
                if asset.asset_kind != "metadata"
            ),
        }
    )
    with pytest.raises(
        DeliveryBuildError,
        match="exactly one local metadata asset",
    ):
        create_delivery_version(
            db,
            lease=lease,
            prepared=without_metadata,
            metadata_verifier=_metadata_verifier(fixture),
            now=NOW + timedelta(seconds=1),
        )

    metadata = next(
        asset
        for asset in prepared.assets
        if asset.asset_kind == "metadata"
    )
    duplicate = PreparedDeliveryAsset(
        **{**metadata.__dict__, "asset_key": "metadata-copy"}
    )
    with pytest.raises(
        DeliveryBuildError,
        match="exactly one local metadata asset",
    ):
        create_delivery_version(
            db,
            lease=lease,
            prepared=PreparedDelivery(
                **{
                    **prepared.__dict__,
                    "assets": (*prepared.assets, duplicate),
                }
            ),
            metadata_verifier=_metadata_verifier(fixture),
            now=NOW + timedelta(seconds=1),
        )

    changed_descriptor = deepcopy(metadata.metadata_json)
    changed_descriptor["binding"]["source_id"] += 1
    changed = PreparedDeliveryAsset(
        **{
            **metadata.__dict__,
            "metadata_json": changed_descriptor,
        }
    )
    with pytest.raises(
        DeliveryBuildError,
        match="binding changed before version creation",
    ):
        create_delivery_version(
            db,
            lease=lease,
            prepared=PreparedDelivery(
                **{
                    **prepared.__dict__,
                    "assets": tuple(
                        changed
                        if asset.asset_kind == "metadata"
                        else asset
                        for asset in prepared.assets
                    ),
                }
            ),
            metadata_verifier=_metadata_verifier(fixture),
            now=NOW + timedelta(seconds=1),
        )


def test_builder_rejects_missing_metadata_blob_before_insert(
    db,
    tmp_path,
) -> None:
    _, lease, fixture = _lease_and_input(db, tmp_path)
    prepared = _prepared(fixture)
    metadata = next(
        asset
        for asset in prepared.assets
        if asset.asset_kind == "metadata"
    )
    fixture.store.resolve_blob(metadata.storage_key).unlink()

    with pytest.raises(
        reference_local_metadata.LocalMetadataError,
        match="unavailable",
    ):
        create_delivery_version(
            db,
            lease=lease,
            prepared=prepared,
            metadata_verifier=_metadata_verifier(fixture),
            now=NOW + timedelta(seconds=1),
        )
    assert db.scalar(select(ReferenceDeliveryVersion.id)) is None


def test_builder_rejects_other_canonical_json_with_valid_binding(
    db,
    tmp_path,
) -> None:
    _, lease, fixture = _lease_and_input(db, tmp_path)
    prepared = _prepared(fixture)
    metadata = next(
        asset
        for asset in prepared.assets
        if asset.asset_kind == "metadata"
    )
    with fixture.store.open_blob(metadata.storage_key) as stream:
        document = json.load(stream)
    document["catalog"]["layer"]["title"] = "Documento ajeno"
    body = canonical_json_bytes(document)
    blob = fixture.store.put_stream(io.BytesIO(body))
    other_metadata = PreparedDeliveryAsset(
        **{
            **metadata.__dict__,
            "storage_key": blob.storage_key,
            "sha256": blob.sha256,
            "size_bytes": blob.size_bytes,
            "metadata_json": local_metadata_asset_descriptor(
                document_sha256=blob.sha256,
                document_size_bytes=blob.size_bytes,
                binding=document["binding"],
            ),
        }
    )
    candidate = PreparedDelivery(
        **{
            **prepared.__dict__,
            "assets": tuple(
                other_metadata
                if asset.asset_kind == "metadata"
                else asset
                for asset in prepared.assets
            ),
        }
    )

    with pytest.raises(
        reference_local_metadata.LocalMetadataError,
        match="locked delivery rows",
    ):
        create_delivery_version(
            db,
            lease=lease,
            prepared=candidate,
            metadata_verifier=_metadata_verifier(fixture),
            now=NOW + timedelta(seconds=1),
        )
    assert db.scalar(select(ReferenceDeliveryVersion.id)) is None


def test_local_metadata_endpoint_is_authenticated_local_and_exact(
    client,
    db,
    tmp_path,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)
    monkeypatch.setattr(
        reference_layer_routes,
        "_reference_blob_store",
        lambda: ReferenceBlobStore(tmp_path),
    )
    viewer = make_user()
    organization = make_organization()
    path = _metadata_route(delivery.layer.id, organization.id)

    denied = client.get(path, headers=headers_for(viewer))
    assert denied.status_code == 403
    grant_permissions(viewer, organization, ["map.view"])

    token = headers_for(viewer)["Authorization"].removeprefix("Bearer ")
    client.cookies.set("access_token", token)
    response = client.get(path)
    assert response.status_code == 200
    repeated = client.get(path)
    assert repeated.status_code == 200
    assert repeated.content == response.content
    assert repeated.headers["etag"] == response.headers["etag"]
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == (
        "private, no-store, max-age=0"
    )
    assert response.headers["etag"] == (
        f'"{delivery.metadata_asset.sha256}"'
    )
    assert response.headers["vary"] == "Authorization, Cookie"
    document = response.json()
    assert document["schema_version"] == (
        "siur-local-delivery-metadata/v1"
    )
    assert document["binding"]["sync_run_id"] == (
        delivery.version.sync_run_id
    )
    assert document["provenance"]["upstream_metadata"] == []
    serialized = response.text
    for internal_key in (
        "source_url",
        "final_url",
        "storage_key",
        "base_url",
        "remote_name",
        "allowed_origins",
    ):
        assert f'"{internal_key}"' not in serialized
    catalog = client.get(
        "/reference-layers/catalog"
        f"?provider_key={delivery.source.provider_key}"
        f"&organization_id={organization.id}",
    )
    assert catalog.status_code == 200
    catalog_layer = next(
        layer
        for layer in catalog.json()["layers"]
        if layer["id"] == delivery.layer.id
    )
    assert catalog_layer["metadata_available"] is True


def test_local_metadata_projects_visible_degraded_ortho_without_urls() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2023",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    source = _source_metadata_projection(
        source=SimpleNamespace(
            id=41,
            source_key="auto:wms_tiles:" + "a" * 32,
        ),
        run_definition=definition,
        source_definition_sha256="b" * 64,
    )

    assert source["protocol"] == "wms_tiles"
    assert source["target_kind"] == "tiles"
    assert source["source_format"] == "image/jpeg"
    substitution = source["ortho_substitution"]
    assert substitution["selected_layer"] == "PNOA2023"
    assert substitution["equivalence_status"] == "substitute_degraded"
    assert substitution["declared_coverage"] == "full"
    assert substitution["promotion_eligible"] is True
    assert substitution["comparison_basis"] == (
        "catalog_and_selected_capabilities_require_degraded_delivery"
    )
    assert len(substitution["catalog_capabilities_sha256"]) == 64
    serialized = json.dumps(source, sort_keys=True)
    assert "capabilities_url" not in serialized
    assert "license_url" not in serialized
    assert "endpoint_url" not in serialized


def test_local_metadata_rejects_blocked_reviewed_ortho() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2021",
    )
    assert reviewed is not None

    with pytest.raises(LocalMetadataError):
        _source_metadata_projection(
            source=SimpleNamespace(
                id=42,
                source_key="auto:wms_tiles:" + "c" * 32,
            ),
            run_definition=(
                reviewed_ign_ortho_expected_source_definition(reviewed)
            ),
            source_definition_sha256="d" * 64,
        )


def test_semantic_catalog_refresh_preserves_frozen_map_and_metadata(
    db,
    tmp_path,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)
    original = resolve_local_metadata(
        db,
        delivery.store,
        layer=delivery.layer,
    )
    original_snapshot_id = delivery.version.catalog_snapshot_id
    original_service = delivery.definition.services[0]
    original_layer = delivery.definition.layers[0]
    original_style = original_layer.styles[0]
    refreshed_definition = replace(
        delivery.definition,
        raw_catalog={"revision": 2},
        services=(
            replace(
                original_service,
                title="WMS refreshed",
            ),
        ),
        layers=(
            replace(
                original_layer,
                title="Planning refreshed",
                styles=(
                    replace(
                        original_style,
                        title="Default refreshed",
                    ),
                ),
            ),
        ),
        retrieved_at=NOW + timedelta(days=1),
    )
    refreshed_snapshot, _ = apply_catalog_definition(
        db,
        refreshed_definition,
    )
    current_layer = db.get(ReferenceLayer, delivery.layer.id)
    current_style = db.scalar(
        select(ReferenceLayerStyle).where(
            ReferenceLayerStyle.layer_id == current_layer.id,
            ReferenceLayerStyle.is_default.is_(True),
            ReferenceLayerStyle.last_seen_snapshot_id
            == refreshed_snapshot.id,
        )
    )

    selection = resolve_local_delivery(
        db,
        layer=current_layer,
        style=current_style,
        operation="tile",
    )
    refreshed = resolve_local_metadata(
        db,
        delivery.store,
        layer=current_layer,
    )
    metadata_availability = catalog_local_metadata_availability(
        db,
        delivery.store,
        provider_key=current_layer.provider_key,
        layers=[current_layer],
        local_availability={
            current_layer.id: LayerDeliveryAvailability(
                delivery_available=True,
                legend_available=True,
                identify_available=True,
                delivery_blocker=None,
                available_style_ids=(current_style.id,),
                available_legend_style_ids=(current_style.id,),
            )
        },
    )

    assert refreshed_snapshot.id != original_snapshot_id
    assert current_layer.last_seen_snapshot_id == refreshed_snapshot.id
    assert selection is not None
    assert selection.version_id == delivery.version.id
    assert refreshed.version_id == delivery.version.id
    assert refreshed.sha256 == original.sha256
    assert refreshed.body == original.body
    assert metadata_availability[current_layer.id] is True


def test_extra_descriptor_key_disables_catalog_and_endpoint(
    client,
    db,
    tmp_path,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)
    viewer = make_user()
    organization = make_organization()
    grant_permissions(viewer, organization, ["map.view"])
    delivery.metadata_asset.metadata_json = {
        **delivery.metadata_asset.metadata_json,
        "unexpected": True,
    }
    monkeypatch.setattr(
        reference_layer_routes,
        "_reference_blob_store",
        lambda: ReferenceBlobStore(tmp_path),
    )
    headers = headers_for(viewer)

    endpoint = client.get(
        _metadata_route(delivery.layer.id, organization.id),
        headers=headers,
    )
    assert endpoint.status_code == 409
    catalog = client.get(
        "/reference-layers/catalog"
        f"?provider_key={delivery.source.provider_key}"
        f"&organization_id={organization.id}",
        headers=headers,
    )
    catalog_layer = next(
        layer
        for layer in catalog.json()["layers"]
        if layer["id"] == delivery.layer.id
    )
    assert catalog_layer["metadata_available"] is False


def test_catalog_metadata_check_is_bounded_and_never_opens_blobs(
    db,
    tmp_path,
    monkeypatch,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)

    def reject_blob_open(_storage_key):
        raise AssertionError("catalog must not open metadata bodies")

    monkeypatch.setattr(
        delivery.store,
        "open_blob",
        reject_blob_open,
    )
    connection = db.connection()
    query_count = 0

    def count_query(*_args):
        nonlocal query_count
        query_count += 1

    event.listen(
        connection,
        "before_cursor_execute",
        count_query,
    )
    try:
        result = catalog_local_metadata_availability(
            db,
            delivery.store,
            provider_key=delivery.source.provider_key,
            layers=[delivery.layer] * 227,
            local_availability={
                delivery.layer.id: LayerDeliveryAvailability(
                    delivery_available=True,
                    legend_available=True,
                    identify_available=True,
                    delivery_blocker=None,
                    available_style_ids=(),
                    available_legend_style_ids=(),
                )
            },
        )
    finally:
        event.remove(
            connection,
            "before_cursor_execute",
            count_query,
        )

    assert result[delivery.layer.id] is True
    assert query_count == 5


@pytest.mark.parametrize("tamper_kind", ["sha256", "size", "body"])
def test_body_tampering_blocks_endpoint_while_catalog_checks_size_only(
    client,
    db,
    tmp_path,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
    tamper_kind,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)
    monkeypatch.setattr(
        reference_layer_routes,
        "_reference_blob_store",
        lambda: ReferenceBlobStore(tmp_path),
    )
    with ReferenceBlobStore(tmp_path) as store:
        path = store.resolve_blob(delivery.metadata_asset.storage_key)
        body = path.read_bytes()
        if tamper_kind == "sha256":
            path.write_bytes(
                bytes([body[0] ^ 1]) + body[1:]
            )
        elif tamper_kind == "size":
            path.write_bytes(body[:-1])
        else:
            path.write_bytes(
                b"{" + (b" " * (len(body) - 2)) + b"}"
            )

    viewer = make_user()
    organization = make_organization()
    grant_permissions(viewer, organization, ["map.view"])
    headers = headers_for(viewer)
    response = client.get(
        _metadata_route(delivery.layer.id, organization.id),
        headers=headers,
    )
    assert response.status_code == 409
    catalog = client.get(
        "/reference-layers/catalog"
        f"?provider_key={delivery.source.provider_key}"
        f"&organization_id={organization.id}",
        headers=headers,
    )
    assert catalog.status_code == 200
    catalog_layer = next(
        layer
        for layer in catalog.json()["layers"]
        if layer["id"] == delivery.layer.id
    )
    assert catalog_layer["metadata_available"] is (
        tamper_kind != "size"
    )


def test_catalog_does_not_announce_incomplete_metadata_evidence(
    client,
    db,
    tmp_path,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)
    viewer = make_user()
    organization = make_organization()
    grant_permissions(viewer, organization, ["map.view"])
    validation = deepcopy(delivery.version.validation_json)
    validation["local_metadata_gate"][
        "descriptor_sha256"
    ] = "0" * 64
    delivery.version.validation_json = validation
    monkeypatch.setattr(
        reference_layer_routes,
        "_reference_blob_store",
        lambda: ReferenceBlobStore(tmp_path),
    )
    headers = headers_for(viewer)

    endpoint = client.get(
        _metadata_route(delivery.layer.id, organization.id),
        headers=headers,
    )
    assert endpoint.status_code == 409
    catalog = client.get(
        "/reference-layers/catalog"
        f"?provider_key={delivery.source.provider_key}"
        f"&organization_id={organization.id}",
        headers=headers,
    )
    catalog_layer = next(
        layer
        for layer in catalog.json()["layers"]
        if layer["id"] == delivery.layer.id
    )
    assert catalog_layer["metadata_available"] is False


def test_revoked_authorization_disables_local_metadata(
    client,
    db,
    tmp_path,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    delivery = _promoted_local_metadata_delivery(db, tmp_path)
    supersede_mirror_authorization(
        db,
        delivery.source,
        delivery.review,
        decision="rejected",
    )
    monkeypatch.setattr(
        reference_layer_routes,
        "_reference_blob_store",
        lambda: ReferenceBlobStore(tmp_path),
    )
    viewer = make_user()
    organization = make_organization()
    grant_permissions(viewer, organization, ["map.view"])
    response = client.get(
        _metadata_route(delivery.layer.id, organization.id),
        headers=headers_for(viewer),
    )
    assert response.status_code == 409


def test_builder_rejects_unlinked_input_and_expired_lease(
    db,
    tmp_path,
) -> None:
    _, lease, artifact = _lease_and_input(db, tmp_path)
    prepared = _prepared(artifact)
    unlinked = PreparedDelivery(
        **{
            **prepared.__dict__,
            "input_artifact_ids": (artifact.id + 999,),
        }
    )
    with pytest.raises(DeliveryBuildError, match="not immutable artifacts"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=unlinked,
            metadata_verifier=_metadata_verifier(artifact),
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(MirrorLeaseLostError):
        create_delivery_version(
            db,
            lease=lease,
            prepared=prepared,
            metadata_verifier=_metadata_verifier(artifact),
            now=NOW + timedelta(seconds=301),
        )


def test_builder_rejects_unsafe_or_inconsistent_primary_asset(
    db,
    tmp_path,
) -> None:
    _, lease, artifact = _lease_and_input(db, tmp_path)
    prepared = _prepared(artifact)
    unsafe = PreparedDelivery(
        **{
            **prepared.__dict__,
            "assets": (
                PreparedDeliveryAsset(
                    **{
                        **prepared.assets[0].__dict__,
                        "storage_key": "public.injected;drop_table",
                    }
                ),
            ),
        }
    )
    with pytest.raises(DeliveryBuildError, match="PostGIS storage key"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=unsafe,
            metadata_verifier=_metadata_verifier(artifact),
            now=NOW,
        )

    mismatch = PreparedDelivery(
        **{**prepared.__dict__, "content_sha256": "c" * 64}
    )
    mismatch = _with_metadata_asset(mismatch, artifact)
    with pytest.raises(DeliveryBuildError, match="content hash"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=mismatch,
            metadata_verifier=_metadata_verifier(artifact),
            now=NOW,
        )

    oversized_version = PreparedDelivery(
        **{**prepared.__dict__, "source_version": "v" * 2_049}
    )
    with pytest.raises(DeliveryBuildError, match="source version"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=oversized_version,
            metadata_verifier=_metadata_verifier(artifact),
            now=NOW,
        )


def test_filesystem_assets_must_be_content_addressed() -> None:
    asset = PreparedDeliveryAsset(
        asset_key="primary",
        asset_kind="tile_archive",
        is_primary=True,
        storage_backend="filesystem",
        storage_key=f"blobs/sha256/{'d' * 2}/{'e' * 64}",
        media_type="application/vnd.sqlite3",
        sha256="e" * 64,
        size_bytes=1024,
        metadata_json={"renderer": "tile_archive"},
    )
    prepared = PreparedDelivery(
        delivery_kind="tiles",
        source_version=None,
        content_sha256="e" * 64,
        reference_at=None,
        crs="EPSG:3857",
        bounds_json={"west": -7, "south": 40, "east": -1, "north": 44},
        feature_count=None,
        validation_json={
            "schema_version": "reference-delivery-validation/v1",
            "passed": True,
            "kind": "tiles",
            "checks": {
                "data_schema": {
                    "schema_version": "reference-tiles-schema/v1",
                    "archives": [
                        {
                            "catalog_style_source_key": None,
                            "image_format": "png",
                            "min_zoom": 0,
                            "max_zoom": 0,
                        }
                    ],
                }
            },
        },
        input_artifact_ids=(1,),
        assets=(asset,),
    )
    with pytest.raises(DeliveryBuildError, match="content-addressed"):
        # Validation happens before database access, so a session is not needed.
        create_delivery_version(
            None,
            lease=None,
            prepared=prepared,
            metadata_verifier=lambda context: None,
        )


def test_continuity_report_checks_kind_crs_bounds_schema_and_features(
    db,
    tmp_path,
) -> None:
    _, lease, artifact = _lease_and_input(db, tmp_path)
    prepared = _prepared(artifact)
    built = create_delivery_version(
        db,
        lease=lease,
        prepared=prepared,
        metadata_verifier=_metadata_verifier(artifact),
        now=NOW + timedelta(seconds=1),
    )
    active = db.get(ReferenceDeliveryVersion, built.version_id)

    crs = PreparedDelivery(
        **{**prepared.__dict__, "crs": "EPSG:25830"}
    )
    bounds = PreparedDelivery(
        **{
            **prepared.__dict__,
            "bounds_json": {
                "west": 50.0,
                "south": 40.0,
                "east": 56.0,
                "north": 43.3,
            },
        }
    )
    schema_validation = deepcopy(prepared.validation_json)
    schema_validation["checks"]["data_schema"]["columns"][1][
        "data_type"
    ] = "geometry(MultiLineString,3857)"
    schema = PreparedDelivery(
        **{
            **prepared.__dict__,
            "validation_json": schema_validation,
        }
    )
    features = PreparedDelivery(
        **{**prepared.__dict__, "feature_count": 1}
    )
    feature_explosion = PreparedDelivery(
        **{**prepared.__dict__, "feature_count": 2_000}
    )
    raster_validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "raster",
        "checks": {
            "data_schema": {
                "schema_version": "reference-raster-schema/v1",
                "driver": "GTiff",
                "band_types": ["Byte"],
                "nodata_values": [None],
                "pixel_size": {"x": 10.0, "y": 10.0},
            }
        },
    }
    kind = PreparedDelivery(
        **{
            **prepared.__dict__,
            "delivery_kind": "raster",
            "feature_count": None,
            "validation_json": raster_validation,
        }
    )

    expected = (
        (crs, "crs"),
        (bounds, "bounds"),
        (schema, "data_schema"),
        (features, "feature_count"),
        (feature_explosion, "feature_count"),
        (kind, "delivery_kind"),
    )
    for candidate, failed_check in expected:
        evidence = evaluate_delivery_continuity(
            active_version=active,
            candidate=candidate,
        )
        assert evidence["schema_version"] == (
            "reference-delivery-continuity/v1"
        )
        assert evidence["passed"] is False
        assert failed_check in evidence["failed_checks"]
        assert evidence["checks"][failed_check]["passed"] is False

    compatible_bounds = PreparedDelivery(
        **{
            **prepared.__dict__,
            "bounds_json": {
                **prepared.bounds_json,
                "east": -1.5,
            },
        }
    )
    evidence = evaluate_delivery_continuity(
        active_version=active,
        candidate=compatible_bounds,
    )
    bounds_check = evidence["checks"]["bounds"]
    assert evidence["passed"] is True
    assert bounds_check["mode"] == "overlap_and_area_ratio"
    assert bounds_check["passed"] is True
    assert bounds_check["overlap_ratio"] == 1.0
    assert bounds_check["minimum_overlap_ratio"] == 0.8
    assert 0.5 <= bounds_check["area_ratio"] <= 2.0

    growth_evidence = evaluate_delivery_continuity(
        active_version=active,
        candidate=feature_explosion,
    )
    growth_check = growth_evidence["checks"]["feature_count"]
    assert growth_evidence["passed"] is False
    assert growth_check["candidate"] == 2_000
    assert growth_check["maximum_growth_ratio"] == 10.0
    assert growth_check["absolute_growth_allowance"] == 1_000
    assert growth_check["maximum_candidate"] == 1_230

    invalid_baseline_validation = {"passed": True}
    invalid_baseline = SimpleNamespace(
        id=active.id,
        delivery_kind=active.delivery_kind,
        crs=active.crs,
        bounds_json=active.bounds_json,
        feature_count=active.feature_count,
        validation_json=invalid_baseline_validation,
        validation_sha256=canonical_json_sha256(
            invalid_baseline_validation
        ),
    )
    invalid_evidence = evaluate_delivery_continuity(
        active_version=invalid_baseline,
        candidate=prepared,
    )
    assert invalid_evidence["passed"] is False
    assert invalid_evidence["failed_checks"] == ["active_baseline"]
    assert invalid_evidence["checks"]["active_baseline"] == {
        "passed": False,
        "reason": "active_validation_schema_invalid",
    }


def test_builder_rejects_massive_feature_collapse_before_version_creation(
    db,
    tmp_path,
) -> None:
    source, lease, artifact = _lease_and_input(db, tmp_path)
    first = _prepared(artifact)
    built = create_delivery_version(
        db,
        lease=lease,
        prepared=first,
        metadata_verifier=_metadata_verifier(artifact),
        now=NOW + timedelta(seconds=1),
    )
    promote_delivery_version(
        db,
        version_id=built.version_id,
        lease=lease,
        expected_generation=0,
        reason="Initial continuity baseline",
        metadata_verifier=_transition_verifier(artifact),
        now=NOW + timedelta(seconds=2),
    )

    due_at = NOW + timedelta(days=1)
    source = db.get(ReferenceLayerSource, source.id)
    source.next_check_at = due_at
    db.commit()
    queued_run_ids = enqueue_due_sources(db, now=due_at)
    assert len(queued_run_ids) == 1
    second_lease = claim_next_sync_run(
        db,
        now=due_at,
        lease_seconds=300,
        token_factory=lambda: "8" * 64,
    )
    assert second_lease is not None
    assert second_lease.run_id == queued_run_ids[0]
    assert second_lease.source_id == source.id
    bind_run_authorization(db, source, second_lease.run_id)
    second_artifact = ReferenceSourceArtifact(
        source_id=source.id,
        artifact_kind="dataset",
        source_url="https://example.test/zones-v2.geojson",
        final_url="https://example.test/zones-v2.geojson",
        source_version="2026-07-24",
        media_type="application/geo+json",
        storage_backend="filesystem",
        storage_key=f"blobs/sha256/{'c' * 2}/{'c' * 64}",
        size_bytes=512,
        sha256="c" * 64,
        metadata_json={},
        retrieved_at=due_at,
    )
    db.add(second_artifact)
    db.flush()
    db.add(
        ReferenceSyncRunArtifact(
            source_id=source.id,
            run_id=second_lease.run_id,
            artifact_id=second_artifact.id,
            role="input",
        )
    )
    second_fixture = _persist_builder_style_plan(
        db,
        source=source,
        lease=second_lease,
        input_artifact=second_artifact,
        now=due_at,
    )
    second_fixture.source = source
    second_fixture.lease = second_lease
    second_fixture.snapshot = db.get(
        ReferenceLayer,
        source.layer_id,
    ).last_seen_snapshot
    second_run = db.get(ReferenceSyncRun, second_lease.run_id)
    second_fixture.review = db.get(
        ReferenceMirrorAuthorizationReview,
        second_run.mirror_authorization_review_id,
    )
    second_fixture.db = db
    second_fixture.store = artifact.store
    db.commit()
    collapsed = _prepared(second_fixture)
    collapsed = PreparedDelivery(
        **{
            **collapsed.__dict__,
            "content_sha256": "c" * 64,
            "feature_count": 1,
            "assets": (
                PreparedDeliveryAsset(
                    **{
                        **collapsed.assets[0].__dict__,
                        "storage_key": (
                            "reference_data.siur_layer_1_run_2"
                        ),
                        "sha256": "c" * 64,
                    }
                ),
                *collapsed.assets[1:],
            ),
        }
    )
    collapsed = _with_metadata_asset(collapsed, second_fixture)

    with pytest.raises(DeliveryContinuityError) as raised:
        create_delivery_version(
            db,
            lease=second_lease,
            prepared=collapsed,
            metadata_verifier=_metadata_verifier(
                second_fixture
            ),
            now=due_at + timedelta(seconds=1),
        )

    evidence = raised.value.evidence
    assert evidence["passed"] is False
    assert evidence["failed_checks"] == ["feature_count"]
    assert evidence["checks"]["feature_count"]["active"] == 123
    assert evidence["checks"]["feature_count"]["candidate"] == 1
    assert evidence["checks"]["feature_count"]["retained_ratio"] == pytest.approx(
        1 / 123
    )
    versions = list(
        db.scalars(
            select(ReferenceDeliveryVersion).where(
                ReferenceDeliveryVersion.source_id == source.id
            )
        )
    )
    assert [version.id for version in versions] == [built.version_id]
    run = db.get(ReferenceSyncRun, second_lease.run_id)
    assert run.status == "running"


def test_tile_bounds_remain_exact_and_raster_resolution_is_continuous() -> None:
    tile_validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "tiles",
        "checks": {
            "data_schema": {
                "schema_version": "reference-tiles-schema/v1",
                "archives": [
                    {
                        "catalog_style_source_key": None,
                        "image_format": "png",
                        "min_zoom": 0,
                        "max_zoom": 4,
                    }
                ],
            }
        },
    }
    tile_candidate = PreparedDelivery(
        delivery_kind="tiles",
        source_version=None,
        content_sha256="d" * 64,
        reference_at=None,
        crs="EPSG:3857",
        bounds_json={"west": -7, "south": 40, "east": -1, "north": 44},
        feature_count=None,
        validation_json=tile_validation,
        input_artifact_ids=(1,),
        assets=(),
    )
    tile_active = SimpleNamespace(
        id=10,
        delivery_kind="tiles",
        crs="EPSG:3857",
        bounds_json=tile_candidate.bounds_json,
        feature_count=None,
        validation_json=tile_validation,
        validation_sha256=canonical_json_sha256(tile_validation),
    )
    exact = evaluate_delivery_continuity(
        active_version=tile_active,
        candidate=tile_candidate,
    )
    changed_tile_bounds = PreparedDelivery(
        **{
            **tile_candidate.__dict__,
            "bounds_json": {
                **tile_candidate.bounds_json,
                "east": -1.01,
            },
        }
    )
    changed = evaluate_delivery_continuity(
        active_version=tile_active,
        candidate=changed_tile_bounds,
    )
    assert exact["checks"]["bounds"] == {
        "passed": True,
        "mode": "exact_tile_coverage",
        "active": {
            "west": -7.0,
            "south": 40.0,
            "east": -1.0,
            "north": 44.0,
        },
        "candidate": {
            "west": -7.0,
            "south": 40.0,
            "east": -1.0,
            "north": 44.0,
        },
    }
    assert changed["checks"]["bounds"]["passed"] is False

    raster_validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "raster",
        "checks": {
            "data_schema": {
                "schema_version": "reference-raster-schema/v1",
                "driver": "GTiff",
                "band_types": ["UInt16"],
                "nodata_values": [-9999.0],
                "pixel_size": {"x": 5.0, "y": 5.0},
            }
        },
    }
    raster_candidate = PreparedDelivery(
        **{
            **tile_candidate.__dict__,
            "delivery_kind": "raster",
            "validation_json": raster_validation,
        }
    )
    raster_active = SimpleNamespace(
        id=11,
        delivery_kind="raster",
        crs="EPSG:3857",
        bounds_json=raster_candidate.bounds_json,
        feature_count=None,
        validation_json=raster_validation,
        validation_sha256=canonical_json_sha256(raster_validation),
    )
    changed_resolution_validation = deepcopy(raster_validation)
    changed_resolution_validation["checks"]["data_schema"]["pixel_size"][
        "x"
    ] = 10.0
    changed_resolution = PreparedDelivery(
        **{
            **raster_candidate.__dict__,
            "validation_json": changed_resolution_validation,
        }
    )
    raster_evidence = evaluate_delivery_continuity(
        active_version=raster_active,
        candidate=changed_resolution,
    )
    assert raster_evidence["passed"] is False
    assert raster_evidence["failed_checks"] == ["data_schema"]
