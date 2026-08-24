"""Deterministic, non-applicable review inventory for SIUR mirror sources.

This module deliberately does not create ``siur-mirror-authorization-v1``
documents.  It presents exact technical facts and leaves every human/legal
field undecided, so an inventory can never be mistaken for an authorization.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationDocumentError,
    MirrorAuthorizationError,
    _require_permissions,
    _reviewed_license_binding,
    _url_values,
    _validate_review_source,
    authorization_chain_is_valid,
    url_origin,
)
from app.reference_layers.mirror_lifecycle import (
    MirrorLifecycleError,
    PlannedMirrorSource,
    stored_source_definition_is_valid,
)
from app.reference_layers.mirror_reconcile import (
    MirrorReconciliationError,
    MirrorReconciliationPlan,
    build_mirror_reconciliation_plan,
)
from app.reference_layers.mirror_strategy import (
    MirrorStrategyError,
    StrategyAssignment,
    canonical_sha256,
    current_mirror_strategies,
)
from app.reference_layers.models import (
    ReferenceLayer,
    ReferenceLayerMirrorStrategy,
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceService,
)


SCHEMA_VERSION = "siur-mirror-review-packet-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$", re.ASCII)
_MAX_SOURCES = 10_000


class MirrorReviewPacketError(RuntimeError):
    """Stable fail-closed error at the operator boundary."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MirrorReviewPacket:
    """One exact technical inventory, excluding its self hash."""

    semantic_document: dict[str, Any]
    packet_sha256: str

    def public_document(self) -> dict[str, Any]:
        return {
            **self.semantic_document,
            "packet_sha256": self.packet_sha256,
        }


@dataclass(frozen=True)
class _PacketState:
    reconciliation: MirrorReconciliationPlan
    layers: dict[int, ReferenceLayer]
    services: dict[int, ReferenceService]
    sources: dict[tuple[int, str], ReferenceLayerSource]
    strategy_rows: dict[int, ReferenceLayerMirrorStrategy]
    authorization_chains: dict[
        int,
        tuple[ReferenceMirrorAuthorizationReview, ...],
    ]
    fence: dict[str, Any]


