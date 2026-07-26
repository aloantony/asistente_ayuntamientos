from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.reference_layers.mirror_orchestrator as mirror_orchestrator
from app.core.config import Settings
from app.reference_layers.acquisition import (
    AcquiredArtifact,
    AcquisitionResult,
    source_candidate_definition_sha256,
)
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.delivery_builder import (
    DeliveryContinuityError,
    PreparedDelivery,
    PreparedDeliveryAsset,
)
from app.reference_layers.geo_ingest import TileArchiveInspection
from app.reference_layers.geoserver_admin import (
    GeoServerLayerSmokeError,
    LayerSmokeResult,
)
from app.reference_layers.mirror_lifecycle import (
    SyncRunLease,
    claim_next_sync_run,
    enqueue_due_sources,
)
from app.reference_layers.mirror_orchestrator import (
    ClassifiedFailure,
    FailureHandlingResult,
    GeoServerPublicationPlan,
    MaterializedDelivery,
    MirrorOrchestrationError,
    MirrorRunProcessor,
    PersistedRunArtifact,
    RunContext,
    StylePublication,
    TileContentCheck,
    TilePublicationPlan,
    _resolve_local_sld_artifacts,
    _tile_documents_for_catalog_styles,
    enqueue_reference_sources_once,
    classify_worker_failure,
    handle_run_failure,
    load_run_context,
    materialize_tile_delivery,
    publish_geoserver_delivery,
    reconcile_reference_sources_once,
)
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSyncRun,
)
from app.reference_layers.source_discovery import SourceCandidate
from app.reference_layers.source_probes import SourceProbe
from app.reference_layers.tile_seed import TileSeedResult

NOW = datetime.now(timezone.utc)


def _definition(provider_key: str = "orchestrator-test") -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://catalog.example.test/settings.json",
        raw_catalog={"version": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service",
                title="Reference service",
                upstream_protocol="wms",
                base_url="https://maps.example.test/wms",
                license_status="approved",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer",
                node_type="layer",
                title="Reference layer",
                service_key="service",
                remote_name="reference",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                bounds={"west": -7, "south": 40, "east": -1, "north": 44},
                min_zoom=0,
                max_zoom=0,
            ),
        ),
        retrieved_at=NOW,
    )


def _candidate(*, priority: int = 0, key: str = "source:xyz") -> SourceCandidate:
    draft = SourceCandidate(
        protocol="xyz",
        target_kind="tiles",
        endpoint_url="https://tiles.example.test/{z}/{x}/{y}.png",
        remote_name="reference",
        sync_strategy="tile_seed",
        priority=priority,
        config={
            "bounds": {"west": -7, "south": 40, "east": -1, "north": 44},
            "min_zoom": 0,
            "max_zoom": 0,
            "format": "image/png",
            "coverage_required": True,
        },
        source_key=key,
        definition_sha256="0" * 64,
    )
    return replace(
        draft,
        definition_sha256=source_candidate_definition_sha256(draft),
    )


def _source(layer: ReferenceLayer, candidate: SourceCandidate) -> ReferenceLayerSource:
    return ReferenceLayerSource(
        provider_key=layer.provider_key,
        layer_id=layer.id,
        source_key=candidate.source_key,
        protocol=candidate.protocol,
        target_kind=candidate.target_kind,
        endpoint_url=candidate.endpoint_url,
        remote_name=candidate.remote_name,
        sync_strategy=candidate.sync_strategy,
        config_json=dict(candidate.config),
        definition_sha256=candidate.definition_sha256,
        enabled=True,
        is_primary=candidate.priority == 0,
        priority=candidate.priority,
        next_check_at=NOW - timedelta(seconds=1),
    )


def _seed_source(db, *, two_sources: bool = False):
    definition = _definition()
    apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == definition.provider_key,
            ReferenceLayer.source_key == "layer",
        )
    )
    sources = [_source(layer, _candidate())]
    if two_sources:
        sources.append(
            _source(
                layer,
                _candidate(priority=10, key="source:xyz:fallback"),
            )
        )
    db.add_all(sources)
    db.commit()
    return layer, sources


