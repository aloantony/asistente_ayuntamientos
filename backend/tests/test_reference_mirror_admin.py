from dataclasses import replace
import json
import io
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.catalog import apply_catalog_definition
from app.reference_layers.mirror_admin import (
    MirrorAdminInputError,
    _parser,
    execute_manual_enqueue,
    execute_transition,
    operational_status,
    staging_gc,
)
from app.reference_layers.mirror_lifecycle import (
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
    deactivate_delivery,
)
from app.reference_layers.models import (
    ReferenceDeliveryPromotion,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceService,
)
from test_reference_mirror_lifecycle import (
    _definition,
    _promote_cross_snapshot_versions,
    _seed_bootstrap,
)
from support_reference_mirror_authorization import (
    authorize_mirror_source,
    ensure_authorized_mirror_source,
)


@pytest.fixture
def metadata_store(tmp_path):
    with ReferenceBlobStore(tmp_path / "reference-data") as store:
        yield store


def test_operational_status_uses_explicit_local_mirror_authorization(
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
    service = db.get(ReferenceService, layer.service_id)
    service.license_status = "pending"
    db.commit()

    report = operational_status(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
    )

    assert report["ok"] is True
    assert report["catalog_snapshot_id"] == snapshot_v2.id
    assert report["summary"]["layer_count"] == 1
    assert report["summary"]["service_count"] == 1
    assert report["summary"]["authorization_model"] == (
        "source_mirror_review"
    )
    assert report["summary"]["legacy_wms_evidence_required"] is False
    assert report["summary"]["mirror_authorization_missing_count"] == 0
    assert (
        report["summary"]["mirror_authorization_missing_service_count"]
        == 0
    )
    assert report["summary"]["license_review_service_count"] == 0
    assert report["summary"]["delivery_attestation_service_count"] == 0
    assert report["summary"]["catalog_license_status_counts"] == {
        "pending": 1
    }
    row = report["layers"][0]
    assert all(
        source["definition_sha256"]
        and source["expected_active_generation"]
        == row["delivery_state_integrity"]["generation"]
        for source in row["sources"]
    )
    assert row["active"]["version_id"] == version_v2.id
    assert row["active"]["uses_current_catalog_snapshot"] is True
    assert row["active"]["size_bytes"] is None
    assert row["active"]["size_bytes_complete"] is False
    assert row["active"]["validation"]["passed"] is True
    assert row["active"]["validation"]["validated_at"] is not None
    assert row["active"]["sync_run"]["duration_seconds"] == pytest.approx(1)
    assert row["last_run"]["duration_seconds"] == pytest.approx(1)
    assert row["previous"]["version_id"] == version_v1.id
    assert row["previous"]["catalog_snapshot_id"] == snapshot_v1.id
    assert row["previous"]["uses_current_catalog_snapshot"] is False
    assert row["delivery_state_integrity"]["valid"] is True
    assert row["recovery"] == {
        "action": "rollback",
        "target_version_id": version_v1.id,
        "expected_generation": 2,
        "requires_explicit_actor_reason_and_dry_run": True,
    }
    authorization = row["mirror_authorization"]
    assert authorization["authorization_model"] == "source_mirror_review"
    assert authorization["legacy_wms_evidence_required"] is False
    assert authorization["mirror_authorized"] is True
    assert authorization["catalog_license_status"] == "pending"
    assert authorization["authorization_status"] == "authorized"
    assert authorization["blocking_reasons"] == []
    assert authorization["mirror_review_count"] == 1
    assert authorization["reviewed_source_count"] == 1
    assert authorization["enabled_source_count"] == 1
    assert report["retention"]["published_blob_gc_enabled"] is False
    assert report["retention"]["protected_delivery_version_count"] == 2


def test_layer_status_ignores_unauthorized_sibling_layers_on_same_service(
    db,
) -> None:
    provider_key = "mirror-admin-layer-authorization-scope"
    base = _definition(provider_key=provider_key)
    selected_definition = replace(
        base.layers[0],
        source_key="layer:selected",
        title="Selected planning zones",
        remote_name="planning:selected",
    )
    sibling_definition = replace(
        base.layers[0],
        source_key="layer:sibling",
        title="Sibling planning zones",
        remote_name="planning:sibling",
    )
    definition = replace(
        base,
        raw_catalog={"revision": "shared-service-two-layers"},
        layers=(selected_definition, sibling_definition),
    )
    apply_catalog_definition(db, definition)
    apply_mirror_bootstrap_plan(
        db,
        build_mirror_bootstrap_plan(db, provider_key=provider_key),
    )
    selected_layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.source_key == "layer:selected",
        )
    )
    sibling_layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.source_key == "layer:sibling",
        )
    )
    assert selected_layer is not None
    assert sibling_layer is not None
    selected_sources = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.layer_id == selected_layer.id)
            .order_by(ReferenceLayerSource.id)
        )
    )
    selected_source = next(
        source
        for source in selected_sources
        if source.target_kind == "vector"
    )
    for source in selected_sources:
        source.enabled = source.id == selected_source.id
        source.is_primary = False
    db.flush()
    selected_source.is_primary = True
    db.commit()
    authorize_mirror_source(db, selected_source)
    assert db.scalar(
        select(func.count(ReferenceLayerSource.id)).where(
            ReferenceLayerSource.layer_id == sibling_layer.id,
            ReferenceLayerSource.enabled.is_(True),
        )
    )

    report = operational_status(
        db,
        provider_key=provider_key,
        layer_id=selected_layer.id,
    )

    authorization = report["layers"][0]["mirror_authorization"]
    assert authorization["authorization_scope"] == "layer_enabled_sources"
    assert authorization["mirror_authorized"] is True
    assert authorization["blocking_reasons"] == []
    assert authorization["reviewed_source_ids"] == [selected_source.id]
    assert authorization["reviewed_source_count"] == 1
    assert authorization["enabled_source_count"] == 1
    assert report["summary"]["mirror_authorization_missing_count"] == 0
    assert (
        report["summary"]["mirror_authorization_missing_service_count"]
        == 0
    )
    assert report["summary"]["mirror_authorization_missing_source_count"] == 0
    source_rows = {
        row["source_id"]: row for row in report["layers"][0]["sources"]
    }
    assert source_rows[selected_source.id]["mirror_authorization"][
        "authorization_scope"
    ] == "source"
    assert source_rows[selected_source.id]["mirror_authorization"][
        "mirror_authorized"
    ] is True