def build_mirror_review_packet(
    db: Session,
    *,
    provider_key: str = "siur",
) -> MirrorReviewPacket:
    """Build a stable, read-only packet and observe its fence twice."""

    _validate_provider_key(provider_key)
    if db.new or db.dirty or db.deleted:
        raise MirrorReviewPacketError(
            "review packet requires a clean read-only session",
            code="mirror_review_packet_session_not_clean",
        )
    try:
        with db.no_autoflush:
            first = _load_packet_state(db, provider_key=provider_key)
            second = _load_packet_state(db, provider_key=provider_key)
    except MirrorReviewPacketError:
        raise
    except (
        MirrorAuthorizationDocumentError,
        MirrorLifecycleError,
        MirrorReconciliationError,
        MirrorStrategyError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise MirrorReviewPacketError(
            "current mirror state cannot produce a review packet",
            code="mirror_review_packet_not_ready",
        ) from error
    if first.fence != second.fence:
        raise MirrorReviewPacketError(
            "mirror state changed while the review packet was built",
            code="mirror_review_packet_drift",
        )
    semantic = _semantic_document(first)
    packet_sha256 = canonical_sha256(semantic)
    return MirrorReviewPacket(
        semantic_document=semantic,
        packet_sha256=packet_sha256,
    )


def require_expected_mirror_review_packet(
    db: Session,
    *,
    provider_key: str,
    expected_packet_sha256: str,
) -> MirrorReviewPacket:
    """Rebuild and match an exact packet without persisting anything."""

    if _SHA256_RE.fullmatch(expected_packet_sha256) is None:
        raise MirrorReviewPacketError(
            "expected packet hash is invalid",
            code="mirror_review_packet_invalid_request",
        )
    packet = build_mirror_review_packet(db, provider_key=provider_key)
    if packet.packet_sha256 != expected_packet_sha256:
        raise MirrorReviewPacketError(
            "review packet changed after human inspection",
            code="mirror_review_packet_changed",
        )
    return packet


def _load_packet_state(
    db: Session,
    *,
    provider_key: str,
) -> _PacketState:
    reconciliation = build_mirror_reconciliation_plan(
        db,
        provider_key=provider_key,
    )
    _require_reconciled_state(reconciliation)
    source_plan = reconciliation.source_plan
    if not 0 <= len(source_plan.sources) <= _MAX_SOURCES:
        raise MirrorReviewPacketError(
            "review packet source count is outside its bound",
            code="mirror_review_packet_not_ready",
        )

    layers = {
        layer.id: layer
        for layer in db.scalars(
            select(ReferenceLayer)
            .where(
                ReferenceLayer.provider_key == provider_key,
                ReferenceLayer.last_seen_snapshot_id
                == source_plan.snapshot_id,
                ReferenceLayer.node_type == "layer",
            )
            .order_by(ReferenceLayer.id)
        )
    }
    expected_layer_ids = {
        item.layer_id
        for item in reconciliation.strategy_plan.assignments
    }
    if (
        set(layers) != expected_layer_ids
        or any(layer.service_id is None for layer in layers.values())
    ):
        raise MirrorReviewPacketError(
            "current leaf layers do not match the strategy plan",
            code="mirror_review_packet_not_ready",
        )
    service_ids = {
        layer.service_id
        for layer in layers.values()
        if layer.service_id is not None
    }
    services = {
        service.id: service
        for service in db.scalars(
            select(ReferenceService)
            .where(
                ReferenceService.provider_key == provider_key,
                ReferenceService.last_seen_snapshot_id
                == source_plan.snapshot_id,
                ReferenceService.id.in_(service_ids),
            )
            .order_by(ReferenceService.id)
        )
    }
    if set(services) != service_ids:
        raise MirrorReviewPacketError(
            "current layer services are incomplete",
            code="mirror_review_packet_not_ready",
        )

    planned_by_key = {
        (item.layer_id, item.source_key): item
        for item in source_plan.sources
    }
    if len(planned_by_key) != len(source_plan.sources):
        raise MirrorReviewPacketError(
            "planned mirror sources are not unique",
            code="mirror_review_packet_not_ready",
        )
    persisted = list(
        db.scalars(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.provider_key == provider_key)
            .order_by(
                ReferenceLayerSource.layer_id,
                ReferenceLayerSource.priority,
                ReferenceLayerSource.source_key,
                ReferenceLayerSource.id,
            )
        )
    )
    persisted_by_key = {
        (source.layer_id, source.source_key): source
        for source in persisted
        if (source.layer_id, source.source_key) in planned_by_key
    }
    if (
        len(persisted_by_key) != len(planned_by_key)
        or set(persisted_by_key) != set(planned_by_key)
    ):
        raise MirrorReviewPacketError(
            "persisted mirror sources do not match the reviewed plan",
            code="mirror_review_packet_not_ready",
        )
    for key, source in persisted_by_key.items():
        if (
            not stored_source_definition_is_valid(source)
            or not _source_matches_planned(source, planned_by_key[key])
        ):
            raise MirrorReviewPacketError(
                "persisted mirror source evidence is invalid",
                code="mirror_review_packet_not_ready",
            )

    strategy_rows = current_mirror_strategies(
        db,
        provider_key=provider_key,
        snapshot_id=source_plan.snapshot_id,
    )
    if set(strategy_rows) != expected_layer_ids:
        raise MirrorReviewPacketError(
            "current strategy generation is incomplete",
            code="mirror_review_packet_not_ready",
        )
    _validate_selected_sources(
        reconciliation.strategy_plan.assignments,
        strategy_rows=strategy_rows,
        sources=persisted_by_key,
    )

    authorization_chains = _load_authorization_chains(
        db,
        source_ids={source.id for source in persisted_by_key.values()},
    )
    for source in persisted_by_key.values():
        chain = authorization_chains.get(source.id, ())
        if chain and not authorization_chain_is_valid(chain):
            raise MirrorReviewPacketError(
                "stored mirror authorization chain is invalid",
                code="mirror_review_packet_not_ready",
            )

    fence = _fence_document(
        reconciliation,
        layers=layers,
        services=services,
        sources=persisted_by_key,
        strategy_rows=strategy_rows,
        authorization_chains=authorization_chains,
    )
    return _PacketState(
        reconciliation=reconciliation,
        layers=layers,
        services=services,
        sources=persisted_by_key,
        strategy_rows=strategy_rows,
        authorization_chains=authorization_chains,
        fence=fence,
    )


