from datetime import datetime, timedelta, timezone

import app.reference_layers.mirror_readiness as mirror_readiness
import pytest
from sqlalchemy import func, select

from app.reference_layers.blob_store import ReferenceBlobStore
from app.reference_layers.mirror_lifecycle import (
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
)
from app.reference_layers.mirror_authorization import source_authorization_blocker
from app.reference_layers.mirror_readiness import (
    READINESS_SCHEMA_VERSION,
    build_mirror_readiness_report,
    readiness_exit_code,
)
from app.reference_layers.models import (
    ReferenceDeliveryPromotion,
    ReferenceDeliveryVersion,
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
)
from app.reference_layers.transition_servability import (
    DeliveryTransitionServabilityError,
)
from support_reference_mirror_authorization import (
    ensure_authorized_mirror_source,
)
from test_reference_mirror_lifecycle import (
    _seed_bootstrap,
)
from test_local_reference_delivery import seed_local_delivery


@pytest.fixture
def metadata_store(tmp_path):
    with ReferenceBlobStore(tmp_path / "reference-data") as store:
        yield store


def _requirement(report, code):
    return next(
        requirement
        for requirement in report["requirements"]
        if requirement["code"] == code
    )


def _prepare_structurally_ready_mirror(db) -> tuple[str, int]:
    layer, _, _, _, _, _ = seed_local_delivery(db)
    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=layer.provider_key,
    )
    apply_mirror_bootstrap_plan(db, plan)
    next_check = datetime.now(timezone.utc) + timedelta(days=1)
    sources = tuple(
        db.scalars(
            select(ReferenceLayerSource)
            .where(
                ReferenceLayerSource.provider_key == layer.provider_key,
                ReferenceLayerSource.enabled.is_(True),
            )
            .order_by(ReferenceLayerSource.id)
        )
    )
    for source in sources:
        source.next_check_at = next_check
        if source_authorization_blocker(db, source=source) is not None:
            ensure_authorized_mirror_source(
                db,
                source,
                reviewed_at=datetime.now(timezone.utc),
            )
    db.commit()
    return layer.provider_key, layer.id


def _metadata_is_available(monkeypatch, layer_id: int) -> None:
    monkeypatch.setattr(
        mirror_readiness,
        "catalog_local_metadata_availability",
        lambda *_args, **_kwargs: {layer_id: True},
    )


def test_readiness_report_is_read_only_and_explains_missing_deliveries(
    db,
    metadata_store,
) -> None:
    _, layer, _, sources, _ = _seed_bootstrap(
        db,
        provider_key="readiness-empty",
    )
    for source in sources:
        source.next_check_at = datetime.now(timezone.utc) + timedelta(days=1)
    db.commit()
    counts_before = (
        db.scalar(select(func.count(ReferenceMirrorAuthorizationReview.id))),
        db.scalar(select(func.count(ReferenceDeliveryVersion.id))),
        db.scalar(select(func.count(ReferenceDeliveryPromotion.id))),
    )

    report = build_mirror_readiness_report(
        db,
        metadata_store,
        provider_key=layer.provider_key,
        remote_proxy_enabled=False,
    )

    assert report["schema_version"] == READINESS_SCHEMA_VERSION
    assert report["ok"] is True
    assert report["ready"] is False
    assert readiness_exit_code(report) == 1
    assert _requirement(report, "catalog_integrity")["passed"] is True
    assert _requirement(report, "strategy_reconciliation")["passed"] is True
    assert (
        _requirement(report, "current_source_authorizations")["passed"]
        is False
    )
    assert _requirement(report, "local_deliveries") == {
        "code": "local_deliveries",
        "passed": False,
        "expected": 1,
        "actual": 0,
    }
    assert report["summary"]["layers_incomplete_count"] == 1
    assert report["safety"] == {
        "database_writes": False,
        "upstream_network": False,
        "watchers_run": False,
        "runs_enqueued": False,
        "downloads_started": False,
        "delivery_mutated": False,
    }
    counts_after = (
        db.scalar(select(func.count(ReferenceMirrorAuthorizationReview.id))),
        db.scalar(select(func.count(ReferenceDeliveryVersion.id))),
        db.scalar(select(func.count(ReferenceDeliveryPromotion.id))),
    )
    assert counts_after == counts_before