def test_layer_status_requires_authorization_for_every_enabled_source(
    db,
) -> None:
    _, layer, _, sources, _ = _seed_bootstrap(
        db,
        provider_key="mirror-admin-source-authorization-scope",
    )
    reviewed_source = next(source for source in sources if source.is_primary)
    authorize_mirror_source(db, reviewed_source)
    enabled_sources = [source for source in sources if source.enabled]
    assert len(enabled_sources) > 1

    report = operational_status(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
    )

    authorization = report["layers"][0]["mirror_authorization"]
    assert authorization["mirror_authorized"] is False
    assert authorization["blocking_reasons"] == [
        "mirror_authorization_missing"
    ]
    assert authorization["reviewed_source_count"] == 1
    assert authorization["enabled_source_count"] == len(enabled_sources)
    assert report["summary"]["mirror_authorization_missing_count"] == 1
    assert (
        report["summary"]["mirror_authorization_missing_source_count"]
        == len(enabled_sources) - 1
    )
    source_authorizations = {
        row["source_id"]: row["mirror_authorization"]
        for row in report["layers"][0]["sources"]
        if row["enabled"]
    }
    assert source_authorizations[reviewed_source.id]["mirror_authorized"] is True
    assert all(
        item["blocking_reasons"] == ["mirror_authorization_missing"]
        for source_id, item in source_authorizations.items()
        if source_id != reviewed_source.id
    )