def _wms_document() -> dict:
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "wms_tiles",
        "definition_sha256": "a" * 64,
        "descriptor": {
            "bounds": {"west": -180.0, "south": -85.0, "east": 180.0, "north": 85.0},
            "min_zoom": 0,
            "max_zoom": 0,
            "format": "image/png",
            "coverage_required": True,
            "estimated_tile_count": 1,
            "max_tile_count": 10,
            "layer": "planning:zones",
            "style": "default",
            "crs": "EPSG:3857",
            "kvp": {
                "endpoint_url": "https://maps.example.test/geoserver/ows",
                "service": "WMS",
                "request": "GetMap",
                "version": "1.3.0",
                "layers": "planning:zones",
                "styles": "default",
                "format": "image/png",
                "transparent": "TRUE",
                "CRS": "EPSG:3857",
                "bbox_placeholder": "{bbox}",
                "width_placeholder": "{width}",
                "height_placeholder": "{height}",
            },
        },
    }


def _styled_context() -> RunContext:
    layer = ReferenceLayer(
        id=11,
        last_seen_snapshot_id=1,
        service_id=1,
        provider_key="styled",
        source_key="layer",
        node_type="layer",
        title="Styled layer",
        remote_name="planning:zones",
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        status="active",
    )
    source = ReferenceLayerSource(
        id=21,
        provider_key="styled",
        layer_id=11,
        source_key="source",
        protocol="wms_tiles",
        target_kind="tiles",
        endpoint_url="https://maps.example.test/geoserver/ows",
        remote_name="planning:zones",
        sync_strategy="tile_seed",
        config_json={},
        definition_sha256="a" * 64,
        enabled=True,
        priority=0,
    )
    run = ReferenceSyncRun(
        id=31,
        source_id=21,
        source_definition_json={},
        source_definition_sha256="a" * 64,
        trigger_kind="scheduled",
        check_mode="full",
        status="running",
        lease_token="b" * 64,
        lease_expires_at=NOW + timedelta(hours=1),
        heartbeat_at=NOW,
        started_at=NOW,
    )
    styles = (
        ReferenceLayerStyle(
            id=41,
            last_seen_snapshot_id=1,
            layer_id=11,
            provider_key="styled",
            source_key="style:default",
            remote_name="default",
            title="Default",
            is_default=True,
            status="active",
        ),
        ReferenceLayerStyle(
            id=42,
            last_seen_snapshot_id=1,
            layer_id=11,
            provider_key="styled",
            source_key="style:alternate",
            remote_name="alternate",
            title="Alternate",
            is_default=False,
            status="active",
        ),
    )
    lease = SyncRunLease(31, 21, "b" * 64, 1, NOW + timedelta(hours=1))
    return RunContext(lease, source, run, layer, styles, None, None, None, None)


def _tile_acquisition() -> AcquisitionResult:
    return AcquisitionResult(
        source_key="source",
        source_definition_sha256="a" * 64,
        protocol="wms_tiles",
        target_kind="tiles",
        not_modified=False,
        artifacts=(),
        manifest_sha256="c" * 64,
        probe=SourceProbe(
            available=True,
            protocol="wms_tiles",
            requested_name="planning:zones",
            canonical_name="planning:zones",
            service_version="1.3.0",
            fingerprint_sha256="d" * 64,
            fingerprint_quality="weak",
            metadata={
                "name": "planning:zones",
                "styles": ["default", "alternate"],
            },
        ),
        observed_etag=None,
        observed_last_modified=None,
        observed_version="1.3.0",
        feature_count=None,
        total_bytes=0,
        stats={},
    )


class FakeSupervisor:
    def __init__(self):
        self.pulses = 0

    def pulse(self, *, force: bool = False):
        del force
        self.pulses += 1

    def tile_heartbeat(self, completed: int, total: int):
        del completed, total
        self.pulses += 1


