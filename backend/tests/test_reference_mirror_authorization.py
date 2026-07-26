from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import app.reference_layers.mirror_authorization as mirror_authorization
import app.reference_layers.mirror_orchestrator as mirror_orchestrator
from app.core.config import Settings
from app.reference_layers.acquisition import AcquisitionResult
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.local_delivery import (
    LocalDeliveryError,
    resolve_local_delivery,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationDocumentError,
    apply_mirror_authorization_review,
    authorization_chain_is_valid,
    effective_service_attributions,
    parse_mirror_authorization,
    plan_mirror_authorization_review,
    stored_mirror_authorization_review_is_valid,
)
from app.reference_layers.mirror_lifecycle import (
    canonical_promotion_event_sha256,
    claim_next_sync_run,
    enqueue_due_sources,
)
from app.reference_layers.mirror_orchestrator import MirrorRunProcessor
from app.reference_layers.models import (
    ReferenceDeliveryAsset,
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceService,
    ReferenceSyncRun,
)
from support_reference_mirror_authorization import (
    authorize_mirror_source,
    mirror_authorization_document,
    supersede_mirror_authorization,
)


NOW = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)


def _seed_source(
    db,
    *,
    provider_key: str = "mirror-authorization-test",
    target_kind: str = "vector",
):
    definition = ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://catalog.example.test/settings.json",
        raw_catalog={"revision": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service",
                title="Official service",
                upstream_protocol="wms",
                base_url="https://maps.example.test/wms",
                attribution=None,
                license_status="pending",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer",
                node_type="layer",
                title="Official layer",
                service_key="service",
                remote_name="official:layer",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=NOW,
    )
    snapshot, _ = apply_catalog_definition(db, definition)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.source_key == "layer",
        )
    )
    source = ReferenceLayerSource(
        provider_key=provider_key,
        layer_id=layer.id,
        source_key="reviewed-source",
        protocol="wfs" if target_kind == "vector" else "wms_tiles",
        target_kind=target_kind,
        endpoint_url="https://data.example.test/features",
        remote_name="official:layer",
        sync_strategy=(
            "paged_snapshot" if target_kind == "vector" else "tile_seed"
        ),
        config_json={
            "metadata_url": "https://metadata.example.test/record/1",
        },
        definition_sha256="0" * 64,
        enabled=True,
        is_primary=True,
        priority=0,
        next_check_at=NOW - timedelta(seconds=1),
    )
    source.definition_sha256 = canonical_json_sha256(
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
    db.add(source)
    db.commit()
    service = db.get(ReferenceService, layer.service_id)
    return snapshot, service, layer, source


def _approved_document(db, source, **kwargs) -> bytes:
    return mirror_authorization_document(
        db,
        source,
        allowed_origins=[
            "https://data.example.test",
            "https://metadata.example.test",
        ],
        **kwargs,
    )


def test_parser_rejects_duplicate_and_extra_keys(db) -> None:
    _, _, _, source = _seed_source(db)
    valid = _approved_document(db, source)
    duplicate = valid.replace(
        b'"reviewer": "Automated test reviewer",',
        b'"reviewer": "First", "reviewer": "Second",',
    )
    with pytest.raises(
        MirrorAuthorizationDocumentError,
        match="duplicate key",
    ):
        parse_mirror_authorization(duplicate)

    value = json.loads(valid)
    value["legacy_allow_cache"] = True
    with pytest.raises(
        MirrorAuthorizationDocumentError,
        match="strict schema",
    ):
        parse_mirror_authorization(json.dumps(value).encode())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["permissions"].update(
                {
                    "dataset_download": False,
                    "local_storage": True,
                }
            ),
            "local_storage requires dataset_download",
        ),
        (
            lambda value: value["permissions"].update(
                {"local_storage": False, "local_service": True}
            ),
            "local_service requires local_storage",
        ),
        (
            lambda value: value.update({"decision": "restricted"}),
            "only approved reviews",
        ),
        (
            lambda value: value["permissions"].update(
                {"bulk_tile_seed": False}
            ),
            "tile local_service requires bulk_tile_seed",
        ),
    ],
)
def test_parser_rejects_invalid_permission_shapes(
    db,
    mutation,
    message,
) -> None:
    _, _, _, source = _seed_source(
        db,
        provider_key=f"auth-permission-{message[:8].replace(' ', '-')}",
        target_kind="tiles",
    )
    value = json.loads(_approved_document(db, source))
    mutation(value)
    with pytest.raises(MirrorAuthorizationDocumentError, match=message):
        parse_mirror_authorization(json.dumps(value).encode())