def _require_reconciled_state(
    reconciliation: MirrorReconciliationPlan,
) -> None:
    source_plan = reconciliation.source_plan
    source_drift = any(
        (
            source_plan.new_source_keys,
            source_plan.updated_source_keys,
            source_plan.deactivated_source_keys,
        )
    )
    if (
        source_drift
        or source_plan.unchanged_count != len(source_plan.sources)
        or not reconciliation.strategy_before.complete
        or not reconciliation.strategy_before.matches_plan
        or reconciliation.strategy_before.generation <= 0
    ):
        raise MirrorReviewPacketError(
            "mirror sources and strategies must be reconciled first",
            code="mirror_review_packet_not_ready",
        )


def _validate_selected_sources(
    assignments: Sequence[StrategyAssignment],
    *,
    strategy_rows: Mapping[int, ReferenceLayerMirrorStrategy],
    sources: Mapping[tuple[int, str], ReferenceLayerSource],
) -> None:
    for assignment in assignments:
        row = strategy_rows[assignment.layer_id]
        if assignment.strategy in {"vector", "raster", "tiles"}:
            if assignment.source_key is None:
                raise MirrorReviewPacketError(
                    "direct strategy has no selected source",
                    code="mirror_review_packet_not_ready",
                )
            source = sources.get(
                (assignment.layer_id, assignment.source_key)
            )
            if source is None or row.source_id != source.id:
                raise MirrorReviewPacketError(
                    "strategy selected source does not match persisted state",
                    code="mirror_review_packet_not_ready",
                )
        elif assignment.strategy in {"composition", "blocked"}:
            if row.source_id is not None:
                raise MirrorReviewPacketError(
                    "source-free strategy unexpectedly references a source",
                    code="mirror_review_packet_not_ready",
                )
        else:
            raise MirrorReviewPacketError(
                "strategy kind is unsupported by the review packet",
                code="mirror_review_packet_not_ready",
            )


def _load_authorization_chains(
    db: Session,
    *,
    source_ids: set[int],
) -> dict[int, tuple[ReferenceMirrorAuthorizationReview, ...]]:
    if not source_ids:
        return {}
    grouped: dict[
        int,
        list[ReferenceMirrorAuthorizationReview],
    ] = defaultdict(list)
    for review in db.scalars(
        select(ReferenceMirrorAuthorizationReview)
        .where(
            ReferenceMirrorAuthorizationReview.source_id.in_(source_ids)
        )
        .order_by(
            ReferenceMirrorAuthorizationReview.source_id,
            ReferenceMirrorAuthorizationReview.reviewed_at,
            ReferenceMirrorAuthorizationReview.id,
        )
    ):
        if review.source_id not in source_ids:
            raise MirrorReviewPacketError(
                "authorization review escaped the requested source set",
                code="mirror_review_packet_not_ready",
            )
        grouped[review.source_id].append(review)
    return {
        source_id: tuple(reviews)
        for source_id, reviews in grouped.items()
    }


def _fence_document(
    reconciliation: MirrorReconciliationPlan,
    *,
    layers: Mapping[int, ReferenceLayer],
    services: Mapping[int, ReferenceService],
    sources: Mapping[tuple[int, str], ReferenceLayerSource],
    strategy_rows: Mapping[int, ReferenceLayerMirrorStrategy],
    authorization_chains: Mapping[
        int,
        tuple[ReferenceMirrorAuthorizationReview, ...],
    ],
) -> dict[str, Any]:
    ordered_sources = sorted(
        sources.values(),
        key=lambda item: (
            item.layer_id,
            item.priority,
            item.source_key,
            item.id,
        ),
    )
    return {
        "catalog_snapshot_id": reconciliation.source_plan.snapshot_id,
        "catalog_definition_sha256": (
            reconciliation.source_plan.catalog_definition_sha256
        ),
        "source_definitions_sha256": canonical_sha256(
            [_source_record_identity(source) for source in ordered_sources]
        ),
        "catalog_projection_sha256": canonical_sha256(
            {
                "services": [
                    _service_projection(services[item])
                    for item in sorted(services)
                ],
                "layers": [
                    _layer_projection(layers[item])
                    for item in sorted(layers)
                ],
            }
        ),
        "strategy_plan_sha256": (
            reconciliation.strategy_plan.plan_sha256
        ),
        "strategy_generation": (
            reconciliation.strategy_before.generation
        ),
        "strategy_generation_sha256": canonical_sha256(
            [
                _strategy_row_projection(strategy_rows[item])
                for item in sorted(strategy_rows)
            ]
        ),
        "authorization_heads_sha256": canonical_sha256(
            [
                _authorization_head_fence(
                    source,
                    authorization_chains.get(source.id, ()),
                )
                for source in ordered_sources
            ]
        ),
    }