def test_raster_materialization_routes_geotiff_zip_to_safe_zip_driver(
    monkeypatch,
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "raster-zip-store"),
        max_blob_bytes=1024 * 1024,
    )
    blob = store.put_stream(io.BytesIO(b"PK\x03\x04archive"))
    artifact = PersistedRunArtifact(
        artifact_id=1,
        artifact_kind="dataset",
        roles=frozenset({"input"}),
        media_type="application/zip",
        storage_backend="filesystem",
        storage_key=blob.storage_key,
        size_bytes=blob.size_bytes,
        sha256=blob.sha256,
        metadata_json={"data_format": "geotiff-zip"},
    )
    captured = {}
    raster_result = object()

    def ingest(_store, **kwargs):
        captured.update(kwargs)
        return raster_result

    monkeypatch.setattr(
        mirror_orchestrator.geo_ingest,
        "ingest_raster_artifact",
        ingest,
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "_geoserver_materialization",
        lambda *_args, **kwargs: kwargs["raster"],
    )
    supervisor = FakeSupervisor()
    try:
        result = mirror_orchestrator.materialize_raster_delivery(
            store,
            SimpleNamespace(
                styles=(),
                source=SimpleNamespace(
                    config_json={"max_pixels": 1_600_000_000}
                ),
            ),
            SimpleNamespace(),
            (artifact,),
            supervisor,
            max_source_bytes=1024 * 1024,
            max_output_bytes=1024 * 1024,
            timeout_seconds=30,
        )
    finally:
        store.close()

    assert result is raster_result
    assert captured["input_driver"] == "GTiffZIP"
    assert captured["input_sha256"] == blob.sha256
    assert captured["max_pixels"] == 1_600_000_000
    assert supervisor.pulses == 1


def test_vector_materialization_routes_ordered_cadastral_zip_parts(
    monkeypatch,
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        Path(tmp_path, "gml-zip-store"),
        max_blob_bytes=1024 * 1024,
    )
    blobs = [
        store.put_stream(io.BytesIO(b"PK\x03\x04second")),
        store.put_stream(io.BytesIO(b"PK\x03\x04first")),
    ]
    artifacts = tuple(
        PersistedRunArtifact(
            artifact_id=index + 1,
            artifact_kind="dataset",
            roles=frozenset({"input"}),
            media_type="application/zip",
            storage_backend="filesystem",
            storage_key=blob.storage_key,
            size_bytes=blob.size_bytes,
            sha256=blob.sha256,
            metadata_json={
                "data_format": "inspire-cadastral-parcel-gml-zip",
                "input_layer": "CadastralParcel",
                "page_index": page_index,
            },
        )
        for index, (blob, page_index) in enumerate(
            ((blobs[0], 1), (blobs[1], 0))
        )
    )
    captured = {}
    vector_result = object()

    def ingest(_db, **kwargs):
        captured.update(kwargs)
        return vector_result

    monkeypatch.setattr(
        mirror_orchestrator.geo_ingest,
        "ingest_vector_artifacts",
        ingest,
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "_geoserver_materialization",
        lambda *_args, **kwargs: kwargs["vector"],
    )
    context = SimpleNamespace(
        styles=(),
        source=SimpleNamespace(
            provider_key="siur",
            layer_id=20,
            config_json={},
        ),
        run=SimpleNamespace(id=30),
    )
    supervisor = FakeSupervisor()
    try:
        result = mirror_orchestrator.materialize_vector_delivery(
            lambda: nullcontext(object()),
            store,
            context,
            SimpleNamespace(),
            artifacts,
            supervisor,
            database_url=(
                "postgresql+psycopg://app:secret@127.0.0.1:5432/app"
            ),
            max_source_bytes=1024 * 1024,
            timeout_seconds=30,
        )
    finally:
        store.close()

    assert result is vector_result
    assert captured["input_driver"] == "GMLZIP"
    assert [item[1] for item in captured["artifacts"]] == [
        blobs[1].sha256,
        blobs[0].sha256,
    ]
    assert all(
        item[2] == "CadastralParcel"
        for item in captured["artifacts"]
    )
    assert supervisor.pulses == 2


