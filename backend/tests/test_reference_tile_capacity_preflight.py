import hashlib
from io import BytesIO
import json
import threading

from PIL import Image
import pytest
from sqlalchemy import event, func, select, update

from app.reference_layers.acquisition import AcquisitionValidationError
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
    ReferenceStorageCapacity,
)
from app.reference_layers.mirror_admin import _parser
from app.reference_layers.models import (
    ReferenceDeliveryVersion,
    ReferenceLayerMirrorStrategy,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceMirrorAuthorizationReview,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
)
from app.reference_layers.safe_download import HTTPSDownloadResult
from app.reference_layers.source_probes import SourceProbe
from app.reference_layers.tile_capacity_preflight import (
    TileCapacityPreflightError,
    TileCapacityPreflightInputError,
    TileSourceResolution,
    _tile_document_variants,
    aggregate_tile_capacity_preflight,
    preflight_tile_variant,
    resolve_tile_source,
    tile_capacity_preflight_exit_code,
)
from app.reference_layers.tile_seed import (
    PREFLIGHT_PROJECTION_SCHEMA,
    TileSeedError,
    TileSeedPreflight,
    authorized_tile_fetcher,
)
from support_reference_mirror_authorization import authorize_mirror_source
from test_reference_mirror_lifecycle import _seed_bootstrap


class CapacityStore:
    max_blob_bytes = 1_000_000
    quota_bytes = 1_000
    min_free_bytes = 100

    def __init__(self, *, available_bytes: int = 900) -> None:
        self.inspect_calls = 0
        self.mutation_calls = 0
        self.capacity = ReferenceStorageCapacity(
            used_bytes=100,
            free_bytes=available_bytes + 100,
            quota_bytes=1_000,
            min_free_bytes=100,
        )

    def inspect_capacity(self):
        self.inspect_calls += 1
        return self.capacity

    def ensure_capacity(self, _additional_bytes):
        self.mutation_calls += 1
        raise AssertionError("the shared CAS must not be reserved")

    def stage(self, **_kwargs):
        self.mutation_calls += 1
        raise AssertionError("the shared CAS must not be staged")


def _seed_tile_strategy(db, provider_key: str):
    _definition, layer, snapshot, sources, _applied = _seed_bootstrap(
        db,
        provider_key=provider_key,
    )
    strategy = db.scalar(
        select(ReferenceLayerMirrorStrategy).where(
            ReferenceLayerMirrorStrategy.provider_key == provider_key,
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot.id,
        )
    )
    source = next(item for item in sources if item.target_kind == "tiles")
    assert strategy is not None
    for item in sources:
        item.is_primary = False
    db.flush()
    source.is_primary = True
    strategy.strategy = "tiles"
    strategy.source_id = source.id
    db.commit()
    assert source is not None and source.protocol == "wms_tiles"
    return layer, snapshot, source, strategy


def _resolution(source, styles) -> TileSourceResolution:
    remote_names = [style.remote_name for style in styles]
    default = next(style.remote_name for style in styles if style.is_default)
    document = {
        "schema": "reference-tile-source/v1",
        "protocol": "wms_tiles",
        "definition_sha256": source.definition_sha256,
        "descriptor": {
            "bounds": {
                "west": -7.0,
                "south": 40.0,
                "east": -1.0,
                "north": 43.0,
            },
            "min_zoom": 0,
            "max_zoom": 0,
            "format": "image/png",
            "coverage_required": True,
            "estimated_tile_count": 1,
            "max_tile_count": 100,
            "layer": source.remote_name,
            "style": default,
            "crs": "EPSG:3857",
            "kvp": {
                "endpoint_url": source.endpoint_url,
                "service": "WMS",
                "request": "GetMap",
                "version": "1.3.0",
                "layers": source.remote_name,
                "styles": default,
                "format": "image/png",
                "transparent": "TRUE",
                "CRS": "EPSG:3857",
                "bbox_placeholder": "{bbox}",
                "width_placeholder": "{width}",
                "height_placeholder": "{height}",
            },
        },
    }
    return TileSourceResolution(
        document=document,
        probe=SourceProbe(
            available=True,
            protocol="wms_tiles",
            requested_name=source.remote_name,
            canonical_name=source.remote_name,
            service_version="1.3.0",
            fingerprint_sha256="a" * 64,
            fingerprint_quality="weak",
            metadata={
                "styles": remote_names,
                "crs": ["EPSG:3857"],
                "formats": ["image/png"],
            },
        ),
    )