def test_plan_apply_requires_exact_hash_and_linear_chain(db) -> None:
    _, _, _, source = _seed_source(db)
    document = _approved_document(db, source)
    plan = plan_mirror_authorization_review(db, document)
    assert plan.already_applied_id is None

    with pytest.raises(
        MirrorAuthorizationDocumentError,
        match="hash changed",
    ):
        apply_mirror_authorization_review(
            db,
            document,
            expected_review_sha256="f" * 64,
        )

    first = apply_mirror_authorization_review(
        db,
        document,
        expected_review_sha256=plan.evidence.review_sha256,
    )
    second_document = _approved_document(
        db,
        source,
        reviewed_at=NOW + timedelta(seconds=1),
        supersedes_review_sha256=first.review_sha256,
    )
    second_plan = plan_mirror_authorization_review(db, second_document)
    second = apply_mirror_authorization_review(
        db,
        second_document,
        expected_review_sha256=second_plan.evidence.review_sha256,
    )
    chain = tuple(
        db.scalars(
            select(ReferenceMirrorAuthorizationReview)
            .where(
                ReferenceMirrorAuthorizationReview.source_id == source.id
            )
            .order_by(
                ReferenceMirrorAuthorizationReview.reviewed_at,
                ReferenceMirrorAuthorizationReview.id,
            )
        )
    )
    assert [item.id for item in chain] == [first.id, second.id]
    assert authorization_chain_is_valid(chain)

    fork = _approved_document(
        db,
        source,
        reviewed_at=NOW + timedelta(seconds=2),
        supersedes_review_sha256=first.review_sha256,
    )
    with pytest.raises(
        MirrorAuthorizationDocumentError,
        match="current chain head",
    ):
        plan_mirror_authorization_review(db, fork)


def test_cli_is_local_dry_run_by_default_and_applies_exact_hash(
    db,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _, _, _, source = _seed_source(db)
    document = _approved_document(db, source)
    document_path = tmp_path / "authorization.json"
    document_path.write_bytes(document)
    expected = parse_mirror_authorization(document).review_sha256
    monkeypatch.setattr(
        mirror_authorization,
        "register_all_models",
        lambda: None,
    )
    monkeypatch.setattr(
        mirror_authorization,
        "SessionLocal",
        lambda: nullcontext(db),
    )

    assert mirror_authorization.main(
        ["--file", str(document_path)]
    ) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["applied"] is False
    assert dry_run["review_sha256"] == expected
    assert db.scalar(
        select(ReferenceMirrorAuthorizationReview.id)
    ) is None

    assert mirror_authorization.main(
        [
            "--file",
            str(document_path),
            "--apply",
            "--expected-review-sha256",
            expected,
        ]
    ) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["applied"] is True
    assert applied["review_sha256"] == expected
    assert db.scalar(
        select(ReferenceMirrorAuthorizationReview.id)
    ) == applied["review_id"]

    assert mirror_authorization.main(
        ["--file", "https://example.test/authorization.json"]
    ) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error_code"] == (
        "mirror_authorization_document_rejected"
    )


def test_source_hash_and_unapproved_origin_changes_fail_closed(db) -> None:
    _, _, _, source = _seed_source(db)
    value = json.loads(_approved_document(db, source))
    value["source_definition_sha256"] = "a" * 64
    with pytest.raises(
        MirrorAuthorizationDocumentError,
        match="exact source definition",
    ):
        plan_mirror_authorization_review(db, json.dumps(value).encode())

    value = json.loads(_approved_document(db, source))
    value["allowed_origins"] = ["https://data.example.test"]
    with pytest.raises(
        MirrorAuthorizationDocumentError,
        match="outside the approved origins",
    ):
        plan_mirror_authorization_review(db, json.dumps(value).encode())


