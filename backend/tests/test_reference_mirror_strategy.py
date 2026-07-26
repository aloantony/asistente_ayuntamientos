from __future__ import annotations

from types import SimpleNamespace

from app.reference_layers import mirror_strategy


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
