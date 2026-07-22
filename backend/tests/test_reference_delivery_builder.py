from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.delivery_builder import (
    DeliveryBuildError,
    PreparedDelivery,
    PreparedDeliveryAsset,
    canonical_json_sha256,
    create_delivery_version,
)
from app.reference_layers.mirror_lifecycle import (
    MirrorLeaseLostError,
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
    claim_next_sync_run,
    enqueue_due_sources,
)
from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
    ReferenceLayerSource,
    ReferenceSourceArtifact,
    ReferenceSyncRunArtifact,
)

NOW = datetime(2026, 7, 23, 8, tzinfo=timezone.utc)


def _lease_and_input(db):
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
        item.next_check_at = NOW - timedelta(seconds=1)
    db.commit()
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=300,
        token_factory=lambda: "7" * 64,
    )
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
    db.commit()
    return source, lease, artifact


def _prepared(artifact_id: int) -> PreparedDelivery:
    validation = {
        "passed": True,
        "checks": ["schema", "bounds", "geometry", "renderer_smoke"],
    }
    return PreparedDelivery(
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
                    "default_style_name": None,
                    "styles": {"12": "siur_style_12_v1"},
                    "identify_available": True,
                    "legend_available": True,
                },
            ),
        ),
    )


def test_create_delivery_version_links_inputs_and_assets(db) -> None:
    source, lease, artifact = _lease_and_input(db)
    prepared = _prepared(artifact.id)

    built = create_delivery_version(
        db,
        lease=lease,
        prepared=prepared,
        now=NOW + timedelta(seconds=1),
    )

    version = db.get(ReferenceDeliveryVersion, built.version_id)
    asset = db.scalar(
        select(ReferenceDeliveryAsset).where(
            ReferenceDeliveryAsset.version_id == version.id
        )
    )
    assert version.source_id == source.id
    assert version.sequence_number == 1
    assert version.validation_sha256 == canonical_json_sha256(
        prepared.validation_json
    )
    assert built.manifest_sha256 == version.manifest_sha256
    assert asset.storage_key == "reference_data.siur_layer_1_run_1"
    assert asset.metadata_json["renderer"] == "geoserver"


def test_builder_rejects_unlinked_input_and_expired_lease(db) -> None:
    _, lease, artifact = _lease_and_input(db)
    with pytest.raises(DeliveryBuildError, match="not immutable artifacts"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=_prepared(artifact.id + 999),
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(MirrorLeaseLostError):
        create_delivery_version(
            db,
            lease=lease,
            prepared=_prepared(artifact.id),
            now=NOW + timedelta(seconds=301),
        )


def test_builder_rejects_unsafe_or_inconsistent_primary_asset(db) -> None:
    _, lease, artifact = _lease_and_input(db)
    prepared = _prepared(artifact.id)
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
        create_delivery_version(db, lease=lease, prepared=unsafe, now=NOW)

    mismatch = PreparedDelivery(
        **{**prepared.__dict__, "content_sha256": "c" * 64}
    )
    with pytest.raises(DeliveryBuildError, match="content hash"):
        create_delivery_version(db, lease=lease, prepared=mismatch, now=NOW)


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
        validation_json={"passed": True},
        input_artifact_ids=(1,),
        assets=(asset,),
    )
    with pytest.raises(DeliveryBuildError, match="content-addressed"):
        # Validation happens before database access, so a session is not needed.
        create_delivery_version(None, lease=None, prepared=prepared)