def test_review_rows_are_immutable_and_integrity_is_recomputed(db) -> None:
    _, _, _, source = _seed_source(db)
    review = authorize_mirror_source(
        db,
        source,
        allowed_origins=[
            "https://data.example.test",
            "https://metadata.example.test",
        ],
    )
    assert stored_mirror_authorization_review_is_valid(review)

    with pytest.raises(DBAPIError):
        db.execute(
            text(
                "UPDATE reference_mirror_authorization_reviews "
                "SET reviewer = 'tampered' WHERE id = :id"
            ),
            {"id": review.id},
        )
        db.commit()
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(
            text("TRUNCATE reference_mirror_authorization_reviews")
        )
        db.commit()
    db.rollback()

    review.review_sha256 = "0" * 64
    assert not stored_mirror_authorization_review_is_valid(review)
    db.expire(review)


def test_missing_authorization_rejects_before_acquisition(
    db,
    tmp_path,
) -> None:
    _, _, _, source = _seed_source(db)
    [run_id] = enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        lease_seconds=3600,
        token_factory=lambda: "1" * 64,
    )
    assert lease.run_id == run_id
    calls = {"acquire": 0}

    class Acquisition:
        def acquire(self, *args, **kwargs):
            calls["acquire"] += 1
            raise AssertionError("network acquisition must not be called")

    store = ReferenceBlobStore(Path(tmp_path, "missing-auth-store"))
    processor = MirrorRunProcessor(
        session_factory=lambda: nullcontext(db),
        store=store,
        acquisition=Acquisition(),
        config=Settings(
            reference_storage_root=str(store.root),
            reference_mirror_lease_seconds=3600,
            reference_mirror_heartbeat_seconds=100,
        ),
    )
    try:
        result = processor.process_lease(lease)
    finally:
        processor.close()

    assert calls["acquire"] == 0
    assert result.state == "rejected"
    assert result.error_code == "mirror_authorization_missing"
    assert db.get(ReferenceSyncRun, run_id).error_code == (
        "mirror_authorization_missing"
    )
    assert source.id == lease.source_id


def test_revocation_during_download_blocks_materialization_and_publication(
    db,
    tmp_path,
    monkeypatch,
) -> None:
    _, _, _, source = _seed_source(db)
    first = authorize_mirror_source(
        db,
        source,
        reviewed_at=NOW,
        allowed_origins=[
            "https://data.example.test",
            "https://metadata.example.test",
        ],
    )
    enqueue_due_sources(db, now=NOW)
    lease = claim_next_sync_run(
        db,
        lease_seconds=3600,
        token_factory=lambda: "2" * 64,
    )
    calls = {"materialize": 0, "publish": 0}

    class Supervisor:
        def __init__(self, factory, current_lease, **kwargs):
            del factory, kwargs
            self.lease = current_lease

        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

        def pulse(self, **kwargs):
            del kwargs
            return self.lease

    monkeypatch.setattr(
        mirror_orchestrator,
        "LeaseSupervisor",
        Supervisor,
    )

    class Acquisition:
        def acquire(self, *args, **kwargs):
            del args, kwargs
            supersede_mirror_authorization(
                db,
                source,
                first,
                decision="restricted",
            )
            return AcquisitionResult(
                source_key=source.source_key,
                source_definition_sha256=source.definition_sha256,
                protocol=source.protocol,
                target_kind=source.target_kind,
                not_modified=False,
                artifacts=(),
                manifest_sha256="a" * 64,
                probe=None,
                observed_etag=None,
                observed_last_modified=None,
                observed_version=None,
                feature_count=0,
                total_bytes=0,
                stats={},
            )

    def materializer(*args):
        del args
        calls["materialize"] += 1
        raise AssertionError("materialization must be blocked")

    def publisher(*args):
        del args
        calls["publish"] += 1
        raise AssertionError("publication must be blocked")

    store = ReferenceBlobStore(Path(tmp_path, "revoked-auth-store"))
    processor = MirrorRunProcessor(
        session_factory=lambda: nullcontext(db),
        store=store,
        acquisition=Acquisition(),
        config=Settings(
            reference_storage_root=str(store.root),
            reference_mirror_lease_seconds=3600,
            reference_mirror_heartbeat_seconds=100,
        ),
        materializer=materializer,
        publisher=publisher,
    )
    try:
        result = processor.process_lease(lease)
    finally:
        processor.close()

    assert result.state == "rejected"
    assert result.error_code == "mirror_authorization_restricted"
    assert calls == {"materialize": 0, "publish": 0}