def test_rollback_dry_run_is_generation_fenced_and_never_appends_event(
    db,
    make_user,
    metadata_store,
) -> None:
    layer, _, _, version_v1, version_v2 = (
        _promote_cross_snapshot_versions(db, metadata_store)
    )
    actor = make_user()
    before_count = db.scalar(
        select(func.count(ReferenceDeliveryPromotion.id)).where(
            ReferenceDeliveryPromotion.provider_key == layer.provider_key,
            ReferenceDeliveryPromotion.layer_id == layer.id,
        )
    )

    result = execute_transition(
        db,
        store=metadata_store,
        action="rollback",
        provider_key=layer.provider_key,
        layer_id=layer.id,
        target_version_id=version_v1.id,
        expected_generation=2,
        actor_user_id=actor.id,
        reason="renderer regression confirmed by operator",
        apply=False,
    )

    assert result["mode"] == "dry-run"
    assert result["applied"] is False
    assert result["transition"] == {
        "provider_key": layer.provider_key,
        "layer_id": layer.id,
        "action": "rollback",
        "from_version_id": version_v2.id,
        "to_version_id": version_v1.id,
        "expected_generation": 2,
        "resulting_generation": 3,
    }
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == version_v2.id
    assert state.generation == 2
    assert db.scalar(
        select(func.count(ReferenceDeliveryPromotion.id)).where(
            ReferenceDeliveryPromotion.provider_key == layer.provider_key,
            ReferenceDeliveryPromotion.layer_id == layer.id,
        )
    ) == before_count


def test_rollback_apply_records_active_actor_and_reason(
    db,
    make_user,
    metadata_store,
) -> None:
    layer, _, _, version_v1, _ = _promote_cross_snapshot_versions(
        db,
        metadata_store,
    )
    actor = make_user()
    reason = "restore the last validated renderer"

    result = execute_transition(
        db,
        store=metadata_store,
        action="rollback",
        provider_key=layer.provider_key,
        layer_id=layer.id,
        target_version_id=version_v1.id,
        expected_generation=2,
        actor_user_id=actor.id,
        reason=reason,
        apply=True,
    )

    assert result["mode"] == "apply"
    assert result["transition"]["generation"] == 3
    promotion = db.get(
        ReferenceDeliveryPromotion,
        result["transition"]["promotion_id"],
    )
    assert promotion.actor_id == actor.id
    assert promotion.reason == reason
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == version_v1.id
    assert state.generation == 3


def test_transition_rejects_inactive_actor_before_lifecycle_mutation(
    db,
    make_user,
    metadata_store,
) -> None:
    layer, _, _, version_v1, version_v2 = (
        _promote_cross_snapshot_versions(db, metadata_store)
    )
    actor = make_user(is_active=False)

    with pytest.raises(MirrorAdminInputError, match="inactive"):
        execute_transition(
            db,
            store=metadata_store,
            action="rollback",
            provider_key=layer.provider_key,
            layer_id=layer.id,
            target_version_id=version_v1.id,
            expected_generation=2,
            actor_user_id=actor.id,
            reason="this must never be recorded",
            apply=True,
        )

    db.rollback()
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.active_version_id == version_v2.id
    assert state.generation == 2


def test_reactivation_dry_run_recovers_only_a_previously_served_version(
    db,
    make_user,
    metadata_store,
) -> None:
    layer, _, _, version_v1, version_v2 = (
        _promote_cross_snapshot_versions(db, metadata_store)
    )
    deactivate_delivery(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
        expected_generation=2,
        reason="maintenance isolation",
    )
    actor = make_user()

    result = execute_transition(
        db,
        store=metadata_store,
        action="reactivate",
        provider_key=layer.provider_key,
        layer_id=layer.id,
        target_version_id=version_v1.id,
        expected_generation=3,
        actor_user_id=actor.id,
        reason="recover validated pre-maintenance version",
        apply=False,
    )

    assert result["transition"]["action"] == "reactivate"
    assert result["transition"]["to_version_id"] == version_v1.id
    state = db.get(
        ReferenceLayerDeliveryState,
        (layer.provider_key, layer.id),
    )
    assert state.status == "disabled"
    assert state.active_version_id is None
    assert state.generation == 3
    assert version_v2.id != version_v1.id


