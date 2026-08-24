"""Durable, fail-closed strategy assignments for SIUR mirror layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import func, select
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
from app.reference_layers.reviewed_ortho_evidence import (
    reviewed_ign_ortho_catalog_projection,
    reviewed_ign_ortho_public_projection,
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
    generation: int
    created_count: int
    unchanged_count: int
    blocked_count: int


@dataclass(frozen=True)
class MirrorStrategyPlanStatus:
    expected_plan_sha256: str
    generation: int
    row_count: int
    complete: bool
    matches_plan: bool


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
    """Append one complete immutable generation when the plan changed."""

    # Source rows are created immediately before this function inside the
    # bootstrap transaction. Make their identities visible to the FK lookup
    # before deriving the immutable strategy evidence.
    db.flush()
    snapshot = _current_snapshot(db, plan.provider_key, for_update=True)
    if (
        snapshot.id != plan.snapshot_id
        or snapshot.definition_sha256 != plan.catalog_definition_sha256
    ):
        raise MirrorStrategyError(
            "strategy plan is based on a stale snapshot"
        )
    layers_by_source_key = {
        item.source_key: item
        for item in db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == plan.provider_key,
                ReferenceLayer.last_seen_snapshot_id == plan.snapshot_id,
                ReferenceLayer.node_type == "layer",
            )
            .order_by(ReferenceLayer.id)
        )
    }
    _validate_plan_assignments(plan, layers_by_source_key)

    resolved_source_ids: dict[int, int | None] = {}
    evidence_sha256_by_layer: dict[int, str] = {}
    for assignment in plan.assignments:
        source_id = None
        if assignment.source_key is not None:
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
        resolved_source_ids[assignment.layer_id] = source_id
        evidence_sha256_by_layer[assignment.layer_id] = canonical_sha256(
            assignment.evidence
        )

    latest_generation = int(
        db.scalar(
            select(func.max(ReferenceLayerMirrorStrategy.generation)).where(
                ReferenceLayerMirrorStrategy.provider_key
                == plan.provider_key,
                ReferenceLayerMirrorStrategy.catalog_snapshot_id
                == plan.snapshot_id,
            )
        )
        or 0
    )
    existing_rows = (
        list(
            db.scalars(
                select(ReferenceLayerMirrorStrategy)
                .where(
                    ReferenceLayerMirrorStrategy.provider_key
                    == plan.provider_key,
                    ReferenceLayerMirrorStrategy.catalog_snapshot_id
                    == plan.snapshot_id,
                    ReferenceLayerMirrorStrategy.generation
                    == latest_generation,
                )
                .order_by(ReferenceLayerMirrorStrategy.layer_id)
            )
        )
        if latest_generation
        else []
    )
    if existing_rows and _generation_matches_plan(
        db,
        rows=existing_rows,
        plan=plan,
        layers_by_source_key=layers_by_source_key,
        source_ids=resolved_source_ids,
        evidence_sha256_by_layer=evidence_sha256_by_layer,
    ):
        return AppliedMirrorStrategyPlan(
            plan_sha256=plan.plan_sha256,
            generation=latest_generation,
            created_count=0,
            unchanged_count=len(plan.assignments),
            blocked_count=sum(
                item.strategy == "blocked" for item in plan.assignments
            ),
        )

    generation = latest_generation + 1
    validated_at = datetime.now(timezone.utc)
    row_by_layer: dict[int, ReferenceLayerMirrorStrategy] = {}
    for assignment in plan.assignments:
        row = ReferenceLayerMirrorStrategy(
            provider_key=plan.provider_key,
            layer_id=assignment.layer_id,
            catalog_snapshot_id=plan.snapshot_id,
            catalog_definition_sha256=plan.catalog_definition_sha256,
            strategy=assignment.strategy,
            source_id=resolved_source_ids[assignment.layer_id],
            strategy_reason_code=assignment.reason_code,
            strategy_reason=assignment.reason[:_MAX_REASON_LENGTH],
            evidence_json=assignment.evidence,
            evidence_sha256=evidence_sha256_by_layer[assignment.layer_id],
            generation=generation,
            validated_at=validated_at,
        )
        db.add(row)
        row_by_layer[assignment.layer_id] = row
    db.flush()

    for assignment in plan.assignments:
        row = row_by_layer[assignment.layer_id]
        if assignment.strategy != "composition":
            continue
        for dependency_order, dependency_key in enumerate(
            assignment.dependency_source_keys
        ):
            dependency_layer = layers_by_source_key[dependency_key]
            dependency_strategy = row_by_layer.get(dependency_layer.id)
            if (
                dependency_strategy is None
                or dependency_strategy.strategy == "blocked"
            ):
                raise MirrorStrategyError(
                    f"composition dependency is blocked: {dependency_key}"
                )
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
        generation=generation,
        created_count=len(plan.assignments),
        unchanged_count=0,
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
    """Return only the latest complete generation for a catalog snapshot."""

    latest_generation = (
        select(func.max(ReferenceLayerMirrorStrategy.generation))
        .where(
            ReferenceLayerMirrorStrategy.provider_key == provider_key,
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot_id,
        )
        .scalar_subquery()
    )
    rows = list(
        db.scalars(
            select(ReferenceLayerMirrorStrategy)
            .where(
                ReferenceLayerMirrorStrategy.provider_key == provider_key,
                ReferenceLayerMirrorStrategy.catalog_snapshot_id
                == snapshot_id,
                ReferenceLayerMirrorStrategy.generation == latest_generation,
            )
            .order_by(ReferenceLayerMirrorStrategy.layer_id)
        )
    )
    if not rows:
        return {}
    if not _generation_is_complete(
        db,
        provider_key=provider_key,
        snapshot_id=snapshot_id,
        rows=rows,
    ):
        raise MirrorStrategyError(
            "current mirror strategy generation is incomplete"
        )
    return {row.layer_id: row for row in rows}


def mirror_strategy_plan_status(
    db: Session,
    plan: MirrorStrategyPlan,
) -> MirrorStrategyPlanStatus:
    """Compare the latest complete generation with the current code plan."""

    layers_by_source_key = {
        item.source_key: item
        for item in db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == plan.provider_key,
                ReferenceLayer.last_seen_snapshot_id == plan.snapshot_id,
                ReferenceLayer.node_type == "layer",
            )
            .order_by(ReferenceLayer.id)
        )
    }
    _validate_plan_assignments(plan, layers_by_source_key)
    latest_generation = int(
        db.scalar(
            select(func.max(ReferenceLayerMirrorStrategy.generation)).where(
                ReferenceLayerMirrorStrategy.provider_key
                == plan.provider_key,
                ReferenceLayerMirrorStrategy.catalog_snapshot_id
                == plan.snapshot_id,
            )
        )
        or 0
    )
    rows = (
        list(
            db.scalars(
                select(ReferenceLayerMirrorStrategy)
                .where(
                    ReferenceLayerMirrorStrategy.provider_key
                    == plan.provider_key,
                    ReferenceLayerMirrorStrategy.catalog_snapshot_id
                    == plan.snapshot_id,
                    ReferenceLayerMirrorStrategy.generation
                    == latest_generation,
                )
                .order_by(ReferenceLayerMirrorStrategy.layer_id)
            )
        )
        if latest_generation
        else []
    )
    complete = bool(rows) and _generation_is_complete(
        db,
        provider_key=plan.provider_key,
        snapshot_id=plan.snapshot_id,
        rows=rows,
    )
    source_ids_by_key = {
        (item.layer_id, item.source_key): item.id
        for item in db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == plan.provider_key,
            )
        )
    }
    source_ids = {
        item.layer_id: (
            source_ids_by_key.get((item.layer_id, item.source_key))
            if item.source_key is not None
            else None
        )
        for item in plan.assignments
    }
    evidence_sha256_by_layer = {
        item.layer_id: canonical_sha256(item.evidence)
        for item in plan.assignments
    }
    matches_plan = complete and _generation_matches_plan(
        db,
        rows=rows,
        plan=plan,
        layers_by_source_key=layers_by_source_key,
        source_ids=source_ids,
        evidence_sha256_by_layer=evidence_sha256_by_layer,
    )
    return MirrorStrategyPlanStatus(
        expected_plan_sha256=plan.plan_sha256,
        generation=latest_generation,
        row_count=len(rows),
        complete=complete,
        matches_plan=matches_plan,
    )


def _validate_plan_assignments(
    plan: MirrorStrategyPlan,
    layers_by_source_key: dict[str, ReferenceLayer],
) -> None:
    assignments = plan.assignments
    expected_layer_ids = {
        layer.id for layer in layers_by_source_key.values()
    }
    assignment_layer_ids = [item.layer_id for item in assignments]
    assignment_source_keys = [
        item.layer_source_key for item in assignments
    ]
    if (
        not assignments
        or len(set(assignment_layer_ids)) != len(assignment_layer_ids)
        or len(set(assignment_source_keys)) != len(assignment_source_keys)
        or set(assignment_layer_ids) != expected_layer_ids
        or set(assignment_source_keys) != set(layers_by_source_key)
    ):
        raise MirrorStrategyError(
            "strategy plan is not a complete catalog leaf assignment"
        )
    for assignment in assignments:
        layer = layers_by_source_key.get(assignment.layer_source_key)
        if layer is None or layer.id != assignment.layer_id:
            raise MirrorStrategyError(
                "strategy assignment layer identity is invalid"
            )
        if (
            assignment.strategy not in STRATEGIES
            or not assignment.reason_code
            or len(assignment.reason_code) > 64
            or not isinstance(assignment.reason, str)
            or not isinstance(assignment.evidence, dict)
        ):
            raise MirrorStrategyError("strategy assignment shape is invalid")
        if assignment.strategy in {"vector", "raster", "tiles"}:
            valid_shape = (
                bool(assignment.source_key)
                and not assignment.dependency_source_keys
            )
        elif assignment.strategy == "composition":
            valid_shape = (
                assignment.source_key is None
                and bool(assignment.dependency_source_keys)
                and len(set(assignment.dependency_source_keys))
                == len(assignment.dependency_source_keys)
                and all(
                    dependency_key in layers_by_source_key
                    and dependency_key != assignment.layer_source_key
                    for dependency_key in assignment.dependency_source_keys
                )
                and assignment.evidence.get("dependencies")
                == list(assignment.dependency_source_keys)
            )
        else:
            valid_shape = (
                assignment.source_key is None
                and not assignment.dependency_source_keys
            )
        if not valid_shape:
            raise MirrorStrategyError("strategy assignment shape is invalid")


def _generation_matches_plan(
    db: Session,
    *,
    rows: list[ReferenceLayerMirrorStrategy],
    plan: MirrorStrategyPlan,
    layers_by_source_key: dict[str, ReferenceLayer],
    source_ids: dict[int, int | None],
    evidence_sha256_by_layer: dict[int, str],
) -> bool:
    rows_by_layer = {row.layer_id: row for row in rows}
    if (
        len(rows_by_layer) != len(rows)
        or set(rows_by_layer)
        != {assignment.layer_id for assignment in plan.assignments}
    ):
        return False
    dependencies = _dependencies_by_strategy(db, rows)
    for assignment in plan.assignments:
        row = rows_by_layer[assignment.layer_id]
        if not _row_matches(
            row,
            assignment,
            source_ids[assignment.layer_id],
            evidence_sha256_by_layer[assignment.layer_id],
            plan.catalog_definition_sha256,
        ):
            return False
        actual = dependencies.get(row.id, ())
        expected = (
            tuple(
                layers_by_source_key[key].id
                for key in assignment.dependency_source_keys
            )
            if assignment.strategy == "composition"
            else ()
        )
        if (
            tuple(item.dependency_layer_id for item in actual) != expected
            or tuple(item.dependency_order for item in actual)
            != tuple(range(len(expected)))
            or any(
                item.strategy_layer_id != assignment.layer_id
                for item in actual
            )
        ):
            return False
    return True


def _generation_is_complete(
    db: Session,
    *,
    provider_key: str,
    snapshot_id: int,
    rows: list[ReferenceLayerMirrorStrategy],
) -> bool:
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == provider_key,
            ReferenceCatalogSnapshot.id == snapshot_id,
        )
    )
    layers = list(
        db.scalars(
            select(ReferenceLayer).where(
                ReferenceLayer.provider_key == provider_key,
                ReferenceLayer.last_seen_snapshot_id == snapshot_id,
                ReferenceLayer.node_type == "layer",
            )
        )
    )
    rows_by_layer = {row.layer_id: row for row in rows}
    generations = {row.generation for row in rows}
    if (
        snapshot is None
        or not rows
        or len(rows_by_layer) != len(rows)
        or len(generations) != 1
        or set(rows_by_layer) != {layer.id for layer in layers}
        or any(
            row.provider_key != provider_key
            or row.catalog_snapshot_id != snapshot_id
            or row.catalog_definition_sha256 != snapshot.definition_sha256
            or not _stored_evidence_is_valid(row)
            for row in rows
        )
    ):
        return False

    layers_by_source_key = {layer.source_key: layer for layer in layers}
    dependencies = _dependencies_by_strategy(db, rows)
    graph: dict[int, tuple[int, ...]] = {}
    for row in rows:
        actual = dependencies.get(row.id, ())
        if any(item.strategy_layer_id != row.layer_id for item in actual):
            return False
        actual_ids = tuple(item.dependency_layer_id for item in actual)
        if tuple(item.dependency_order for item in actual) != tuple(
            range(len(actual))
        ):
            return False
        if row.strategy != "composition":
            if actual:
                return False
            graph[row.layer_id] = ()
            continue
        raw_dependencies = (
            row.evidence_json.get("dependencies")
            if isinstance(row.evidence_json, dict)
            else None
        )
        if (
            not isinstance(raw_dependencies, list)
            or not raw_dependencies
            or any(
                not isinstance(item, str)
                or item not in layers_by_source_key
                for item in raw_dependencies
            )
        ):
            return False
        expected_ids = tuple(
            layers_by_source_key[item].id for item in raw_dependencies
        )
        if (
            actual_ids != expected_ids
            or any(
                dependency_id not in rows_by_layer
                for dependency_id in actual_ids
            )
            or any(
                rows_by_layer[dependency_id].strategy == "blocked"
                for dependency_id in actual_ids
            )
        ):
            return False
        graph[row.layer_id] = actual_ids
    return not _dependency_graph_has_cycle(graph)


def _dependencies_by_strategy(
    db: Session,
    rows: list[ReferenceLayerMirrorStrategy],
) -> dict[int, tuple[ReferenceLayerMirrorStrategyDependency, ...]]:
    if not rows:
        return {}
    grouped: dict[int, list[ReferenceLayerMirrorStrategyDependency]] = {}
    for dependency in db.scalars(
        select(ReferenceLayerMirrorStrategyDependency)
        .where(
            ReferenceLayerMirrorStrategyDependency.provider_key
            == rows[0].provider_key,
            ReferenceLayerMirrorStrategyDependency.strategy_id.in_(
                [row.id for row in rows]
            ),
        )
        .order_by(
            ReferenceLayerMirrorStrategyDependency.strategy_id,
            ReferenceLayerMirrorStrategyDependency.dependency_order,
            ReferenceLayerMirrorStrategyDependency.id,
        )
    ):
        grouped.setdefault(dependency.strategy_id, []).append(dependency)
    return {key: tuple(value) for key, value in grouped.items()}


def _dependency_graph_has_cycle(
    graph: dict[int, tuple[int, ...]],
) -> bool:
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(layer_id: int) -> bool:
        if layer_id in visiting:
            return True
        if layer_id in visited:
            return False
        visiting.add(layer_id)
        if any(visit(dependency_id) for dependency_id in graph[layer_id]):
            return True
        visiting.remove(layer_id)
        visited.add(layer_id)
        return False

    return any(visit(layer_id) for layer_id in graph)


def _stored_evidence_is_valid(
    row: ReferenceLayerMirrorStrategy,
) -> bool:
    try:
        return canonical_sha256(row.evidence_json) == row.evidence_sha256
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


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
        service_definition = _service_definition(service)
        layer_definition = _layer_definition(layer)
        candidates = acquisition_candidates(
            service_definition,
            layer_definition,
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
        substitution = reviewed_ign_ortho_catalog_projection(
            service_definition.base_url,
            layer_definition.remote_name or "",
        )
        if (
            substitution is not None
            and substitution["promotion_eligible"] is False
        ):
            return _blocked(
                layer,
                "reviewed_ortho_substitution_blocked",
                "reviewed IGN ortho mapping is partial or semantically "
                "different and cannot become a local source",
                {"ortho_substitution": substitution},
            )
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
            {
                "source_key": selected.source_key,
                "target_kind": selected.target_kind,
            },
        )
    substitution = reviewed_ign_ortho_public_projection(selected.config)
    reason_code = "candidate_selected"
    reason = "highest-priority safe local acquisition candidate selected"
    evidence = {
        "source_key": selected.source_key,
        "protocol": selected.protocol,
        "target_kind": selected.target_kind,
        "priority": selected.priority,
    }
    if substitution is not None:
        evidence["ortho_substitution"] = substitution
        if substitution["equivalence_status"] == "substitute_degraded":
            reason_code = "candidate_substitute_degraded"
            reason = (
                "reviewed official ortho substitute selected as an "
                "explicitly degraded local delivery"
            )
        else:
            reason_code = "candidate_exact_official_substitution"
            reason = (
                "reviewed official PNOA substitution selected after both "
                "capabilities declarations matched"
            )
    return StrategyAssignment(
        layer_id=layer.id,
        layer_source_key=layer.source_key,
        strategy=strategy,
        source_key=selected.source_key,
        dependency_source_keys=(),
        reason_code=reason_code,
        reason=reason,
        evidence=evidence,
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
        and row.evidence_json == assignment.evidence
        and row.evidence_sha256 == evidence_sha256
        and row.catalog_definition_sha256 == catalog_definition_sha256
    )


def _current_snapshot(
    db: Session,
    provider_key: str,
    *,
    for_update: bool = False,
) -> ReferenceCatalogSnapshot:
    query = select(ReferenceCatalogSnapshot).where(
        ReferenceCatalogSnapshot.provider_key == provider_key,
        ReferenceCatalogSnapshot.is_current.is_(True),
        ReferenceCatalogSnapshot.status == "applied",
    )
    if for_update:
        query = query.with_for_update()
    snapshot = db.scalar(query)
    if snapshot is None:
        raise MirrorStrategyError("current applied catalog is unavailable")
    return snapshot


__all__ = [
    "AppliedMirrorStrategyPlan",
    "MirrorStrategyError",
    "MirrorStrategyPlan",
    "MirrorStrategyPlanStatus",
    "STRATEGIES",
    "StrategyAssignment",
    "apply_mirror_strategy_plan",
    "build_mirror_strategy_plan",
    "canonical_sha256",
    "current_mirror_strategies",
    "mirror_strategy_plan_status",
]