def _styles(db, source):
    return tuple(
        db.scalars(
            select(ReferenceLayerStyle)
            .where(
                ReferenceLayerStyle.provider_key == source.provider_key,
                ReferenceLayerStyle.layer_id == source.layer_id,
            )
            .order_by(ReferenceLayerStyle.sort_order, ReferenceLayerStyle.id)
        )
    )


def _fixed_preflight(
    _store,
    *,
    source_document,
    allowed_origins,
    max_archive_bytes,
    sample_limit,
    concurrency,
):
    assert tuple(allowed_origins) == ("https://example.test",)
    assert max_archive_bytes == 1_000_000
    assert sample_limit == 8
    assert concurrency == 2
    style = source_document["descriptor"]["style"]
    projected = 400 if style.endswith("default") else 350
    return TileSeedPreflight(
        tile_count=10,
        sample_count=2,
        sample_bytes=20,
        largest_tile_bytes=10,
        projected_payload_bytes=projected - 100,
        projected_archive_bytes=projected,
        projection_schema=PREFLIGHT_PROJECTION_SCHEMA,
    )


def _xyz_document(origin: str = "https://tiles.example.test"):
    return {
        "schema": "reference-tile-source/v1",
        "protocol": "xyz",
        "definition_sha256": "a" * 64,
        "descriptor": {
            "bounds": {
                "west": -180.0,
                "south": -85.0,
                "east": 180.0,
                "north": 85.0,
            },
            "min_zoom": 0,
            "max_zoom": 0,
            "format": "image/png",
            "coverage_required": True,
            "estimated_tile_count": 1,
            "max_tile_count": 1,
            "layer": "base",
            "url_template": f"{origin}/{{z}}/{{x}}/{{y}}.png",
            "scheme": "xyz",
        },
    }


def _counts(db):
    return {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (
            ReferenceSyncRun,
            ReferenceSourceArtifact,
            ReferenceDeliveryVersion,
            ReferenceMirrorAuthorizationReview,
        )
    }


def test_aggregate_counts_every_style_once_and_is_read_only_and_deterministic(
    db,
):
    layer, snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-style-test",
    )
    db.add(
        ReferenceLayerStyle(
            last_seen_snapshot_id=snapshot.id,
            layer_id=layer.id,
            provider_key=source.provider_key,
            source_key="style:alternate",
            remote_name="planning:alternate",
            title="Alternate",
            sort_order=1,
            is_default=False,
            status="active",
        )
    )
    db.commit()
    authorize_mirror_source(db, source)
    styles = _styles(db, source)
    before_counts = _counts(db)
    before_schedule = source.next_check_at
    flushes = []

    def before_flush(*_args):
        flushes.append(True)

    event.listen(db, "before_flush", before_flush)
    store = CapacityStore(available_bytes=700)
    try:
        reports = [
            aggregate_tile_capacity_preflight(
                db,
                store,
                provider_key=source.provider_key,
                sample_limit=8,
                concurrency=2,
                max_archive_bytes=1_000_000,
                max_tile_count=100,
                resolver=lambda *_args: _resolution(source, styles),
                variant_preflight=_fixed_preflight,
            )
            for _index in range(2)
        ]
    finally:
        event.remove(db, "before_flush", before_flush)

    assert json.dumps(reports[0], sort_keys=True) == json.dumps(
        reports[1],
        sort_keys=True,
    )
    summary = reports[0]["summary"]
    assert summary["strategy_count"] == 1
    assert summary["archive_count"] == 2
    assert summary["projected_tile_count"] == 20
    assert summary["projected_archive_bytes"] == 750
    assert summary["storage_available_bytes"] == 700
    assert summary["capacity_margin_bytes"] == -50
    assert summary["capacity_exceeded"] is True
    assert summary["errors_by_code"] == {
        "aggregate_capacity_exceeded": 1
    }
    assert reports[0]["ok"] is False
    assert reports[0]["report_generated"] is True
    assert reports[0]["state_fence"]["strategy_count"] == 1
    assert reports[0]["state_fence"]["style_count"] == 2
    assert tile_capacity_preflight_exit_code(reports[0]) == 1
    assert store.inspect_calls == 2
    assert store.mutation_calls == 0
    assert flushes == []
    assert _counts(db) == before_counts
    assert db.get(ReferenceLayerSource, source.id).next_check_at == before_schedule
    assert not db.new and not db.dirty and not db.deleted