def test_structural_readiness_requires_all_local_evidence(
    db,
    metadata_store,
    monkeypatch,
) -> None:
    provider_key, layer_id = _prepare_structurally_ready_mirror(db)
    _metadata_is_available(monkeypatch, layer_id)

    report = build_mirror_readiness_report(
        db,
        metadata_store,
        provider_key=provider_key,
        remote_proxy_enabled=False,
    )

    assert report["ready"] is True, report
    assert readiness_exit_code(report) == 0
    assert report["profile"] == "structural"
    assert report["summary"]["leaf_layer_count"] == 1
    assert report["summary"]["layers_ready_count"] == 1
    assert report["summary"]["local_delivery_available_count"] == 1
    assert report["summary"]["metadata_available_count"] == 1
    assert report["summary"]["style_complete_layer_count"] == 1
    assert report["summary"]["unauthorized_enabled_source_count"] == 0
    assert report["summary"]["source_drift_count"] == 0
    assert len(report["state_fence"]["report_sha256"]) == 64
    assert all(requirement["passed"] for requirement in report["requirements"])
    assert report["layers"][0]["readiness"] == "ready"
    assert report["acceptance_scope"][
        "structural_mirror_readiness_proven"
    ] is True
    assert report["acceptance_scope"]["physical_delivery_proven"] is False


def test_readiness_fails_when_remote_proxy_is_enabled(
    db,
    metadata_store,
    monkeypatch,
) -> None:
    provider_key, layer_id = _prepare_structurally_ready_mirror(db)
    _metadata_is_available(monkeypatch, layer_id)

    report = build_mirror_readiness_report(
        db,
        metadata_store,
        provider_key=provider_key,
        remote_proxy_enabled=True,
    )

    assert report["ready"] is False
    assert _requirement(report, "remote_proxy_disabled") == {
        "code": "remote_proxy_disabled",
        "passed": False,
        "expected": False,
        "actual": True,
    }


def test_physical_profile_invokes_only_local_verifiers(
    db,
    metadata_store,
    monkeypatch,
) -> None:
    provider_key, layer_id = _prepare_structurally_ready_mirror(db)
    _metadata_is_available(monkeypatch, layer_id)
    verified: list[tuple[str, int]] = []

    def metadata_verifier(store, session, version):
        assert store is metadata_store
        assert session is db
        verified.append(("metadata", version.id))
        return object()

    def physical_verifier(store, session, version):
        assert store is metadata_store
        assert session is db
        verified.append(("delivery", version.id))
        return object()

    report = build_mirror_readiness_report(
        db,
        metadata_store,
        provider_key=provider_key,
        remote_proxy_enabled=False,
        physical=True,
        metadata_verifier=metadata_verifier,
        physical_verifier=physical_verifier,
    )

    assert report["ready"] is True, report
    assert report["profile"] == "physical"
    assert report["summary"]["physical_verified_count"] == 1
    assert _requirement(report, "physical_delivery_integrity")["passed"]
    assert report["acceptance_scope"]["physical_delivery_proven"] is True
    assert [kind for kind, _ in verified] == ["metadata", "delivery"]
    assert report["layers"][0]["physical"] == {
        "checked": True,
        "passed": True,
        "blocker": None,
    }


def test_physical_profile_fails_closed_on_local_servability_error(
    db,
    metadata_store,
    monkeypatch,
) -> None:
    provider_key, layer_id = _prepare_structurally_ready_mirror(db)
    _metadata_is_available(monkeypatch, layer_id)

    def physical_verifier(_store, _session, _version):
        raise DeliveryTransitionServabilityError("local smoke failed")

    report = build_mirror_readiness_report(
        db,
        metadata_store,
        provider_key=provider_key,
        remote_proxy_enabled=False,
        physical=True,
        metadata_verifier=lambda *_args: object(),
        physical_verifier=physical_verifier,
    )

    assert report["ready"] is False
    assert report["summary"]["physical_verified_count"] == 0
    assert (
        _requirement(report, "physical_delivery_integrity")["passed"]
        is False
    )
    assert "physical_delivery_invalid" in report["layers"][0]["blockers"]
    assert report["acceptance_scope"]["physical_delivery_proven"] is False