def _semantic_document(state: _PacketState) -> dict[str, Any]:
    assignments = {
        item.layer_id: item
        for item in state.reconciliation.strategy_plan.assignments
    }
    selected_keys = {
        (item.layer_id, item.source_key)
        for item in assignments.values()
        if item.strategy in {"vector", "raster", "tiles"}
        and item.source_key is not None
    }
    primary: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    ordered_sources = sorted(
        state.sources.items(),
        key=lambda item: (
            item[1].layer_id,
            item[1].priority,
            item[1].source_key,
            item[1].id,
        ),
    )
    for key, source in ordered_sources:
        assignment = assignments[source.layer_id]
        layer = state.layers[source.layer_id]
        if layer.service_id is None:
            raise MirrorReviewPacketError(
                "reviewable source layer has no service",
                code="mirror_review_packet_not_ready",
            )
        role = "primary" if key in selected_keys else "fallback"
        entry = _source_review_entry(
            source,
            role=role,
            layer=layer,
            service=state.services[layer.service_id],
            assignment=assignment,
            chain=state.authorization_chains.get(source.id, ()),
        )
        if role == "primary":
            primary.append(entry)
        else:
            fallback.append(entry)

    blocked: list[dict[str, Any]] = []
    compositions: list[dict[str, Any]] = []
    for assignment in state.reconciliation.strategy_plan.assignments:
        layer = state.layers[assignment.layer_id]
        row = state.strategy_rows[assignment.layer_id]
        item = {
            "layer": _layer_projection(layer),
            "strategy": assignment.strategy,
            "reason_code": assignment.reason_code,
            "reason": assignment.reason,
            "evidence": assignment.evidence,
            "evidence_sha256": row.evidence_sha256,
        }
        if assignment.strategy == "blocked":
            blocked.append(item)
        elif assignment.strategy == "composition":
            item["dependency_source_keys"] = list(
                assignment.dependency_source_keys
            )
            compositions.append(item)

    primary_pending = sum(
        item["current_authorization"]["blocker"] is not None
        for item in primary
    )
    fallback_pending = sum(
        item["current_authorization"]["blocker"] is not None
        for item in fallback
    )
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "applicable_as_authorization": False,
        "provider_key": state.reconciliation.source_plan.provider_key,
        "fence": state.fence,
        "summary": {
            "leaf_layer_count": len(state.layers),
            "primary_source_count": len(primary),
            "fallback_source_count": len(fallback),
            "blocked_layer_count": len(blocked),
            "composition_layer_count": len(compositions),
            "pending_primary_review_count": primary_pending,
            "pending_fallback_review_count": fallback_pending,
        },
        "review_questions": [
            (
                "¿Las condiciones del conjunto exacto permiten consultar "
                "metadatos y descargarlo mediante este origen?"
            ),
            (
                "¿Permiten conservar el resultado transformado en nuestros "
                "servidores y servirlo localmente a los usuarios previstos?"
            ),
            (
                "¿La atribución, la fecha y los términos reproducidos son "
                "exactos para este conjunto?"
            ),
            (
                "Para teselas: ¿se permite una descarga masiva controlada "
                "para construir la pirámide local?"
            ),
            (
                "¿Los permisos decididos son independientes y no amplían "
                "un permiso parcial?"
            ),
        ],
        "primary_sources": primary,
        "fallback_sources": fallback,
        "blocked_layers": blocked,
        "composition_layers": compositions,
        "safety": {
            "database_writes_performed": False,
            "network_performed": False,
            "authorization_created": False,
            "runs_enqueued": False,
            "downloads_started": False,
            "human_fields_are_undecided": True,
        },
    }