def test_gate_exits_zero_only_when_every_selected_strategy_is_ready(db):
    _layer, _snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-ready-gate-test",
    )
    authorize_mirror_source(db, source)
    styles = _styles(db, source)
    events = []

    class OrderedCapacityStore(CapacityStore):
        def inspect_capacity(self):
            events.append("capacity")
            return super().inspect_capacity()

    def resolver(*_args):
        events.append("capabilities")
        return _resolution(source, styles)

    def variant(*args, **kwargs):
        events.append("sample")
        return _fixed_preflight(*args, **kwargs)

    report = aggregate_tile_capacity_preflight(
        db,
        OrderedCapacityStore(available_bytes=900),
        provider_key=source.provider_key,
        sample_limit=8,
        concurrency=2,
        max_archive_bytes=1_000_000,
        max_tile_count=100,
        resolver=resolver,
        variant_preflight=variant,
    )

    assert events == ["capabilities", "sample", "capacity"]
    assert report["ok"] is True
    assert report["report_generated"] is True
    assert report["summary"]["projection_complete"] is True
    assert report["summary"]["ready_for_bulk_seed"] is True
    assert report["summary"]["capacity_exceeded"] is False
    assert tile_capacity_preflight_exit_code(report) == 0
    assert tile_capacity_preflight_exit_code(
        {"ok": False, "summary": {"ready_for_bulk_seed": True}}
    ) == 1
    assert tile_capacity_preflight_exit_code(
        {"ok": True, "summary": {"ready_for_bulk_seed": False}}
    ) == 1


def test_unauthorized_and_restricted_sources_never_reach_network(db):
    calls = []

    def network_forbidden(*_args):
        calls.append(True)
        raise AssertionError("authorization must precede every network call")

    for provider_key, decision, expected_status in (
        ("tile-capacity-unauthorized-test", None, "unauthorized"),
        ("tile-capacity-restricted-test", "restricted", "blocked"),
    ):
        _layer, _snapshot, source, _strategy = _seed_tile_strategy(
            db,
            provider_key,
        )
        if decision is not None:
            authorize_mirror_source(db, source, decision=decision)
        store = CapacityStore()
        report = aggregate_tile_capacity_preflight(
            db,
            store,
            provider_key=provider_key,
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
            resolver=network_forbidden,
            variant_preflight=network_forbidden,
        )
        assert report["sources"][0]["status"] == expected_status
        assert report["summary"]["ready_for_bulk_seed"] is False
        assert report["ok"] is False
        assert tile_capacity_preflight_exit_code(report) == 1
        assert store.inspect_calls == 1
        assert store.mutation_calls == 0
    assert calls == []


@pytest.mark.parametrize(
    ("provider_key", "mutation", "error_code"),
    [
        (
            "tile-capacity-disabled-test",
            lambda source, _strategy: setattr(source, "enabled", False),
            "tile_source_disabled",
        ),
        (
            "tile-capacity-incompatible-test",
            lambda source, _strategy: setattr(
                source,
                "sync_strategy",
                "full_snapshot",
            ),
            "tile_source_incompatible",
        ),
    ],
)
def test_local_blockers_never_reach_authorization_or_network(
    db,
    provider_key,
    mutation,
    error_code,
):
    _layer, _snapshot, source, strategy = _seed_tile_strategy(
        db,
        provider_key,
    )
    mutation(source, strategy)
    db.commit()

    def network_forbidden(*_args):
        raise AssertionError("local blockers must precede every network call")

    report = aggregate_tile_capacity_preflight(
        db,
        CapacityStore(),
        provider_key=provider_key,
        sample_limit=8,
        concurrency=2,
        max_archive_bytes=1_000_000,
        max_tile_count=100,
        resolver=network_forbidden,
        variant_preflight=network_forbidden,
    )

    assert report["sources"][0]["status"] == "blocked"
    assert report["sources"][0]["errors"][0]["code"] == error_code
    assert report["summary"]["projection_complete"] is False
    assert report["summary"]["ready_for_bulk_seed"] is False
    assert report["ok"] is False
    assert tile_capacity_preflight_exit_code(report) == 1


