from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.reference_layers import (
    mirror_lifecycle,
    mirror_reconcile,
    mirror_strategy,
)
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.models import (
    ReferenceLayerMirrorStrategy,
    ReferenceLayerSource,
)
from app.reference_layers.source_discovery import SourceCandidate


NOW = datetime(2026, 7, 27, 20, tzinfo=timezone.utc)


def _catalog() -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key="siur",
        source_url="https://catalog.example.test/siur.json",
        raw_catalog={"revision": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service:test",
                title="Test service",
                upstream_protocol="wms",
                base_url="https://maps.example.test/wms",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:test",
                node_type="layer",
                title="Test layer",
                service_key="service:test",
                remote_name="test:layer",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=NOW,
    )


def _candidate(revision: int) -> SourceCandidate:
    identity = {
        "protocol": "download",
        "target_kind": "vector",
        "endpoint_url": (
            f"https://downloads.example.test/layer-v{revision}.gpkg"
        ),
        "remote_name": f"layer_v{revision}",
        "sync_strategy": "conditional_get",
        "priority": 10,
        "config": {
            "format": "geopackage",
            "revision": revision,
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return SourceCandidate(
        **identity,
        source_key=f"auto:download:{digest[:32]}",
        definition_sha256=digest,
    )


def _install_discovery(monkeypatch, selected: list[SourceCandidate]) -> None:
    def discover(_service, _layer):
        return tuple(selected)

    monkeypatch.setattr(
        mirror_lifecycle,
        "acquisition_candidates",
        discover,
    )
    monkeypatch.setattr(
        mirror_strategy,
        "acquisition_candidates",
        discover,
    )


def test_same_snapshot_candidate_change_reconciles_generation_idempotently(
    db,
    monkeypatch,
) -> None:
    snapshot, _ = apply_catalog_definition(db, _catalog())
    selected = [_candidate(1)]
    _install_discovery(monkeypatch, selected)

    first_plan = mirror_reconcile.build_mirror_reconciliation_plan(db)

    assert first_plan.source_plan.snapshot_id == snapshot.id
    assert first_plan.strategy_before.generation == 0
    assert first_plan.strategy_before.matches_plan is False
    assert db.scalar(select(func.count(ReferenceLayerSource.id))) == 0
    first, first_status = mirror_reconcile.apply_mirror_reconciliation_plan(
        db,
        first_plan,
        expected_plan_sha256=first_plan.plan_sha256,
    )
    old_source = db.scalar(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.source_key == selected[0].source_key
        )
    )

    assert old_source is not None
    assert old_source.enabled is True
    assert old_source.is_primary is True
    assert first.created_count == 1
    assert first.strategy_generation == 1
    assert first.strategy_created_count == 1
    assert first_status.matches_plan is True

    selected[:] = [_candidate(2)]
    second_plan = mirror_reconcile.build_mirror_reconciliation_plan(db)

    assert second_plan.source_plan.snapshot_id == snapshot.id
    assert len(second_plan.source_plan.new_source_keys) == 1
    assert len(second_plan.source_plan.deactivated_source_keys) == 1
    assert second_plan.strategy_before.generation == 1
    assert second_plan.strategy_before.complete is True
    assert second_plan.strategy_before.matches_plan is False
    with pytest.raises(
        mirror_reconcile.MirrorReconciliationError,
        match="plan changed",
    ):
        mirror_reconcile.apply_mirror_reconciliation_plan(
            db,
            second_plan,
            expected_plan_sha256=first_plan.plan_sha256,
        )

    second, second_status = (
        mirror_reconcile.apply_mirror_reconciliation_plan(
            db,
            second_plan,
            expected_plan_sha256=second_plan.plan_sha256,
        )
    )
    db.refresh(old_source)
    new_source = db.scalar(
        select(ReferenceLayerSource).where(
            ReferenceLayerSource.source_key == selected[0].source_key
        )
    )
    latest_strategy = db.scalar(
        select(ReferenceLayerMirrorStrategy).where(
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot.id,
            ReferenceLayerMirrorStrategy.generation == 2,
        )
    )

    assert old_source.enabled is False
    assert old_source.is_primary is False
    assert new_source is not None
    assert new_source.enabled is True
    assert new_source.is_primary is True
    assert second.created_count == 1
    assert second.deactivated_count == 1
    assert second.strategy_generation == 2
    assert second.strategy_created_count == 1
    assert latest_strategy.source_id == new_source.id
    assert second_status.complete is True
    assert second_status.matches_plan is True

    third_plan = mirror_reconcile.build_mirror_reconciliation_plan(db)
    third, third_status = mirror_reconcile.apply_mirror_reconciliation_plan(
        db,
        third_plan,
        expected_plan_sha256=third_plan.plan_sha256,
    )

    assert third_plan.source_plan.new_source_keys == ()
    assert third_plan.source_plan.updated_source_keys == ()
    assert third_plan.source_plan.deactivated_source_keys == ()
    assert third_plan.strategy_before.matches_plan is True
    assert third.created_count == 0
    assert third.updated_count == 0
    assert third.deactivated_count == 0
    assert third.strategy_generation == 2
    assert third.strategy_created_count == 0
    assert third.strategy_unchanged_count == 1
    assert third_status.matches_plan is True
    assert db.scalar(
        select(func.count(ReferenceLayerMirrorStrategy.id)).where(
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot.id
        )
    ) == 2


def test_cli_defaults_to_non_mutating_reconcile_only_dry_run(
    db,
    monkeypatch,
    capsys,
) -> None:
    apply_catalog_definition(db, _catalog())
    selected = [_candidate(1)]
    _install_discovery(monkeypatch, selected)
    monkeypatch.setattr(
        mirror_reconcile,
        "SessionLocal",
        lambda: nullcontext(db),
    )
    monkeypatch.setattr(
        mirror_reconcile,
        "register_all_models",
        lambda: None,
    )

    result = mirror_reconcile.main([])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    assert payload["applied"] is False
    assert payload["ready"] is False
    assert payload["drift_detected"] is True
    assert payload["sources"]["new_count"] == 1
    assert payload["strategies"]["before"]["drift_detected"] is True
    assert payload["safety"] == {
        "network_performed": False,
        "watchers_run": False,
        "runs_enqueued": False,
        "downloads_started": False,
        "authorization_created": False,
        "delivery_mutation_performed": False,
    }
    assert db.scalar(select(func.count(ReferenceLayerSource.id))) == 0
    assert db.scalar(
        select(func.count(ReferenceLayerMirrorStrategy.id))
    ) == 0


def test_cli_apply_uses_the_exact_reviewed_dry_run_hash(
    db,
    monkeypatch,
    capsys,
) -> None:
    apply_catalog_definition(db, _catalog())
    selected = [_candidate(1)]
    _install_discovery(monkeypatch, selected)
    monkeypatch.setattr(
        mirror_reconcile,
        "SessionLocal",
        lambda: nullcontext(db),
    )
    monkeypatch.setattr(
        mirror_reconcile,
        "register_all_models",
        lambda: None,
    )
    assert mirror_reconcile.main([]) == 0
    dry_run = json.loads(capsys.readouterr().out)

    result = mirror_reconcile.main(
        [
            "--apply",
            "--expected-plan-sha256",
            dry_run["plan_sha256"],
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["mode"] == "apply"
    assert payload["applied"] is True
    assert payload["changed"] is True
    assert payload["ready"] is True
    assert payload["drift_detected"] is False
    assert payload["sources"]["apply_result"]["created_count"] == 1
    assert payload["strategies"]["apply_result"]["generation"] == 1
    assert payload["strategies"]["after"]["matches_plan"] is True


def test_cli_apply_requires_reviewed_plan_hash(capsys) -> None:
    result = mirror_reconcile.main(["--apply"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload == {
        "ok": False,
        "error_code": "operator_confirmation_missing",
        "error_summary": "--apply requires --expected-plan-sha256",
    }