def test_serving_and_effective_attribution_follow_current_review(db) -> None:
    snapshot, service, layer, source = _seed_source(db)
    first = authorize_mirror_source(
        db,
        source,
        reviewed_at=NOW,
        attribution="Fuente oficial revisada",
        allowed_origins=[
            "https://data.example.test",
            "https://metadata.example.test",
        ],
    )
    run = ReferenceSyncRun(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        source_definition_json={
            "protocol": source.protocol,
            "target_kind": source.target_kind,
            "endpoint_url": source.endpoint_url,
            "remote_name": source.remote_name,
            "sync_strategy": source.sync_strategy,
            "priority": source.priority,
            "config": source.config_json,
        },
        source_definition_sha256=source.definition_sha256,
        mirror_authorization_review_id=first.id,
        mirror_authorization_review_sha256=first.review_sha256,
        trigger_kind="manual",
        check_mode="full",
        status="succeeded",
        queued_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )
    db.add(run)
    db.flush()
    validation = {"passed": True}
    version = ReferenceDeliveryVersion(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        sync_run_id=run.id,
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        mirror_authorization_review_id=first.id,
        mirror_authorization_review_sha256=first.review_sha256,
        sequence_number=1,
        delivery_kind="vector",
        content_sha256="e" * 64,
        manifest_sha256="c" * 64,
        validation_sha256=canonical_json_sha256(validation),
        crs="EPSG:3857",
        bounds_json={"west": -7, "south": 40, "east": -1, "north": 44},
        feature_count=1,
        validation_json=validation,
    )
    db.add(version)
    db.flush()
    db.add(
        ReferenceDeliveryAsset(
            version_id=version.id,
            asset_key="primary",
            asset_kind="vector_table",
            is_primary=True,
            storage_backend="postgres",
            storage_key="reference_data.authorized_layer",
            media_type="application/x-postgis-table",
            sha256=version.content_sha256,
            metadata_json={
                "renderer": "geoserver",
                "layer_name": "authorized_layer",
                "styles": {},
                "identify_available": False,
                "legend_available": False,
            },
        )
    )
    reason = "authorized initial delivery"
    promotion = ReferenceDeliveryPromotion(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        sequence_number=1,
        action="promote",
        to_version_id=version.id,
        run_id=run.id,
        reason=reason,
        event_sha256=canonical_promotion_event_sha256(
            provider_key=source.provider_key,
            layer_id=source.layer_id,
            sequence_number=1,
            action="promote",
            from_version_id=None,
            to_version_id=version.id,
            run_id=run.id,
            actor_id=None,
            reason=reason,
            previous_event_id=None,
            previous_event_sha256=None,
            created_at=NOW,
        ),
        created_at=NOW,
    )
    db.add(promotion)
    db.flush()
    db.add(
        ReferenceLayerDeliveryState(
            provider_key=source.provider_key,
            layer_id=source.layer_id,
            status="active",
            active_version_id=version.id,
            generation=1,
            last_promotion_id=promotion.id,
        )
    )
    db.commit()

    assert effective_service_attributions(
        db,
        services=[service],
    )[service.id] == "Fuente oficial revisada"
    assert resolve_local_delivery(
        db,
        layer=layer,
        style=None,
        operation="tile",
    ) is not None

    supersede_mirror_authorization(
        db,
        source,
        first,
        decision="restricted",
    )
    with pytest.raises(LocalDeliveryError) as error:
        resolve_local_delivery(
            db,
            layer=layer,
            style=None,
            operation="tile",
        )
    assert error.value.blocker == "mirror_authorization_restricted"
    assert effective_service_attributions(
        db,
        services=[service],
    )[service.id] is None