def test_stale_strategy_evidence_invalidates_the_complete_matrix(db) -> None:
    _layer, _snapshot, source, strategy = _seed_tile_strategy(
        db,
        "tile-capacity-stale-test",
    )
    strategy.catalog_definition_sha256 = "b" * 64
    db.commit()

    with pytest.raises(
        TileCapacityPreflightInputError,
        match="strategy generation is incomplete",
    ):
        aggregate_tile_capacity_preflight(
            db,
            CapacityStore(),
            provider_key=source.provider_key,
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
            resolver=lambda *_args: pytest.fail("network must not be reached"),
            variant_preflight=lambda *_args, **_kwargs: pytest.fail(
                "network must not be reached"
            ),
        )


def test_exact_filters_select_one_strategy_and_mismatches_fail_closed(db):
    layer, _snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-filter-test",
    )
    authorize_mirror_source(db, source)
    styles = _styles(db, source)
    report = aggregate_tile_capacity_preflight(
        db,
        CapacityStore(),
        provider_key=source.provider_key,
        layer_id=layer.id,
        source_id=source.id,
        source_key=source.source_key,
        sample_limit=8,
        concurrency=2,
        max_archive_bytes=1_000_000,
        max_tile_count=100,
        resolver=lambda *_args: _resolution(source, styles),
        variant_preflight=_fixed_preflight,
    )

    assert report["filters"] == {
        "layer_id": layer.id,
        "source_id": source.id,
        "source_key": source.source_key,
    }
    assert [row["source_id"] for row in report["sources"]] == [source.id]
    assert report["ok"] is True
    with pytest.raises(
        TileCapacityPreflightInputError,
        match="no current tile strategy",
    ):
        aggregate_tile_capacity_preflight(
            db,
            CapacityStore(),
            provider_key=source.provider_key,
            source_key=f"{source.source_key}:different",
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
        )


def test_preflight_ignores_historical_tile_strategy_generations(db) -> None:
    layer, snapshot, source, strategy = _seed_tile_strategy(
        db,
        "tile-capacity-latest-generation-test",
    )
    evidence = {"reason": "latest generation blocks bulk tiles"}
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    db.add(
        ReferenceLayerMirrorStrategy(
            provider_key=source.provider_key,
            layer_id=layer.id,
            catalog_snapshot_id=snapshot.id,
            catalog_definition_sha256=snapshot.definition_sha256,
            strategy="blocked",
            source_id=None,
            strategy_reason_code="latest_generation_blocked",
            strategy_reason="Latest reviewed generation blocks tile seeding.",
            evidence_json=evidence,
            evidence_sha256=evidence_sha256,
            generation=strategy.generation + 1,
            validated_at=strategy.validated_at,
        )
    )
    db.commit()

    with pytest.raises(
        TileCapacityPreflightInputError,
        match="no current tile strategy",
    ):
        aggregate_tile_capacity_preflight(
            db,
            CapacityStore(),
            provider_key=source.provider_key,
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
        )


