"""Durable, fail-closed strategy assignments for SIUR mirror layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers.catalog import canonical_normalized_definition_sha256
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerMirrorStrategy,
    ReferenceLayerMirrorStrategyDependency,
    ReferenceLayerSource,
    ReferenceService,
)
from app.reference_layers.source_audit import (
    _layer_definition,
    _service_definition,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
)


STRATEGY_PLAN_SCHEMA = "siur-mirror-strategy-plan-v1"
STRATEGIES = frozenset(
    {"vector", "raster", "tiles", "composition", "blocked"}
)
_MAX_REASON_LENGTH = 4_096


class MirrorStrategyError(RuntimeError):
    """Raised when a durable strategy plan cannot be safely applied."""


@dataclass(frozen=True)
class StrategyAssignment:
    layer_id: int
    layer_source_key: str
    strategy: str
    source_key: str | None
    dependency_source_keys: tuple[str, ...]
    reason_code: str
    reason: str
    evidence: dict[str, Any]

    def identity(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "layer_source_key": self.layer_source_key,
            "strategy": self.strategy,
            "source_key": self.source_key,
            "dependency_source_keys": list(self.dependency_source_keys),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class MirrorStrategyPlan:
    provider_key: str
    snapshot_id: int
    catalog_definition_sha256: str
    assignments: tuple[StrategyAssignment, ...]

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(
            {
                "schema_version": STRATEGY_PLAN_SCHEMA,
                "provider_key": self.provider_key,
                "snapshot_id": self.snapshot_id,
                "catalog_definition_sha256": self.catalog_definition_sha256,
                "assignments": [item.identity() for item in self.assignments],
            }
        )


@dataclass(frozen=True)
class AppliedMirrorStrategyPlan:
    plan_sha256: str
    created_count: int
    unchanged_count: int
    blocked_count: int


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_mirror_strategy_plan(
    db: Session,
    *,
    provider_key: str,
) -> MirrorStrategyPlan:
    snapshot = _current_snapshot(db, provider_key)
    if (
        canonical_normalized_definition_sha256(
            snapshot.normalized_definition_json
        )
        != snapshot.definition_sha256
    ):
        raise MirrorStrategyError("current catalog definition hash is invalid")

    services = {
        item.id: item
        for item in db.scalars(
            select(ReferenceService).where(
                ReferenceService.provider_key == provider_key,
                ReferenceService.last_seen_snapshot_id == snapshot.id,
            )
        )
    }
    layers = tuple(
        db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == provider_key,
                ReferenceLayer.last_seen_snapshot_id == snapshot.id,
                ReferenceLayer.node_type == "layer",
            )
            .order_by(ReferenceLayer.id)
        )
    )
    if not layers:
        raise MirrorStrategyError("current catalog has no leaf layers")
    by_source_key = {item.source_key: item for item in layers}
    assignments: list[StrategyAssignment] = []
    for layer in layers:
        assignments.append(
            _derive_assignment(
                layer,
                services.get(layer.service_id or -1),
                by_source_key,
            )
        )
    assignments = _resolve_composition_blockers(assignments, by_source_key)
    return MirrorStrategyPlan(
        provider_key=provider_key,
        snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        assignments=tuple(assignments),
    )


def apply_mirror_strategy_plan(
    db: Session,
    plan: MirrorStrategyPlan,
) -> AppliedMirrorStrategyPlan:
    """Persist one immutable assignment per current catalog leaf."""

    # Source rows are created immediately before this function inside the
    # bootstrap transaction. Make their identities visible to the FK lookup
    # before deriving the immutable strategy evidence.
    db.flush()
    snapshot = _current_snapshot(db, plan.provider_key)
    if snapshot.id != plan.snapshot_id:
        raise MirrorStrategyError("strategy plan is based on a stale snapshot")
    existing = {
        row.layer_id: row
        for row in db.scalars(
            select(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key == plan.provider_key,
                ReferenceLayerMirrorStrategy.catalog_snapshot_id == plan.snapshot_id,
            )
        )
    }
    layers = {
        item.source_key: item
        for item in db.scalars(
            select(ReferenceLayer).where(
                ReferenceLayer.provider_key == plan.provider_key,
                ReferenceLayer.last_seen_snapshot_id == plan.snapshot_id,
                ReferenceLayer.node_type == "layer",
            )
        )
    }
    created = 0
    unchanged = 0
    row_by_layer: dict[int, ReferenceLayerMirrorStrategy] = {}
    for assignment in plan.assignments:
        row = existing.get(assignment.layer_id)
        source_id = None
        if assignment.source_key is not None:
            layer = layers.get(assignment.layer_source_key)
            if layer is None:
                raise MirrorStrategyError("strategy layer is not in the snapshot")
            source_id = db.scalar(
                select(ReferenceLayerSource.id).where(
                    ReferenceLayerSource.provider_key == plan.provider_key,
                    ReferenceLayerSource.layer_id == assignment.layer_id,
                    ReferenceLayerSource.source_key == assignment.source_key,
                )
            )
            if source_id is None:
                raise MirrorStrategyError(
                    f"strategy source is not reconciled: {assignment.source_key}"
                )
        evidence_sha256 = canonical_sha256(assignment.evidence)
        if row is not None:
            if not _row_matches(
                row,
                assignment,
                source_id,
                evidence_sha256,
                plan.catalog_definition_sha256,
            ):
                raise MirrorStrategyError(
                    "immutable strategy assignment changed for snapshot"
                )
            unchanged += 1
            row_by_layer[assignment.layer_id] = row
            continue
        row = ReferenceLayerMirrorStrategy(
            provider_key=plan.provider_key,
            layer_id=assignment.layer_id,
            catalog_snapshot_id=plan.snapshot_id,
            catalog_definition_sha256=plan.catalog_definition_sha256,
            strategy=assignment.strategy,
            source_id=source_id,
            strategy_reason_code=assignment.reason_code,
            strategy_reason=assignment.reason[:_MAX_REASON_LENGTH],
            evidence_json=assignment.evidence,
            evidence_sha256=evidence_sha256,
            generation=1,
            validated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.flush()
        row_by_layer[assignment.layer_id] = row
        created += 1

    strategy_ids = tuple(row.id for row in row_by_layer.values())
    existing_dependencies = {
        (item.strategy_id, item.dependency_layer_id): item
        for item in db.scalars(
            select(ReferenceLayerMirrorStrategyDependency).where(
                ReferenceLayerMirrorStrategyDependency.provider_key
                == plan.provider_key,
                ReferenceLayerMirrorStrategyDependency.strategy_id.in_(strategy_ids),
            )
        )
    }
    for assignment in plan.assignments:
        row = row_by_layer[assignment.layer_id]
        if assignment.strategy != "composition":
            continue
        for dependency_order, dependency_key in enumerate(
            assignment.dependency_source_keys
        ):
            dependency_layer = layers.get(dependency_key)
            if dependency_layer is None:
                raise MirrorStrategyError(
                    f"composition dependency is not in the snapshot: {dependency_key}"
                )
            dependency_strategy = row_by_layer.get(dependency_layer.id)
            if dependency_strategy is None or dependency_strategy.strategy == "blocked":
                raise MirrorStrategyError(
                    f"composition dependency is blocked: {dependency_key}"
                )
            dependency_key = (row.id, dependency_layer.id)
            existing_dependency = existing_dependencies.get(dependency_key)
            if existing_dependency is not None:
                if existing_dependency.dependency_order != dependency_order:
                    raise MirrorStrategyError(
                        "immutable composition dependency order changed"
                    )
                continue
            db.add(
                ReferenceLayerMirrorStrategyDependency(
                    provider_key=plan.provider_key,
                    strategy_id=row.id,
                    strategy_layer_id=assignment.layer_id,
                    dependency_layer_id=dependency_layer.id,
                    dependency_order=dependency_order,
                )
            )
    db.flush()
    return AppliedMirrorStrategyPlan(
        plan_sha256=plan.plan_sha256,
        created_count=created,
        unchanged_count=unchanged,
        blocked_count=sum(
            item.strategy == "blocked" for item in plan.assignments
        ),
    )


def current_mirror_strategies(
    db: Session,
    *,
    provider_key: str,
    snapshot_id: int,
) -> dict[int, ReferenceLayerMirrorStrategy]:
    return {
        row.layer_id: row
        for row in db.scalars(
            select(ReferenceLayerMirrorStrategy).where(
                ReferenceLayerMirrorStrategy.provider_key == provider_key,
                ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot_id,
            )
        )
    }


def _derive_assignment(
    layer: ReferenceLayer,
    service: ReferenceService | None,
    layers_by_source_key: dict[str, ReferenceLayer],
) -> StrategyAssignment:
    dependencies = _composition_dependencies(layer.options_json)
    if dependencies is not None:
        if not dependencies:
            return _blocked(
                layer,
                "composition_dependency_invalid",
                "composition dependencies must be a non-empty unique list",
                {},
            )
        invalid = [key for key in dependencies if key not in layers_by_source_key]
        if invalid:
            return _blocked(
                layer,
                "composition_dependency_missing",
                f"composition references unknown layers: {', '.join(invalid)}",
                {"dependencies": list(dependencies), "missing": invalid},
            )
        return StrategyAssignment(
            layer_id=layer.id,
            layer_source_key=layer.source_key,
            strategy="composition",
            source_key=None,
            dependency_source_keys=dependencies,
            reason_code="composition_declared",
            reason="composition dependencies are explicitly declared",
            evidence={"dependencies": list(dependencies)},
        )
    if service is None:
        return _blocked(
            layer,
            "catalog_service_missing",
            "layer has no current catalog service",
            {},
        )
    try:
        candidates = acquisition_candidates(
            _service_definition(service),
            _layer_definition(layer),
        )
    except SourceDiscoveryError as error:
        code = getattr(error, "code", "source_discovery_error")
        evidence = getattr(error, "evidence", {})
        if not isinstance(code, str) or not code:
            code = "source_discovery_error"
        if not isinstance(evidence, dict):
            evidence = {}
        return _blocked(
            layer,
            code,
            str(error),
            {
                "error_type": type(error).__name__,
                **evidence,
            },
        )
    if not candidates:
        return _blocked(
            layer,
            "source_candidate_missing",
            "layer has no safe acquisition candidate",
            {},
        )
    selected = min(candidates, key=lambda item: (item.priority, item.source_key))
    strategy = selected.target_kind
    if strategy not in {"vector", "raster", "tiles"}:
        return _blocked(
            layer,
            "source_target_unsupported",
            f"source target kind is unsupported: {selected.target_kind}",
            {"source_key": selected.source_key, "target_kind": selected.target_kind},
        )
    return StrategyAssignment(
        layer_id=layer.id,
        layer_source_key=layer.source_key,
        strategy=strategy,
        source_key=selected.source_key,
        dependency_source_keys=(),
        reason_code="candidate_selected",
        reason="highest-priority safe local acquisition candidate selected",
        evidence={
            "source_key": selected.source_key,
            "protocol": selected.protocol,
            "target_kind": selected.target_kind,
            "priority": selected.priority,
        },
    )


def _blocked(
    layer: ReferenceLayer,
    code: str,
    reason: str,
    evidence: dict[str, Any],
) -> StrategyAssignment:
    return StrategyAssignment(
        layer_id=layer.id,
        layer_source_key=layer.source_key,
        strategy="blocked",
        source_key=None,
        dependency_source_keys=(),
        reason_code=code[:64],
        reason=reason[:_MAX_REASON_LENGTH],
        evidence=evidence,
    )


def _composition_dependencies(
    options: dict[str, Any] | None,
) -> tuple[str, ...] | None:
    if not isinstance(options, dict):
        return None
    composition = options.get("mirror_composition")
    if composition is None:
        return None
    if not isinstance(composition, dict):
        return ()
    raw = composition.get("dependencies")
    if not isinstance(raw, list) or not raw or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        return ()
    normalized = tuple(item.strip() for item in raw)
    if len(set(normalized)) != len(normalized):
        return ()
    return normalized


def _resolve_composition_blockers(
    assignments: list[StrategyAssignment],
    layers_by_source_key: dict[str, ReferenceLayer],
) -> list[StrategyAssignment]:
    by_key = {item.layer_source_key: item for item in assignments}
    state: dict[str, int] = {}
    blocked: dict[str, str] = {}

    def visit(key: str, stack: tuple[str, ...] = ()) -> bool:
        if key in blocked:
            return False
        if state.get(key) == 1:
            for cycle_key in (*stack, key):
                blocked[cycle_key] = "composition_dependency_cycle"
            return False
        if state.get(key) == 2:
            return key not in blocked
        state[key] = 1
        assignment = by_key[key]
        if assignment.strategy == "blocked":
            blocked[key] = assignment.reason_code
            state[key] = 2
            return False
        if assignment.strategy == "composition":
            for dependency in assignment.dependency_source_keys:
                if dependency not in layers_by_source_key:
                    blocked[key] = "composition_dependency_missing"
                    break
                if not visit(dependency, (*stack, key)):
                    blocked[key] = "composition_dependency_blocked"
                    break
        state[key] = 2
        return key not in blocked

    for key in by_key:
        visit(key)
    return [
        _blocked(
            layers_by_source_key[item.layer_source_key],
            blocked[item.layer_source_key],
            "composition cannot be served from its declared dependencies",
            {
                "dependencies": list(item.dependency_source_keys),
                "blocked_dependency": blocked[item.layer_source_key],
            },
        )
        if item.layer_source_key in blocked and item.strategy == "composition"
        else item
        for item in assignments
    ]


def _row_matches(
    row: ReferenceLayerMirrorStrategy,
    assignment: StrategyAssignment,
    source_id: int | None,
    evidence_sha256: str,
    catalog_definition_sha256: str,
) -> bool:
    return (
        row.strategy == assignment.strategy
        and row.source_id == source_id
        and row.strategy_reason_code == assignment.reason_code
        and row.strategy_reason == assignment.reason[:_MAX_REASON_LENGTH]
        and row.evidence_sha256 == evidence_sha256
        and row.catalog_definition_sha256 == catalog_definition_sha256
    )


def _current_snapshot(
    db: Session,
    provider_key: str,
) -> ReferenceCatalogSnapshot:
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    if snapshot is None:
        raise MirrorStrategyError("current applied catalog is unavailable")
    return snapshot


__all__ = [
    "AppliedMirrorStrategyPlan",
    "MirrorStrategyError",
    "MirrorStrategyPlan",
    "STRATEGIES",
    "StrategyAssignment",
    "apply_mirror_strategy_plan",
    "build_mirror_strategy_plan",
    "canonical_sha256",
    "current_mirror_strategies",
]