def test_geoserver_publication_smokes_every_local_style_and_returns_audit(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(Path(tmp_path, "smoke-store"))
    first = store.put_stream(io.BytesIO(b"<sld>first</sld>"))
    second = store.put_stream(io.BytesIO(b"<sld>second</sld>"))
    plan = GeoServerPublicationPlan(
        delivery_kind="vector",
        layer_name="planning_v_012345",
        title="Planning",
        primary_storage_key="reference_data.planning_v_012345",
        declared_srs="EPSG:3857",
        table_name="planning_v_012345",
        store_name=None,
        styles=(
            StylePublication(1, "style_one_v_012345", first.storage_key, first.sha256),
            StylePublication(
                2,
                "style_two_v_012345",
                second.storage_key,
                second.sha256,
            ),
        ),
        smoke_style_names=(
            "style_one_v_012345",
            "style_two_v_012345",
        ),
        legend_available=True,
        identify_available=True,
    )

    class Client:
        def __init__(self):
            self.smokes = []

        def health(self):
            return None

        def publish_versioned_table(self, **kwargs):
            return kwargs

        def publish_immutable_sld(self, **kwargs):
            return kwargs

        def ensure_layer_style(self, **kwargs):
            return kwargs

        def smoke_layer(self, **kwargs):
            self.smokes.append(kwargs)
            style = kwargs["style_name"]
            return LayerSmokeResult(
                layer_name=kwargs["layer_name"],
                style_name=style,
                image_sha256="a" * 64,
                image_bytes=100,
                image_content_type="image/png",
                z=kwargs["z"],
                x=kwargs["x"],
                y=kwargs["y"],
                legend_sha256="b" * 64,
                legend_bytes=50,
                legend_content_type="image/png",
                identify_sha256="c" * 64,
                identify_bytes=42,
                identify_content_type="application/geo+json",
                identify_feature_count=0,
                pixel_x=kwargs["pixel_x"],
                pixel_y=kwargs["pixel_y"],
            )

    client = Client()
    try:
        evidence = publish_geoserver_delivery(
            store,
            client,
            plan,
            FakeSupervisor(),
        )
    finally:
        store.close()

    assert len(client.smokes) == 2
    assert all(item["legend_available"] is True for item in client.smokes)
    assert all(item["identify_available"] is True for item in client.smokes)
    assert all((item["z"], item["x"], item["y"]) == (0, 0, 0) for item in client.smokes)
    assert all(
        (item["pixel_x"], item["pixel_y"]) == (128, 128)
        for item in client.smokes
    )
    assert evidence["transport"] == "numeric_loopback_http"
    assert len(evidence["style_checks"]) == 2
    assert evidence["style_checks"][0]["identify"]["feature_count"] == 0


def test_baked_styles_are_verified_and_seeded_as_distinct_local_assets(tmp_path) -> None:
    store = ReferenceBlobStore(Path(tmp_path, "store"), max_blob_bytes=1024 * 1024)
    try:
        document = _wms_document()
        descriptor_blob = store.put_stream(
            io.BytesIO(json.dumps(document).encode()),
            max_bytes=1024 * 1024,
        )
        descriptor = PersistedRunArtifact(
            artifact_id=1,
            artifact_kind="metadata",
            roles=frozenset({"input"}),
            media_type="application/json",
            storage_backend="filesystem",
            storage_key=descriptor_blob.storage_key,
            size_bytes=descriptor_blob.size_bytes,
            sha256=descriptor_blob.sha256,
            metadata_json={"schema": "reference-tile-source/v1"},
        )
        seeded_styles = []
        inspected = []

        def seed(store, *, source_document, **kwargs):
            del kwargs
            style = source_document["descriptor"]["style"]
            seeded_styles.append(style)
            blob = store.put_stream(io.BytesIO(f"archive:{style}".encode()))
            return TileSeedResult(
                blob=blob,
                tile_count=1,
                coordinate_sha256=("1" if style == "default" else "2") * 64,
                min_zoom=0,
                max_zoom=0,
                image_format="png",
                bounds_json=document["descriptor"]["bounds"],
                validation_json={"passed": True},
            )

        def inspect(store, **kwargs):
            del store
            inspected.append(kwargs)
            return TileArchiveInspection(
                tile_count=1,
                min_zoom=0,
                max_zoom=0,
                image_format="png",
                coordinate_sha256=kwargs[
                    "expected_coordinate_sha256"
                ],
                bounds_json=document["descriptor"]["bounds"],
                validation_json={"passed": True, "checks": kwargs},
            )

        result = materialize_tile_delivery(
            store,
            _styled_context(),
            _tile_acquisition(),
            (descriptor,),
            FakeSupervisor(),
            max_archive_bytes=1024 * 1024,
            max_tiles=10,
            concurrency=1,
            batch_size=1,
            seed=seed,
            inspect=inspect,
        )

        assert seeded_styles == ["default", "alternate"]
        assert [item.metadata_json["catalog_style_id"] for item in result.prepared.assets] == [41, 42]
        assert all(
            "legend_available" not in item.metadata_json
            and "identify_available" not in item.metadata_json
            for item in result.prepared.assets
        )
        assert [item.is_primary for item in result.prepared.assets] == [True, False]
        assert [item["expected_coordinate_sha256"] for item in inspected] == ["1" * 64, "2" * 64]
        assert isinstance(result.publication, TilePublicationPlan)
    finally:
        store.close()


def test_baked_style_must_be_advertised_by_the_exact_probe() -> None:
    acquired = _tile_acquisition()
    acquired = replace(
        acquired,
        probe=replace(acquired.probe, metadata={"styles": ["default"]}),
    )
    with pytest.raises(MirrorOrchestrationError) as raised:
        _tile_documents_for_catalog_styles(
            _wms_document(),
            _styled_context(),
            acquired,
        )
    assert raised.value.code == "tile_style_unverifiable"


def test_local_sld_artifacts_resolve_by_portable_style_source_key() -> None:
    artifact = PersistedRunArtifact(
        artifact_id=5,
        artifact_kind="style",
        roles=frozenset({"style"}),
        media_type="application/vnd.ogc.sld+xml",
        storage_backend="filesystem",
        storage_key="blobs/sha256/aa/" + "a" * 64,
        size_bytes=100,
        sha256="a" * 64,
        metadata_json={"catalog_style_source_key": "style:default"},
    )
    alternate = replace(
        artifact,
        artifact_id=6,
        storage_key="blobs/sha256/bb/" + "b" * 64,
        sha256="b" * 64,
        metadata_json={"catalog_style_source_key": "style:alternate"},
    )
    resolved = _resolve_local_sld_artifacts(
        _styled_context(),
        (artifact, alternate),
    )
    assert set(resolved) == {41, 42}


def test_missing_local_sld_rejects_geoserver_delivery() -> None:
    with pytest.raises(MirrorOrchestrationError) as raised:
        _resolve_local_sld_artifacts(_styled_context(), ())
    assert raised.value.code == "local_style_missing"


def test_failure_handler_uses_lifecycle_followup_after_terminal_finish(db) -> None:
    _, sources = _seed_source(db, two_sources=True)
    fallback_source_id = sources[1].id
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        now=NOW,
        lease_seconds=3600,
        token_factory=lambda: "e" * 64,
    )
    result = handle_run_failure(
        lambda: nullcontext(db),
        lease,
        ClassifiedFailure(
            code="local_style_missing",
            summary="Local style is unavailable.",
            retryable=False,
            outcome="rejected",
            stats_json={"continuity_audit": {"passed": False}},
        ),
    )

    assert result.followup == "fallback"
    assert result.source_id == fallback_source_id
    assert db.get(ReferenceSyncRun, lease.run_id).stats_json == {
        "retryable_classification": False,
        "continuity_audit": {"passed": False},
    }
    child = db.scalar(
        select(ReferenceSyncRun).where(
            ReferenceSyncRun.parent_run_id == lease.run_id
        )
    )
    assert child is not None
    assert child.source_id == fallback_source_id