def test_preflight_rejects_an_incomplete_latest_strategy_generation(db) -> None:
    layer, snapshot, source, strategy = _seed_tile_strategy(
        db,
        "tile-capacity-incomplete-generation-test",
    )
    db.add(
        ReferenceLayerMirrorStrategy(
            provider_key=source.provider_key,
            layer_id=layer.id,
            catalog_snapshot_id=snapshot.id,
            catalog_definition_sha256=snapshot.definition_sha256,
            strategy="tiles",
            source_id=source.id,
            strategy_reason_code="invalid_latest_generation",
            strategy_reason="Latest generation has invalid evidence.",
            evidence_json={"generation": strategy.generation + 1},
            evidence_sha256="0" * 64,
            generation=strategy.generation + 1,
            validated_at=strategy.validated_at,
        )
    )
    db.commit()

    with pytest.raises(
        TileCapacityPreflightInputError,
        match="strategy generation is incomplete",
    ):
        aggregate_tile_capacity_preflight(
            db,
            CapacityStore(),
            provider_key=source.provider_key,
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
        )


def test_authorized_descriptor_failure_is_an_error_and_exit_one(db):
    _layer, _snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-error-test",
    )
    authorize_mirror_source(db, source)

    def invalid_capabilities(*_args):
        raise AcquisitionValidationError(
            "capabilities are malformed",
            code="invalid_capabilities",
        )

    report = aggregate_tile_capacity_preflight(
        db,
        CapacityStore(),
        provider_key=source.provider_key,
        sample_limit=8,
        concurrency=2,
        max_archive_bytes=1_000_000,
        max_tile_count=100,
        resolver=invalid_capabilities,
    )

    assert report["sources"][0]["status"] == "error"
    assert report["summary"]["authorized_error_count"] == 1
    assert report["summary"]["errors_by_code"] == {
        "invalid_capabilities": 1
    }
    assert report["ok"] is False
    assert tile_capacity_preflight_exit_code(report) == 1


def test_capabilities_resolver_uses_only_the_authorized_canonical_origin(db):
    _layer, _snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-origin-test",
    )
    authorization = authorize_mirror_source(
        db,
        source,
        allowed_origins=[
            "https://example.test",
            "https://unused.example.test",
        ],
    )
    style = _styles(db, source)[0]
    configured_style = source.config_json["style_name"]
    body = f"""<WMS_Capabilities version="1.3.0"><Capability>
      <Request><GetMap><Format>image/png</Format></GetMap></Request>
      <Layer><CRS>EPSG:3857</CRS><Layer>
      <Name>{source.remote_name}</Name>
      <Style><Name>{configured_style}</Name></Style>
      <Style><Name>{style.remote_name}</Name></Style>
      </Layer></Layer></Capability></WMS_Capabilities>""".encode()
    observed = {}

    class Downloader:
        def __init__(self, policy):
            observed["policy"] = policy

        def download(self, url, sink, **_kwargs):
            sink.write(body)
            digest = hashlib.sha256(body).hexdigest()
            return HTTPSDownloadResult(
                source_url=url,
                final_url=url,
                status_code=200,
                not_modified=False,
                content_type="application/xml",
                size_bytes=len(body),
                sha256=digest,
                etag=None,
                last_modified=None,
                redirects=0,
                redirect_chain=(url,),
            )

    resolved = resolve_tile_source(
        source,
        authorization,
        downloader_factory=Downloader,
    )

    assert observed["policy"].allowed_origins == (
        authorization.canonical_origin,
    )
    assert resolved.document["definition_sha256"] == source.definition_sha256
    assert resolved.probe is not None and resolved.probe.available is True


def test_final_fence_rejects_a_source_change_during_sampling(db):
    _layer, _snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-final-fence-test",
    )
    authorize_mirror_source(db, source)
    styles = _styles(db, source)
    store = CapacityStore()

    def mutate_source_during_sample(*args, **kwargs):
        result = _fixed_preflight(*args, **kwargs)
        db.execute(
            update(ReferenceLayerSource)
            .where(ReferenceLayerSource.id == source.id)
            .values(enabled=False)
        )
        db.commit()
        return result

    with pytest.raises(
        TileCapacityPreflightInputError,
        match="changed during preflight",
    ):
        aggregate_tile_capacity_preflight(
            db,
            store,
            provider_key=source.provider_key,
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
            resolver=lambda *_args: _resolution(source, styles),
            variant_preflight=mutate_source_during_sample,
        )

    assert store.inspect_calls == 0
    assert store.mutation_calls == 0


