"""Hash-gated reconciliation of mirror sources and strategies only.

Unlike the scheduler, this entry point never runs watchers, enqueues work,
downloads data or performs network I/O.  Its default mode is a read-only plan.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.mirror_lifecycle import (
    AppliedMirrorBootstrap,
    MirrorBootstrapPlan,
    MirrorLifecycleError,
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
)
from app.reference_layers.mirror_strategy import (
    MirrorStrategyError,
    MirrorStrategyPlan,
    MirrorStrategyPlanStatus,
    build_mirror_strategy_plan,
    canonical_sha256,
    mirror_strategy_plan_status,
)


RECONCILIATION_PLAN_SCHEMA = "siur-mirror-reconciliation-plan/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MirrorReconciliationError(RuntimeError):
    """A reconcile-only operator transition cannot proceed safely."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MirrorReconciliationPlan:
    """Exact source and strategy transition presented to the operator."""

    source_plan: MirrorBootstrapPlan
    strategy_plan: MirrorStrategyPlan
    strategy_before: MirrorStrategyPlanStatus

    def semantic_document(self) -> dict[str, Any]:
        return {
            "schema": RECONCILIATION_PLAN_SCHEMA,
            "provider_key": self.source_plan.provider_key,
            "catalog_snapshot_id": self.source_plan.snapshot_id,
            "catalog_definition_sha256": (
                self.source_plan.catalog_definition_sha256
            ),
            "source_plan_sha256": self.source_plan.plan_sha256,
            "strategy_plan_sha256": self.strategy_plan.plan_sha256,
        }

    @property
    def plan_sha256(self) -> str:
        return canonical_sha256(self.semantic_document())

    def public_summary(
        self,
        *,
        applied: AppliedMirrorBootstrap | None = None,
        strategy_after: MirrorStrategyPlanStatus | None = None,
    ) -> dict[str, Any]:
        strategy_counts = Counter(
            item.strategy for item in self.strategy_plan.assignments
        )
        source_drift = any(
            (
                self.source_plan.new_source_keys,
                self.source_plan.updated_source_keys,
                self.source_plan.deactivated_source_keys,
            )
        )
        observed_status = strategy_after or self.strategy_before
        ready = (
            observed_status.complete
            and observed_status.matches_plan
            and (applied is not None or not source_drift)
        )
        changed = bool(
            applied is not None
            and any(
                (
                    applied.created_count,
                    applied.updated_count,
                    applied.deactivated_count,
                    applied.strategy_created_count,
                )
            )
        )
        return {
            "ok": True,
            "mode": "apply" if applied is not None else "dry-run",
            "applied": applied is not None,
            "changed": changed,
            "ready": ready,
            "drift_detected": not ready,
            "plan_sha256": self.plan_sha256,
            **self.semantic_document(),
            "sources": {
                "planned_count": len(self.source_plan.sources),
                "new_count": len(self.source_plan.new_source_keys),
                "new": list(self.source_plan.new_source_keys),
                "updated_count": len(self.source_plan.updated_source_keys),
                "updated": list(self.source_plan.updated_source_keys),
                "deactivated_count": len(
                    self.source_plan.deactivated_source_keys
                ),
                "deactivated": list(
                    self.source_plan.deactivated_source_keys
                ),
                "unchanged_count": self.source_plan.unchanged_count,
                "apply_result": (
                    {
                        "created_count": applied.created_count,
                        "updated_count": applied.updated_count,
                        "deactivated_count": applied.deactivated_count,
                    }
                    if applied is not None
                    else None
                ),
            },
            "strategies": {
                "expected_assignment_count": len(
                    self.strategy_plan.assignments
                ),
                "expected_counts": {
                    key: strategy_counts[key]
                    for key in sorted(strategy_counts)
                },
                "before": _status_summary(self.strategy_before),
                "apply_result": (
                    {
                        "generation": applied.strategy_generation,
                        "created_count": applied.strategy_created_count,
                        "unchanged_count": (
                            applied.strategy_unchanged_count
                        ),
                        "blocked_count": applied.strategy_blocked_count,
                        "plan_sha256": applied.strategy_plan_sha256,
                    }
                    if applied is not None
                    else None
                ),
                "after": (
                    _status_summary(strategy_after)
                    if strategy_after is not None
                    else None
                ),
            },
            "safety": {
                "network_performed": False,
                "watchers_run": False,
                "runs_enqueued": False,
                "downloads_started": False,
                "authorization_created": False,
                "delivery_mutation_performed": False,
            },
        }


