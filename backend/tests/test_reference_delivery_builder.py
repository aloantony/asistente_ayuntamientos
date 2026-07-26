from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
    DeliveryContinuityError,
    PreparedDelivery,
    PreparedDeliveryAsset,
    canonical_json_sha256,
    create_delivery_version,
    evaluate_delivery_continuity,
)
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
    ReferenceSourceArtifact,
    ReferenceSyncRun,
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
        item.is_primary = item.id == source.id
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
        version.validation_json
    )
    assert version.validation_json["continuity_gate"]["passed"] is True
    assert (
        version.validation_json["continuity_gate"]["baseline_version_id"]
        is None
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

    oversized_version = PreparedDelivery(
        **{**prepared.__dict__, "source_version": "v" * 2_049}
    )
    with pytest.raises(DeliveryBuildError, match="source version"):
        create_delivery_version(
            db,
            lease=lease,
            prepared=oversized_version,
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
        create_delivery_version(None, lease=None, prepared=prepared)


def test_continuity_report_checks_kind_crs_bounds_schema_and_features(db) -> None:
    _, lease, artifact = _lease_and_input(db)
    prepared = _prepared(artifact.id)
    built = create_delivery_version(
        db,
        lease=lease,
        prepared=prepared,
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
) -> None:
    source, lease, artifact = _lease_and_input(db)
    first = _prepared(artifact.id)
    built = create_delivery_version(
        db,
        lease=lease,
        prepared=first,
        now=NOW + timedelta(seconds=1),
    )
    promote_delivery_version(
        db,
        version_id=built.version_id,
        lease=lease,
        expected_generation=0,
        reason="Initial continuity baseline",
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
    db.commit()
    collapsed = _prepared(second_artifact.id)
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
            ),
        }
    )

    with pytest.raises(DeliveryContinuityError) as raised:
        create_delivery_version(
            db,
            lease=second_lease,
            prepared=collapsed,
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
