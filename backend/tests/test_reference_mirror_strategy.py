from __future__ import annotations

from types import SimpleNamespace

from app.reference_layers import mirror_strategy
from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
)


def _layer(source_key: str, *, options=None, layer_id: int = 1):
    return SimpleNamespace(
        id=layer_id,
        source_key=source_key,
        options_json=options,
    )


def test_invalid_composition_is_durable_blocked_assignment() -> None:
    assignment = mirror_strategy._derive_assignment(
        _layer("layer:a", options={"mirror_composition": {"dependencies": []}}),
        None,
        {"layer:a": _layer("layer:a")},
    )

    assert assignment.strategy == "blocked"
    assert assignment.reason_code == "composition_dependency_invalid"


def test_source_discovery_blocker_evidence_is_preserved(
    monkeypatch,
) -> None:
    def blocked(*_args):
        raise mirror_strategy.SourceDiscoveryError(
            "official download is unavailable",
            code="official_download_unavailable",
            evidence={
                "wms_tiles_eligible": False,
                "wms_guard": "bulk_wms_prohibited",
            },
        )

    monkeypatch.setattr(mirror_strategy, "acquisition_candidates", blocked)
    monkeypatch.setattr(
        mirror_strategy,
        "_service_definition",
        lambda _service: object(),
    )
    monkeypatch.setattr(
        mirror_strategy,
        "_layer_definition",
        lambda _record: object(),
    )
    catalog_layer = _layer("layer:flood")

    assignment = mirror_strategy._derive_assignment(
        catalog_layer,
        object(),
        {catalog_layer.source_key: catalog_layer},
    )

    assert assignment.strategy == "blocked"
    assert assignment.reason_code == "official_download_unavailable"
    assert assignment.evidence["wms_tiles_eligible"] is False
    assert assignment.evidence["wms_guard"] == "bulk_wms_prohibited"


def test_composition_cycle_is_blocked_without_aborting_other_layers() -> None:
    a = _layer(
        "layer:a",
        options={"mirror_composition": {"dependencies": ["layer:b"]}},
        layer_id=1,
    )
    b = _layer(
        "layer:b",
        options={"mirror_composition": {"dependencies": ["layer:a"]}},
        layer_id=2,
    )
    vector = mirror_strategy.StrategyAssignment(
        layer_id=3,
        layer_source_key="layer:c",
        strategy="vector",
        source_key="auto:c",
        dependency_source_keys=(),
        reason_code="candidate_selected",
        reason="selected",
        evidence={},
    )
    assignments = [
        mirror_strategy._derive_assignment(a, None, {"layer:a": a, "layer:b": b}),
        mirror_strategy._derive_assignment(b, None, {"layer:a": a, "layer:b": b}),
        vector,
    ]
    resolved = mirror_strategy._resolve_composition_blockers(
        assignments,
        {"layer:a": a, "layer:b": b, "layer:c": _layer("layer:c", layer_id=3)},
    )

    assert [item.strategy for item in resolved] == ["blocked", "blocked", "vector"]
    assert all(
        item.reason_code.startswith("composition_dependency")
        for item in resolved[:2]
    )


def test_strategy_plan_hash_changes_when_evidence_changes() -> None:
    assignment = mirror_strategy.StrategyAssignment(
        layer_id=1,
        layer_source_key="layer:a",
        strategy="blocked",
        source_key=None,
        dependency_source_keys=(),
        reason_code="source_candidate_missing",
        reason="none",
        evidence={"candidate_count": 0},
    )
    first = mirror_strategy.MirrorStrategyPlan(
        provider_key="siur",
        snapshot_id=1,
        catalog_definition_sha256="a" * 64,
        assignments=(assignment,),
    )
    changed = mirror_strategy.MirrorStrategyPlan(
        provider_key="siur",
        snapshot_id=1,
        catalog_definition_sha256="a" * 64,
        assignments=(
            mirror_strategy.StrategyAssignment(
                **{**assignment.__dict__, "evidence": {"candidate_count": 1}}
            ),
        ),
    )

    assert first.plan_sha256 != changed.plan_sha256


def test_reviewed_ortho_strategy_preserves_exact_and_degraded_evidence(
    monkeypatch,
) -> None:
    service = ReferenceServiceDefinition(
        source_key="service:itacyl",
        title="Ortofotos ITACyL",
        upstream_protocol="wms",
        base_url="https://orto.wms.itacyl.es/WMS",
    )
    monkeypatch.setattr(
        mirror_strategy,
        "_service_definition",
        lambda _service: service,
    )

    for layer_id, catalog_name, status, strategy, reason_code in (
        (
            1,
            "Ortofoto_2020",
            "exact",
            "tiles",
            "candidate_exact_official_substitution",
        ),
        (
            2,
            "Ortofoto_2002",
            "substitute_degraded",
            "tiles",
            "candidate_substitute_degraded",
        ),
        (
            3,
            "Ortofoto_2021",
            "blocked",
            "blocked",
            "reviewed_ortho_substitution_blocked",
        ),
    ):
        source_key = "layer:siur:" + str(layer_id) * 64
        definition = ReferenceLayerDefinition(
            source_key=source_key,
            node_type="layer",
            title=catalog_name,
            service_key=service.source_key,
            remote_name=catalog_name,
            role="overlay",
            renderer="raster_tile",
            delivery_mode="mirror",
        )
        catalog_layer = _layer(source_key, layer_id=layer_id)
        monkeypatch.setattr(
            mirror_strategy,
            "_layer_definition",
            lambda _layer, definition=definition: definition,
        )

        assignment = mirror_strategy._derive_assignment(
            catalog_layer,
            object(),
            {source_key: catalog_layer},
        )

        assert assignment.strategy == strategy
        assert assignment.reason_code == reason_code
        substitution = assignment.evidence["ortho_substitution"]
        assert substitution["equivalence_status"] == status
        assert substitution["promotion_eligible"] is (strategy == "tiles")
        if strategy == "tiles":
            assert assignment.evidence["protocol"] == "wms_tiles"
        else:
            assert assignment.source_key is None
        if status == "substitute_degraded":
            assert "degradado" in substitution["public_notice"].casefold()
            assert "explicit_public_degradation_notice" in substitution[
                "parity_requirements"
            ]