def build_mirror_reconciliation_plan(
    db: Session,
    *,
    provider_key: str = "siur",
) -> MirrorReconciliationPlan:
    """Build an exact read-only source and strategy reconciliation plan."""

    source_plan = build_mirror_bootstrap_plan(
        db,
        provider_key=provider_key,
    )
    strategy_plan = build_mirror_strategy_plan(
        db,
        provider_key=provider_key,
    )
    if (
        source_plan.provider_key != strategy_plan.provider_key
        or source_plan.snapshot_id != strategy_plan.snapshot_id
        or source_plan.catalog_definition_sha256
        != strategy_plan.catalog_definition_sha256
    ):
        raise MirrorReconciliationError(
            "source and strategy plans do not describe the same catalog",
            code="plan_identity_mismatch",
        )
    return MirrorReconciliationPlan(
        source_plan=source_plan,
        strategy_plan=strategy_plan,
        strategy_before=mirror_strategy_plan_status(db, strategy_plan),
    )


def apply_mirror_reconciliation_plan(
    db: Session,
    reviewed_plan: MirrorReconciliationPlan,
    *,
    expected_plan_sha256: str,
) -> tuple[AppliedMirrorBootstrap, MirrorStrategyPlanStatus]:
    """Apply only the reviewed transition and verify it before commit."""

    if (
        _SHA256_RE.fullmatch(expected_plan_sha256) is None
        or expected_plan_sha256 != reviewed_plan.plan_sha256
    ):
        raise MirrorReconciliationError(
            "mirror reconciliation plan changed after operator review",
            code="plan_changed",
        )
    try:
        applied = apply_mirror_bootstrap_plan(
            db,
            reviewed_plan.source_plan,
            expected_strategy_plan_sha256=(
                reviewed_plan.strategy_plan.plan_sha256
            ),
            commit=False,
        )
        current_plan = build_mirror_strategy_plan(
            db,
            provider_key=reviewed_plan.source_plan.provider_key,
        )
        if current_plan.plan_sha256 != reviewed_plan.strategy_plan.plan_sha256:
            raise MirrorReconciliationError(
                "mirror strategy plan changed during reconciliation",
                code="plan_changed",
            )
        status = mirror_strategy_plan_status(db, current_plan)
        if not status.complete or not status.matches_plan:
            raise MirrorReconciliationError(
                "persisted mirror strategy did not match the reviewed plan",
                code="postcondition_failed",
            )
        db.commit()
        return applied, status
    except Exception:
        db.rollback()
        raise


def _status_summary(status: MirrorStrategyPlanStatus) -> dict[str, Any]:
    return {
        "generation": status.generation,
        "row_count": status.row_count,
        "complete": status.complete,
        "expected_plan_sha256": status.expected_plan_sha256,
        "matches_plan": status.matches_plan,
        "drift_detected": not status.matches_plan,
        "ready": status.complete and status.matches_plan,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or apply local mirror source/strategy reconciliation "
            "without watchers, enqueueing or network access"
        )
    )
    parser.add_argument("--provider-key", default="siur")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.apply and not arguments.expected_plan_sha256:
            raise MirrorReconciliationError(
                "--apply requires --expected-plan-sha256",
                code="operator_confirmation_missing",
            )
        if (
            not arguments.apply
            and arguments.expected_plan_sha256 is not None
        ):
            raise MirrorReconciliationError(
                "--expected-plan-sha256 is only valid with --apply",
                code="operator_input_invalid",
            )
        register_all_models()
        with SessionLocal() as db:
            plan = build_mirror_reconciliation_plan(
                db,
                provider_key=arguments.provider_key,
            )
            if arguments.apply:
                applied, strategy_after = (
                    apply_mirror_reconciliation_plan(
                        db,
                        plan,
                        expected_plan_sha256=(
                            arguments.expected_plan_sha256
                        ),
                    )
                )
                result = plan.public_summary(
                    applied=applied,
                    strategy_after=strategy_after,
                )
            else:
                result = plan.public_summary()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (
        MirrorLifecycleError,
        MirrorReconciliationError,
        MirrorStrategyError,
    ) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": getattr(
                        error,
                        "code",
                        "mirror_reconciliation_rejected",
                    ),
                    "error_summary": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except Exception as error:  # noqa: BLE001  # pragma: no cover
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "mirror_reconciliation_unavailable",
                    "error_summary": type(error).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())


__all__ = [
    "MirrorReconciliationError",
    "MirrorReconciliationPlan",
    "apply_mirror_reconciliation_plan",
    "build_mirror_reconciliation_plan",
    "main",
]