def _source_review_entry(
    source: ReferenceLayerSource,
    *,
    role: str,
    layer: ReferenceLayer,
    service: ReferenceService,
    assignment: StrategyAssignment,
    chain: tuple[ReferenceMirrorAuthorizationReview, ...],
) -> dict[str, Any]:
    try:
        origins = tuple(
            sorted(
                {
                    url_origin(value)
                    for value in (
                        source.endpoint_url,
                        *_url_values(source.config_json),
                    )
                    if value is not None
                }
            )
        )
        if source.endpoint_url is None or not 1 <= len(origins) <= 32:
            raise MirrorAuthorizationDocumentError(
                "source has no bounded HTTPS origin set"
            )
        canonical_origin = url_origin(source.endpoint_url)
        reviewed_license = _reviewed_license_binding(source)
    except MirrorAuthorizationDocumentError as error:
        raise MirrorReviewPacketError(
            "source cannot be represented by an exact authorization",
            code="mirror_review_packet_not_ready",
        ) from error

    head = chain[-1] if chain else None
    blocker = _authorization_blocker(source, chain)
    technical_license = (
        None
        if reviewed_license is None
        else {
            "name": reviewed_license[0],
            "url": reviewed_license[1],
            "required_attribution": reviewed_license[2],
            "grants_permission": False,
        }
    )
    return {
        "review_role": role,
        "needed_for": (
            "initial_delivery"
            if role == "primary"
            else "automatic_fallback"
        ),
        "layer": _layer_projection(layer),
        "service": _service_projection(service),
        "layer_strategy": {
            "kind": assignment.strategy,
            "reason_code": assignment.reason_code,
            "reason": assignment.reason,
            "evidence": assignment.evidence,
            "evidence_sha256": canonical_sha256(
                assignment.evidence
            ),
        },
        "source": {
            **_source_record_identity(source),
            "canonical_origin": canonical_origin,
            "required_source_origins": list(origins),
        },
        "technical_license_evidence": technical_license,
        "current_authorization": {
            "status": "authorized" if blocker is None else "blocked",
            "blocker": blocker,
            "head": _authorization_head_projection(head),
            "required_supersedes_review_sha256": (
                head.review_sha256 if head is not None else None
            ),
        },
        "human_review": _empty_human_review(),
    }


def _authorization_blocker(
    source: ReferenceLayerSource,
    chain: tuple[ReferenceMirrorAuthorizationReview, ...],
) -> str | None:
    if not chain:
        return "mirror_authorization_missing"
    current = chain[-1]
    try:
        _validate_review_source(current, source)
        _require_permissions(
            current,
            source=source,
            acquisition=True,
        )
    except MirrorAuthorizationError as error:
        return error.code
    return None


def _empty_human_review() -> dict[str, Any]:
    return {
        "state": "undecided",
        "decision": None,
        "reviewer": None,
        "reviewed_at": None,
        "allowed_origins": None,
        "license": {
            "name": None,
            "url": None,
            "terms": None,
            "attribution": None,
        },
        "permissions": {
            "metadata_probe": None,
            "dataset_download": None,
            "local_storage": None,
            "local_service": None,
            "bulk_tile_seed": None,
        },
    }


def _source_record_identity(
    source: ReferenceLayerSource,
) -> dict[str, Any]:
    return {
        "source_id": source.id,
        "layer_id": source.layer_id,
        "source_key": source.source_key,
        "source_definition_sha256": source.definition_sha256,
        "definition": {
            "protocol": source.protocol,
            "target_kind": source.target_kind,
            "endpoint_url": source.endpoint_url,
            "remote_name": source.remote_name,
            "sync_strategy": source.sync_strategy,
            "priority": source.priority,
            "config": source.config_json,
        },
        "source_format": source.source_format,
        "enabled": source.enabled,
        "is_primary": source.is_primary,
        "check_interval_seconds": source.check_interval_seconds,
        "full_refresh_interval_seconds": (
            source.full_refresh_interval_seconds
        ),
    }


def _source_matches_planned(
    source: ReferenceLayerSource,
    planned: PlannedMirrorSource,
) -> bool:
    return bool(
        source.layer_id == planned.layer_id
        and source.source_key == planned.source_key
        and source.protocol == planned.protocol
        and source.target_kind == planned.target_kind
        and source.endpoint_url == planned.endpoint_url
        and source.remote_name == planned.remote_name
        and source.source_format == planned.source_format
        and source.sync_strategy == planned.sync_strategy
        and source.config_json == planned.config_json
        and source.definition_sha256 == planned.definition_sha256
        and source.priority == planned.priority
        and source.is_primary == (planned.is_primary and source.enabled)
        and source.check_interval_seconds
        == planned.check_interval_seconds
        and source.full_refresh_interval_seconds
        == planned.full_refresh_interval_seconds
    )