def test_staging_gc_defaults_to_non_mutating_plan_and_never_deletes_blobs(
    tmp_path,
) -> None:
    store = ReferenceBlobStore(
        tmp_path / "reference-data",
        max_blob_bytes=1024,
    )
    try:
        published = store.put_stream(io.BytesIO(b"published"))
        candidate = (
            store.root / "staging" / ("a" * 32 + ".part")
        )
        candidate.write_bytes(b"stale")
        os.utime(candidate, (1, 1))

        plan = staging_gc(
            apply=False,
            older_than_seconds=3_600,
            store=store,
        )
        assert plan["eligible_count"] == 1
        assert plan["deleted_count"] == 0
        assert plan["published_blobs_deleted"] == 0
        assert candidate.exists()
        assert store.resolve_blob(published.storage_key).exists()

        applied = staging_gc(
            apply=True,
            older_than_seconds=3_600,
            store=store,
        )
        assert applied["deleted_count"] == 1
        assert not candidate.exists()
        assert store.resolve_blob(published.storage_key).exists()
    finally:
        store.close()


def test_transition_cli_requires_exactly_one_explicit_mode() -> None:
    base = [
        "rollback",
        "--provider-key",
        "siur",
        "--layer-id",
        "1",
        "--target-version-id",
        "2",
        "--expected-generation",
        "3",
        "--actor-user-id",
        "4",
        "--reason",
        "operator-approved rollback",
    ]
    with pytest.raises(SystemExit):
        _parser().parse_args(base)
    with pytest.raises(SystemExit):
        _parser().parse_args([*base, "--dry-run", "--apply"])
    parsed = _parser().parse_args([*base, "--dry-run"])
    assert parsed.dry_run is True
    assert parsed.apply is False


def test_manual_enqueue_cli_and_active_actor_gate(
    db,
    make_user,
) -> None:
    _, _, _, sources, _ = _seed_bootstrap(
        db,
        provider_key="mirror-admin-enqueue-test",
    )
    source = next(item for item in sources if item.is_primary)
    ensure_authorized_mirror_source(db, source)
    actor = make_user()
    base = [
        "enqueue",
        "--provider-key",
        source.provider_key,
        "--source-id",
        str(source.id),
        "--expected-source-definition-sha256",
        source.definition_sha256,
        "--expected-generation",
        "0",
        "--actor-user-id",
        str(actor.id),
        "--reason",
        "representative vector acceptance check",
    ]
    with pytest.raises(SystemExit):
        _parser().parse_args(base)
    parsed = _parser().parse_args([*base, "--dry-run"])
    assert parsed.check_mode == "full"
    assert parsed.apply is False

    result = execute_manual_enqueue(
        db,
        provider_key=source.provider_key,
        source_id=source.id,
        expected_source_definition_sha256=source.definition_sha256,
        expected_generation=0,
        check_mode="conditional",
        actor_user_id=actor.id,
        reason="representative vector acceptance check",
        apply=False,
    )

    assert result["applied"] is False
    assert result["manual_sync"]["run_id"] is None
    assert result["manual_sync"]["check_mode"] == "conditional"

    inactive = make_user(is_active=False)
    with pytest.raises(MirrorAdminInputError, match="inactive"):
        execute_manual_enqueue(
            db,
            provider_key=source.provider_key,
            source_id=source.id,
            expected_source_definition_sha256=source.definition_sha256,
            expected_generation=0,
            check_mode="full",
            actor_user_id=inactive.id,
            reason="must be rejected",
            apply=True,
        )


@pytest.mark.parametrize("seconds", (0, 3_599, 2_592_001, True))
def test_staging_retention_setting_is_bounded(seconds) -> None:
    with pytest.raises(ValueError, match="retention"):
        Settings(
            _env_file=None,
            reference_staging_retention_seconds=seconds,
        )


def test_status_report_is_json_serializable(
    db,
    metadata_store,
) -> None:
    layer, _, _, _, _ = _promote_cross_snapshot_versions(
        db,
        metadata_store,
    )
    report = operational_status(
        db,
        provider_key=layer.provider_key,
        layer_id=layer.id,
    )

    encoded = json.dumps(
        report,
        default=lambda value: (
            value.astimezone(timezone.utc).isoformat()
            if isinstance(value, datetime)
            else str(value)
        ),
    )
    assert '"mirror_authorization"' in encoded