def test_operation_smoke_evidence_is_finalized_with_the_promoted_run(
    monkeypatch,
    tmp_path,
) -> None:
    context = replace(_styled_context(), active_version_id=77)
    evidence = {
        "local_operation_smoke": {
            "schema_version": "reference-local-operation-smoke/v1",
            "renderer": "geoserver",
            "style_checks": [{"map": {"sha256": "a" * 64}}],
        }
    }
    promoted = []

    class Supervisor(FakeSupervisor):
        def __init__(self, factory, lease, **kwargs):
            del factory, kwargs
            super().__init__()
            self.lease = lease

        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

    monkeypatch.setattr(mirror_orchestrator, "LeaseSupervisor", Supervisor)
    monkeypatch.setattr(
        mirror_orchestrator,
        "load_run_context",
        lambda factory, lease: context,
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "persist_run_acquisition",
        lambda factory, lease, acquired: (),
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "persist_run_style_parity",
        lambda factory, context, artifacts, acquired: SimpleNamespace(
            complete=True
        ),
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "persist_delivery_version",
        lambda factory, lease, prepared: SimpleNamespace(version_id=88),
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "promote_existing_delivery",
        lambda *args, **kwargs: promoted.append(kwargs),
    )
    prepared = PreparedDelivery(
        delivery_kind="tiles",
        source_version="v1",
        content_sha256="a" * 64,
        reference_at=None,
        crs="EPSG:3857",
        bounds_json={"west": -7, "south": 40, "east": -1, "north": 44},
        feature_count=None,
        validation_json={"passed": True},
        input_artifact_ids=(),
        assets=(),
    )
    store = ReferenceBlobStore(Path(tmp_path, "success-store"))
    processor = MirrorRunProcessor(
        session_factory=lambda: nullcontext(None),
        store=store,
        acquisition=SimpleNamespace(
            acquire=lambda *args, **kwargs: _tile_acquisition()
        ),
        config=Settings(
            reference_storage_root=str(store.root),
            reference_mirror_lease_seconds=600,
            reference_mirror_heartbeat_seconds=100,
        ),
        materializer=lambda *args: MaterializedDelivery(
            prepared,
            TilePublicationPlan((), requires_full_inspection=False),
        ),
        publisher=lambda *args: evidence,
    )
    try:
        result = processor.process_lease(context.lease)
    finally:
        processor.close()

    assert result.state == "succeeded"
    assert promoted[0]["stats"]["local_operation_smoke"] == evidence[
        "local_operation_smoke"
    ]