def _layer_projection(layer: ReferenceLayer) -> dict[str, Any]:
    return {
        "layer_id": layer.id,
        "source_key": layer.source_key,
        "title": layer.title,
        "remote_name": layer.remote_name,
        "service_id": layer.service_id,
        "renderer": layer.renderer,
        "delivery_mode": layer.delivery_mode,
        "status": layer.status,
        "metadata_url": layer.metadata_url,
    }


def _service_projection(service: ReferenceService) -> dict[str, Any]:
    return {
        "service_id": service.id,
        "source_key": service.source_key,
        "title": service.title,
        "upstream_protocol": service.upstream_protocol,
        "base_url": service.base_url,
        "capabilities_url": service.capabilities_url,
        "catalog_license": {
            "status": service.license_status,
            "name": service.license_name,
            "url": service.license_url,
            "attribution": service.attribution,
            "grants_mirror_permission": False,
        },
    }


def _strategy_row_projection(
    row: ReferenceLayerMirrorStrategy,
) -> dict[str, Any]:
    return {
        "strategy_id": row.id,
        "layer_id": row.layer_id,
        "strategy": row.strategy,
        "source_id": row.source_id,
        "reason_code": row.strategy_reason_code,
        "evidence_sha256": row.evidence_sha256,
        "generation": row.generation,
    }


def _authorization_head_fence(
    source: ReferenceLayerSource,
    chain: tuple[ReferenceMirrorAuthorizationReview, ...],
) -> dict[str, Any]:
    head = chain[-1] if chain else None
    return {
        "source_id": source.id,
        "review_id": head.id if head is not None else None,
        "review_sha256": (
            head.review_sha256 if head is not None else None
        ),
        "document_sha256": (
            head.document_sha256 if head is not None else None
        ),
    }


def _authorization_head_projection(
    head: ReferenceMirrorAuthorizationReview | None,
) -> dict[str, Any] | None:
    if head is None:
        return None
    return {
        "review_id": head.id,
        "review_sha256": head.review_sha256,
        "document_sha256": head.document_sha256,
        "source_definition_sha256": head.source_definition_sha256,
        "decision": head.decision,
        "reviewer": head.reviewer,
        "reviewed_at": _utc_isoformat(head.reviewed_at),
        "license": {
            "name": head.license_name,
            "url": head.license_url,
            "terms": head.license_terms,
            "attribution": head.attribution,
        },
        "permissions": {
            "metadata_probe": head.allow_metadata_probe,
            "dataset_download": head.allow_dataset_download,
            "local_storage": head.allow_local_storage,
            "local_service": head.allow_local_service,
            "bulk_tile_seed": head.allow_bulk_tile_seed,
        },
    }


def _validate_provider_key(provider_key: str) -> None:
    if (
        not isinstance(provider_key, str)
        or _PROVIDER_RE.fullmatch(provider_key) is None
    ):
        raise MirrorReviewPacketError(
            "provider key is invalid",
            code="mirror_review_packet_invalid_request",
        )


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MirrorReviewPacketError(
            "authorization timestamp is not timezone-aware",
            code="mirror_review_packet_not_ready",
        )
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Emit a deterministic, non-applicable SIUR mirror review packet"
        )
    )
    parser.add_argument("--provider-key", default="siur")
    parser.add_argument(
        "--expected-packet-sha256",
        help=(
            "fail unless a fresh read-only rebuild matches this reviewed hash"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        register_all_models()
        with SessionLocal() as db:
            if arguments.expected_packet_sha256 is not None:
                packet = require_expected_mirror_review_packet(
                    db,
                    provider_key=arguments.provider_key,
                    expected_packet_sha256=(
                        arguments.expected_packet_sha256
                    ),
                )
            else:
                packet = build_mirror_review_packet(
                    db,
                    provider_key=arguments.provider_key,
                )
        print(
            json.dumps(
                packet.public_document(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except MirrorReviewPacketError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": error.code,
                    "error_summary": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except Exception as error:  # pragma: no cover - operator boundary
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "mirror_review_packet_unavailable",
                    "error_summary": type(error).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