def test_final_fence_rejects_a_source_change_during_capacity_scan(db):
    _layer, _snapshot, source, _strategy = _seed_tile_strategy(
        db,
        "tile-capacity-scan-fence-test",
    )
    authorize_mirror_source(db, source)
    styles = _styles(db, source)

    class MutatingCapacityStore(CapacityStore):
        def inspect_capacity(self):
            db.execute(
                update(ReferenceLayerSource)
                .where(ReferenceLayerSource.id == source.id)
                .values(enabled=False)
            )
            db.commit()
            return super().inspect_capacity()

    store = MutatingCapacityStore()
    with pytest.raises(
        TileCapacityPreflightInputError,
        match="changed during preflight",
    ):
        aggregate_tile_capacity_preflight(
            db,
            store,
            provider_key=source.provider_key,
            sample_limit=8,
            concurrency=2,
            max_archive_bytes=1_000_000,
            max_tile_count=100,
            resolver=lambda *_args: _resolution(source, styles),
            variant_preflight=_fixed_preflight,
        )

    assert store.inspect_calls == 1
    assert store.mutation_calls == 0


def test_wmts_variants_cover_each_style_and_reject_duplicate_names():
    styles = (
        ReferenceLayerStyle(
            last_seen_snapshot_id=1,
            layer_id=2,
            provider_key="siur",
            source_key="style:default",
            remote_name="default",
            title="Default",
            sort_order=0,
            is_default=True,
            status="active",
        ),
        ReferenceLayerStyle(
            last_seen_snapshot_id=1,
            layer_id=2,
            provider_key="siur",
            source_key="style:alternate",
            remote_name="alternate",
            title="Alternate",
            sort_order=1,
            is_default=False,
            status="active",
        ),
    )
    resolution = TileSourceResolution(
        document={
            "schema": "reference-tile-source/v1",
            "protocol": "wmts",
            "definition_sha256": "a" * 64,
            "descriptor": {
                "style": "default",
                "kvp": {"style": "default"},
            },
        },
        probe=SourceProbe(
            available=True,
            protocol="wmts",
            requested_name="layer",
            canonical_name="layer",
            service_version="1.0.0",
            fingerprint_sha256="b" * 64,
            fingerprint_quality="strong",
            metadata={
                "styles": [
                    {"name": "default", "default": True},
                    {"name": "alternate", "default": False},
                ]
            },
        ),
    )

    implicit = _tile_document_variants(resolution, ())
    variants = _tile_document_variants(resolution, styles)

    assert len(implicit) == 1
    assert implicit[0].style_source_key is None
    assert [item.style_source_key for item in variants] == [
        "style:default",
        "style:alternate",
    ]
    assert [
        item.document["descriptor"]["kvp"]["style"] for item in variants
    ] == ["default", "alternate"]

    styles[1].remote_name = "default"
    with pytest.raises(
        TileCapacityPreflightError,
        match="not all advertised",
    ):
        _tile_document_variants(resolution, styles)


def test_authorized_tile_fetcher_rejects_a_different_origin_before_network():
    with pytest.raises(TileSeedError, match="outside the authorized origins"):
        authorized_tile_fetcher(
            source_document=_xyz_document(),
            allowed_origins=("https://other.example.test",),
        )
    with pytest.raises(TileSeedError, match="not canonical"):
        authorized_tile_fetcher(
            source_document=_xyz_document(),
            allowed_origins=("https://tiles.example.test/path",),
        )


def test_variant_preflight_never_reserves_or_stages_the_shared_cas(monkeypatch):
    source_document = _xyz_document()
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    output = BytesIO()
    image.save(output, format="PNG")
    body = output.getvalue()
    monkeypatch.setattr(
        "app.reference_layers.tile_capacity_preflight.authorized_tile_fetcher",
        lambda **_kwargs: lambda _url, _media_type: body,
    )
    store = CapacityStore()

    result = preflight_tile_variant(
        store,
        source_document=source_document,
        allowed_origins=("https://tiles.example.test",),
        max_archive_bytes=1_000_000,
        sample_limit=1,
        concurrency=1,
    )

    assert result.tile_count == 1
    assert store.mutation_calls == 0