def test_operation_smoke_failure_prevents_promotion_and_preserves_active(
    monkeypatch,
    tmp_path,
) -> None:
    context = replace(_styled_context(), active_version_id=77)
    active = {"version_id": 77}
    promotion_calls = []

    class Supervisor(FakeSupervisor):
        def __init__(self, factory, lease, **kwargs):
            del factory, kwargs
            super().__init__()
            self.lease = lease

        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

    monkeypatch.setattr(mirror_orchestrator, "LeaseSupervisor", Supervisor)
    monkeypatch.setattr(
        mirror_orchestrator,
        "load_run_context",
        lambda factory, lease: context,
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "persist_run_acquisition",
        lambda factory, lease, acquired: (),
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "persist_run_style_parity",
        lambda factory, context, artifacts, acquired: SimpleNamespace(
            complete=True
        ),
    )
    monkeypatch.setattr(
        mirror_orchestrator,
        "persist_delivery_version",
        lambda factory, lease, prepared: SimpleNamespace(version_id=88),
    )

    def promote(*args, **kwargs):
        promotion_calls.append((args, kwargs))
        active["version_id"] = 88

    monkeypatch.setattr(
        mirror_orchestrator,
        "promote_existing_delivery",
        promote,
    )
    prepared = PreparedDelivery(
        delivery_kind="tiles",
        source_version="v1",
        content_sha256="a" * 64,
        reference_at=None,
        crs="EPSG:3857",
        bounds_json={"west": -7, "south": 40, "east": -1, "north": 44},
        feature_count=None,
        validation_json={"passed": True},
        input_artifact_ids=(),
        assets=(),
    )

    def fail_smoke(*args):
        del args
        raise GeoServerLayerSmokeError("local operation failed")

    store = ReferenceBlobStore(Path(tmp_path, "failure-store"))
    processor = MirrorRunProcessor(
        session_factory=lambda: nullcontext(None),
        store=store,
        acquisition=SimpleNamespace(
            acquire=lambda *args, **kwargs: _tile_acquisition()
        ),
        config=Settings(
            reference_storage_root=str(store.root),
            reference_mirror_lease_seconds=600,
            reference_mirror_heartbeat_seconds=100,
        ),
        materializer=lambda *args: MaterializedDelivery(
            prepared,
            TilePublicationPlan((), requires_full_inspection=False),
        ),
        publisher=fail_smoke,
        failure_handler=lambda factory, lease, failure: FailureHandlingResult(
            lease.run_id,
            failure.outcome,
            "none",
        ),
    )
    try:
        result = processor.process_lease(context.lease)
    finally:
        processor.close()

    assert result.state == "failed"
    assert result.error_code == "local_renderer_failed"
    assert promotion_calls == []
    assert active["version_id"] == 77


def test_continuity_rejection_classification_preserves_audit_evidence() -> None:
    evidence = {
        "schema_version": "reference-delivery-continuity/v1",
        "passed": False,
        "failed_checks": ["feature_count"],
    }

    failure = classify_worker_failure(DeliveryContinuityError(evidence))

    assert failure.code == "delivery_continuity_rejected"
    assert failure.outcome == "rejected"
    assert failure.retryable is False
    assert failure.stats_json == {"delivery_continuity": evidence}


def test_scheduler_reconciles_sources_before_enqueue_on_fresh_catalog(db) -> None:
    definition = _definition("bootstrap-test")
    apply_catalog_definition(db, definition)

    first = reconcile_reference_sources_once(lambda: nullcontext(db))
    second = reconcile_reference_sources_once(lambda: nullcontext(db))
    run_ids = enqueue_reference_sources_once(
        lambda: nullcontext(db),
        reconcile=False,
    )

    assert len(first) == 1
    assert first[0].provider_key == definition.provider_key
    assert first[0].created_count > 0
    assert second[0].created_count == 0
    assert second[0].updated_count == 0
    assert run_ids


def test_conditional_tiles_reuse_active_archive_until_definition_changes(
    db,
    tmp_path,
) -> None:
    layer, [source] = _seed_source(db)
    source_id = source.id
    original_definition_sha256 = source.definition_sha256
    enqueue_due_sources(db, now=NOW)
    store = ReferenceBlobStore(
        Path(tmp_path, "processor-store"),
        max_blob_bytes=1024 * 1024,
    )
    descriptor_blob = store.put_stream(io.BytesIO(b'{"descriptor":true}'))
    delivery_blob = store.put_stream(io.BytesIO(b"validated-mbtiles"))
    acquired = AcquisitionResult(
        source_key=source.source_key,
        source_definition_sha256=source.definition_sha256,
        protocol=source.protocol,
        target_kind=source.target_kind,
        not_modified=False,
        artifacts=(
            AcquiredArtifact(
                artifact_kind="metadata",
                role="input",
                media_type="application/json",
                blob=descriptor_blob,
                metadata={"schema": "reference-tile-source/v1"},
            ),
        ),
        manifest_sha256=descriptor_blob.sha256,
        probe=None,
        observed_etag=None,
        observed_last_modified=None,
        observed_version="test-v1",
        feature_count=None,
        total_bytes=descriptor_blob.size_bytes,
        stats={"artifact_count": 1},
    )

    class Acquisition:
        def acquire(self, candidate, **kwargs):
            del kwargs
            return replace(
                acquired,
                source_key=candidate.source_key,
                source_definition_sha256=candidate.definition_sha256,
                protocol=candidate.protocol,
                target_kind=candidate.target_kind,
            )

    materialized = []

    def materializer(context, result, artifacts, supervisor):
        del result
        materialized.append(context.run.id)
        supervisor.pulse()
        prepared = PreparedDelivery(
            delivery_kind="tiles",
            source_version="test-v1",
            content_sha256=delivery_blob.sha256,
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
            input_artifact_ids=tuple(
                item.artifact_id for item in artifacts if "input" in item.roles
            ),
            assets=(
                PreparedDeliveryAsset(
                    asset_key="primary",
                    asset_kind="tile_archive",
                    is_primary=True,
                    storage_backend="filesystem",
                    storage_key=delivery_blob.storage_key,
                    media_type="application/vnd.mapbox.mbtiles",
                    sha256=delivery_blob.sha256,
                    size_bytes=delivery_blob.size_bytes,
                    metadata_json={
                        "renderer": "tile_archive",
                        "expected_tile_count": 1,
                        "expected_coordinate_sha256": "a" * 64,
                        "smoke_coordinate": [0, 0, 0],
                    },
                ),
            ),
        )
        return MaterializedDelivery(
            prepared,
            TilePublicationPlan((), requires_full_inspection=False),
        )

    published = []
    processor = MirrorRunProcessor(
        session_factory=lambda: nullcontext(db),
        store=store,
        acquisition=Acquisition(),
        config=Settings(
            reference_storage_root=str(store.root),
            reference_mirror_lease_seconds=600,
            reference_mirror_heartbeat_seconds=100,
        ),
        materializer=materializer,
        publisher=lambda context, plan, supervisor: published.append(
            (context, plan)
        ),
        tile_content_checker=lambda *args: TileContentCheck(
            unchanged=True,
            sample_count=1,
            remote_sha256="d" * 64,
            local_sha256="d" * 64,
        ),
    )
    try:
        first = processor.process_next()
        assert first.state == "succeeded"
        assert len(materialized) == 1

        current_source = db.get(ReferenceLayerSource, source_id)
        current_source.next_check_at = NOW
        db.commit()
        enqueue_due_sources(db, now=NOW + timedelta(minutes=1))
        conditional = processor.process_next()

        assert conditional.state == "unchanged"
        assert conditional.version_id == first.version_id
        assert len(materialized) == 1
        assert len(published) == 2
        assert isinstance(published[1][1], TilePublicationPlan)
        assert published[1][1].requires_full_inspection is True

        current_source = db.get(ReferenceLayerSource, source_id)
        candidate = _candidate()
        changed_draft = replace(
            candidate,
            config={**candidate.config, "max_zoom": 1},
            definition_sha256="0" * 64,
        )
        changed = replace(
            changed_draft,
            definition_sha256=source_candidate_definition_sha256(changed_draft),
        )
        current_source.config_json = dict(changed.config)
        current_source.definition_sha256 = changed.definition_sha256
        current_source.next_check_at = NOW
        db.commit()
        enqueue_due_sources(db, now=NOW + timedelta(minutes=2))
        changed_lease = claim_next_sync_run(
            db,
            lease_seconds=3600,
            token_factory=lambda: "9" * 64,
        )
        changed_context = load_run_context(
            lambda: nullcontext(db),
            changed_lease,
        )
    finally:
        processor.close()

    assert changed_context.run.check_mode == "conditional"
    assert changed_context.run.source_definition_sha256 != original_definition_sha256
    assert changed_context.conditional is None
    assert changed_context.active_manifest_sha256 is None
    state = db.scalar(
        select(ReferenceLayerDeliveryState).where(
            ReferenceLayerDeliveryState.provider_key == layer.provider_key,
            ReferenceLayerDeliveryState.layer_id == layer.id,
        )
    )
    assert state.active_version_id == first.version_id


def test_reference_settings_reject_incoherent_lease_and_quota(tmp_path) -> None:
    defaults = Settings(
        reference_storage_root=str(tmp_path),
        _env_file=None,
    )
    opted_in = Settings(
        reference_storage_root=str(tmp_path),
        reference_remote_proxy_enabled=True,
        _env_file=None,
    )
    assert defaults.reference_remote_proxy_enabled is False
    assert opted_in.reference_remote_proxy_enabled is True

    with pytest.raises(ValueError, match="half the lease"):
        Settings(
            reference_storage_root=str(tmp_path),
            reference_mirror_lease_seconds=60,
            reference_mirror_heartbeat_seconds=30,
        )
    with pytest.raises(ValueError, match="storage quota"):
        Settings(
            reference_storage_root=str(tmp_path),
            reference_blob_max_bytes=10 * 1024 * 1024,
            reference_storage_quota_bytes=5 * 1024 * 1024,
        )