def test_capacity_snapshot_can_inspect_an_existing_cas_without_writes(
    tmp_path,
):
    root = tmp_path / "reference-cas"
    with ReferenceBlobStore(
        root,
        max_blob_bytes=1_000,
        quota_bytes=1_000,
        min_free_bytes=0,
    ):
        pass
    payload = root / "existing"
    payload.write_bytes(b"existing-bytes")
    before = sorted(path.relative_to(root) for path in root.rglob("*"))

    with ReferenceBlobStore(
        root,
        max_blob_bytes=1_000,
        quota_bytes=1_000,
        min_free_bytes=0,
        read_only=True,
    ) as store:
        capacity = store.inspect_capacity()

    assert capacity.used_bytes == len(b"existing-bytes")
    assert capacity.quota_available_bytes == (
        1_000 - len(b"existing-bytes")
    )
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before


def test_read_only_capacity_requires_the_existing_safe_lock_without_creating_it(
    tmp_path,
):
    missing_root = tmp_path / "missing-lock"
    missing_root.mkdir()
    with pytest.raises(
        ReferenceBlobStoreError,
        match="lock is unavailable",
    ):
        ReferenceBlobStore(
            missing_root,
            max_blob_bytes=1_000,
            quota_bytes=1_000,
            read_only=True,
        ).inspect_capacity()
    assert list(missing_root.iterdir()) == []

    unsafe_root = tmp_path / "unsafe-lock"
    unsafe_root.mkdir()
    external = tmp_path / "external-lock"
    external.write_bytes(b"")
    (unsafe_root / ".reference-blob-store.lock").symlink_to(external)
    with pytest.raises(ReferenceBlobStoreError):
        ReferenceBlobStore(
            unsafe_root,
            max_blob_bytes=1_000,
            quota_bytes=1_000,
            read_only=True,
        ).inspect_capacity()
    assert (unsafe_root / ".reference-blob-store.lock").is_symlink()


def test_read_only_capacity_rejects_lock_replacement_during_scan(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "replaced-lock"
    writer = ReferenceBlobStore(
        root,
        max_blob_bytes=1_000,
        quota_bytes=1_000,
    )
    reader = ReferenceBlobStore(
        root,
        max_blob_bytes=1_000,
        quota_bytes=1_000,
        read_only=True,
    )
    original_snapshot = reader._capacity_snapshot

    def replace_lock():
        capacity = original_snapshot()
        lock_path = root / ".reference-blob-store.lock"
        lock_path.unlink()
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)
        return capacity

    monkeypatch.setattr(reader, "_capacity_snapshot", replace_lock)
    try:
        with pytest.raises(ReferenceBlobStoreError, match="lock is unsafe"):
            reader.inspect_capacity()
    finally:
        reader.close()
        writer.close()


def test_read_only_capacity_snapshot_waits_for_the_writer_lock(tmp_path):
    root = tmp_path / "locked-capacity"
    writer = ReferenceBlobStore(
        root,
        max_blob_bytes=1_000,
        quota_bytes=1_000,
    )
    reader = ReferenceBlobStore(
        root,
        max_blob_bytes=1_000,
        quota_bytes=1_000,
        read_only=True,
    )
    started = threading.Event()
    completed = threading.Event()
    failures = []

    def inspect():
        started.set()
        try:
            reader.inspect_capacity()
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)
        finally:
            completed.set()

    worker = threading.Thread(target=inspect)
    try:
        with writer._exclusive_lock():
            worker.start()
            assert started.wait(1)
            assert not completed.wait(0.05)
        assert completed.wait(2)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert failures == []
    finally:
        reader.close()
        writer.close()


def test_cli_exposes_only_explicit_dry_run_for_tile_preflight():
    arguments = _parser().parse_args(
        [
            "tile-preflight",
            "--provider-key",
            "siur",
            "--layer-id",
            "12",
            "--source-key",
            "source:test",
            "--dry-run",
        ]
    )

    assert arguments.command == "tile-preflight"
    assert arguments.dry_run is True
    with pytest.raises(SystemExit):
        _parser().parse_args(["tile-preflight", "--apply"])
