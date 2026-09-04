"""Read-only aggregate capacity preflight for current tile mirror strategies."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import re
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.reference_layers.acquisition import (
    AcquisitionConfigurationError,
    AcquisitionLimits,
    AcquisitionValidationError,
    ReferenceAcquisitionError,
    build_tile_source_document,
    candidate_from_source_model,
    tile_source_probe_request,
)
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceStorageCapacity,
)
from app.reference_layers.catalog import (
    canonical_normalized_definition_sha256,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_current_source_authorization,
    url_origin,
)
from app.reference_layers.mirror_strategy import (
    MirrorStrategyError,
    current_mirror_strategies,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerMirrorStrategy,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceMirrorAuthorizationReview,
)
from app.reference_layers.safe_download import (
    HTTPSDownloadPolicy,
    SafeDownloadError,
    SafeHTTPSDownloader,
    normalize_https_url,
)
from app.reference_layers.source_probes import (
    SourceProbe,
    SourceProbeError,
    probe_candidate_document,
)
from app.reference_layers.tile_seed import (
    DEFAULT_PREFLIGHT_SAMPLES,
    TileSeedError,
    TileSeedPreflight,
    authorized_tile_fetcher,
    parse_tile_source_document,
    preflight_tile_archive,
)


SCHEMA_VERSION = "siur-tile-capacity-preflight-v1"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$", re.ASCII)
_MAX_SAMPLE_LIMIT = 256
_MAX_CONCURRENCY = 16


class TileCapacityPreflightInputError(ValueError):
    """The operator selected no safe, current tile strategy set."""


class TileCapacityPreflightError(RuntimeError):
    """One authorized source cannot be projected before bulk seeding."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TileSourceResolution:
    document: dict[str, Any]
    probe: SourceProbe | None


@dataclass(frozen=True)
class TileDocumentVariant:
    style_source_key: str | None
    style_remote_name: str | None
    is_default: bool
    document: dict[str, Any]


@dataclass(frozen=True)
class TileCapacityPreflightFence:
    """Exact current state that every sample and capacity result describes."""

    catalog_snapshot_id: int
    catalog_definition_sha256: str
    strategy_count: int
    style_count: int
    authorization_review_count: int
    selection_sha256: str
    state_sha256: str


class DownloaderFactory(Protocol):
    def __call__(self, policy: HTTPSDownloadPolicy) -> Any: ...


class SourceResolver(Protocol):
    def __call__(
        self,
        source: ReferenceLayerSource,
        authorization: ReferenceMirrorAuthorizationReview,
    ) -> TileSourceResolution: ...


class VariantPreflight(Protocol):
    def __call__(
        self,
        store: ReferenceBlobStore,
        *,
        source_document: object,
        allowed_origins: Sequence[str],
        max_archive_bytes: int,
        sample_limit: int,
        concurrency: int,
    ) -> TileSeedPreflight: ...


@dataclass(frozen=True)
class _ProjectionOnlyStore:
    """The estimator needs a byte ceiling, not a per-archive reservation."""

    max_blob_bytes: int

    def ensure_capacity(self, additional_bytes: int) -> None:
        if (
            isinstance(additional_bytes, bool)
            or not isinstance(additional_bytes, int)
            or additional_bytes < 0
        ):
            raise ValueError("additional_bytes is invalid")


def resolve_tile_source(
    source: ReferenceLayerSource,
    authorization: ReferenceMirrorAuthorizationReview,
    *,
    limits: AcquisitionLimits | None = None,
    downloader_factory: DownloaderFactory = SafeHTTPSDownloader,
) -> TileSourceResolution:
    """Resolve capabilities and the tile descriptor entirely in memory."""

    candidate = candidate_from_source_model(source)
    request = tile_source_probe_request(candidate)
    if request is None:
        return TileSourceResolution(
            document=build_tile_source_document(candidate, probe=None),
            probe=None,
        )

    if url_origin(request.url) != authorization.canonical_origin:
        raise AcquisitionConfigurationError(
            "tile capabilities request crosses the authorized origin",
            code="cross_origin_url",
        )
    effective_limits = limits or AcquisitionLimits()
    policy = HTTPSDownloadPolicy(
        allowed_origins=(authorization.canonical_origin,),
        max_response_bytes=effective_limits.max_probe_bytes,
        timeout_seconds=effective_limits.timeout_seconds,
        idle_timeout_seconds=effective_limits.idle_timeout_seconds,
        max_redirects=effective_limits.max_redirects,
        allowed_content_types=request.allowed_content_types,
    )
    sink = BytesIO()
    result = downloader_factory(policy).download(
        request.url,
        sink,
        accept=request.accept,
    )
    document = sink.getvalue()
    digest = hashlib.sha256(document).hexdigest()
    if (
        result.not_modified
        or result.status_code != 200
        or normalize_https_url(result.source_url)
        != normalize_https_url(request.url)
        or url_origin(result.final_url) != authorization.canonical_origin
        or result.size_bytes != len(document)
        or result.sha256 != digest
    ):
        raise AcquisitionValidationError(
            "tile capabilities download evidence is invalid",
            code="invalid_download_result",
        )
    try:
        probe = probe_candidate_document(candidate, document)
    except SourceProbeError as error:
        # El motivo va en el mensaje, no sólo en la cadena de excepciones: es
        # lo único que queda registrado en el resumen del error, y sin él un
        # rechazo de este tipo obliga a reproducir la sonda a mano. La ortofoto
        # estuvo semanas caída por un motivo que se veía en una línea.
        raise AcquisitionValidationError(
            "tile capabilities did not pass their protocol probe: "
            f"{error}",
            code="invalid_capabilities",
        ) from error
    if not probe.available or probe.canonical_name is None:
        raise AcquisitionValidationError(
            probe.reason or "requested tile layer is unavailable",
            code="collection_unavailable",
        )
    return TileSourceResolution(
        document=build_tile_source_document(candidate, probe=probe),
        probe=probe,
    )


def preflight_tile_variant(
    store: ReferenceBlobStore,
    *,
    source_document: object,
    allowed_origins: Sequence[str],
    max_archive_bytes: int,
    sample_limit: int,
    concurrency: int,
) -> TileSeedPreflight:
    """Project one archive without reserving or writing to the shared CAS."""

    fetcher = authorized_tile_fetcher(
        source_document=source_document,
        allowed_origins=allowed_origins,
    )
    projection_store = _ProjectionOnlyStore(
        max_blob_bytes=store.max_blob_bytes,
    )
    return preflight_tile_archive(
        projection_store,  # type: ignore[arg-type]
        source_document=source_document,
        max_archive_bytes=max_archive_bytes,
        fetcher=fetcher,
        sample_limit=sample_limit,
        concurrency=concurrency,
    )


def aggregate_tile_capacity_preflight(
    db: Session,
    store: ReferenceBlobStore,
    *,
    provider_key: str = "siur",
    layer_id: int | None = None,
    source_id: int | None = None,
    source_key: str | None = None,
    sample_limit: int = DEFAULT_PREFLIGHT_SAMPLES,
    concurrency: int = 4,
    max_archive_bytes: int,
    max_tile_count: int,
    resolver: SourceResolver = resolve_tile_source,
    variant_preflight: VariantPreflight = preflight_tile_variant,
) -> dict[str, Any]:
    """Project every selected current strategy without durable mutations."""

    _validate_inputs(
        provider_key=provider_key,
        layer_id=layer_id,
        source_id=source_id,
        source_key=source_key,
        sample_limit=sample_limit,
        concurrency=concurrency,
        max_archive_bytes=max_archive_bytes,
        max_tile_count=max_tile_count,
        store=store,
    )
    snapshot = _current_snapshot(db, provider_key)
    selected = _selected_strategies(
        db,
        snapshot=snapshot,
        layer_id=layer_id,
        source_id=source_id,
        source_key=source_key,
    )
    if not selected:
        raise TileCapacityPreflightInputError(
            "no current tile strategy matches the requested filters"
        )
    styles_by_layer = _styles_by_layer(
        db,
        snapshot=snapshot,
        layer_ids=[layer.id for _strategy, _source, layer in selected],
    )
    fence = _capture_preflight_fence(
        db,
        snapshot=snapshot,
        layer_id=layer_id,
        source_id=source_id,
        source_key=source_key,
    )
    if fence.selection_sha256 != _model_selection_sha256(
        snapshot,
        selected,
        styles_by_layer,
    ):
        raise TileCapacityPreflightInputError(
            "current tile strategy set changed before sampling"
        )
    rows: list[dict[str, Any]] = []
    errors = Counter[str]()
    authorized_error_count = 0
    for strategy, source, layer in selected:
        row = _base_row(strategy, source, layer)
        blocker = _local_blocker(snapshot, strategy, source, layer)
        if blocker is not None:
            row["status"] = "blocked"
            row["errors"] = [_error(blocker, "tile source is not operational")]
            errors[blocker] += 1
            rows.append(row)
            continue

        try:
            authorization = require_current_source_authorization(
                db,
                source=source,
                require_acquisition=True,
            )
        except MirrorAuthorizationError as error:
            status = (
                "blocked"
                if error.code == "mirror_authorization_restricted"
                else "unauthorized"
            )
            row["status"] = status
            row["authorization_status"] = status
            row["errors"] = [_error(error.code, str(error))]
            errors[error.code] += 1
            rows.append(row)
            continue

        row["authorization_status"] = "authorized"
        row["authorization_review_id"] = authorization.id
        row["authorization_review_sha256"] = authorization.review_sha256
        try:
            resolution = resolver(source, authorization)
            variants = _tile_document_variants(
                resolution,
                styles_by_layer.get(layer.id, ()),
            )
            projected = []
            for variant in variants:
                descriptor = parse_tile_source_document(variant.document)
                if descriptor.estimated_tile_count > max_tile_count:
                    raise TileCapacityPreflightError(
                        "tile coverage exceeds the deployment tile limit",
                        code="tile_count_limit",
                    )
                result = variant_preflight(
                    store,
                    source_document=variant.document,
                    allowed_origins=tuple(
                        authorization.allowed_origins_json
                    ),
                    max_archive_bytes=max_archive_bytes,
                    sample_limit=sample_limit,
                    concurrency=concurrency,
                )
                projected.append(_variant_row(variant, result))
            row.update(_successful_row(projected))
        except (
            ReferenceAcquisitionError,
            SafeDownloadError,
            TileCapacityPreflightError,
            TileSeedError,
        ) as error:
            code = getattr(error, "code", "tile_preflight_failed")
            row["status"] = "error"
            row["errors"] = [_error(code, str(error))]
            errors[code] += 1
            authorized_error_count += 1
        rows.append(row)

    # Reject catalog/source/authorization races before measuring capacity.
    _require_preflight_fence_current(
        db,
        expected=fence,
        snapshot=snapshot,
        layer_id=layer_id,
        source_id=source_id,
        source_key=source_key,
    )
    # This is the sole quota/free-space measurement.  It runs after all
    # sampling under the shared CAS lock, so the two values describe one
    # writer-free instant rather than the beginning of a potentially long run.
    capacity = store.inspect_capacity()
    # Cover a catalog change while the filesystem walk held the CAS lock.
    _require_preflight_fence_current(
        db,
        expected=fence,
        snapshot=snapshot,
        layer_id=layer_id,
        source_id=source_id,
        source_key=source_key,
    )
    summary = _summary(
        rows,
        capacity=capacity,
        errors=errors,
        authorized_error_count=authorized_error_count,
    )
    ready = summary["ready_for_bulk_seed"] is True
    return {
        "ok": ready,
        "report_generated": True,
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "applied": False,
        "provider_key": provider_key,
        "catalog_snapshot_id": snapshot.id,
        "catalog_definition_sha256": snapshot.definition_sha256,
        "filters": {
            "layer_id": layer_id,
            "source_id": source_id,
            "source_key": source_key,
        },
        "limits": {
            "sample_limit_per_archive": sample_limit,
            "concurrency_per_archive": concurrency,
            "max_archive_bytes": max_archive_bytes,
            "max_tile_count_per_archive": max_tile_count,
        },
        "state_fence": {
            "strategy_count": fence.strategy_count,
            "style_count": fence.style_count,
            "authorization_review_count": (
                fence.authorization_review_count
            ),
            "selection_sha256": fence.selection_sha256,
            "state_sha256": fence.state_sha256,
        },
        "summary": summary,
        "sources": rows,
    }


def tile_capacity_preflight_exit_code(report: dict[str, Any]) -> int:
    """Return zero only for a complete, ready, internally coherent gate."""

    summary = report.get("summary")
    if not isinstance(summary, dict) or report.get("ok") is not True:
        return 1
    return 0 if summary.get("ready_for_bulk_seed") is True else 1


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
        raise TileCapacityPreflightInputError(
            "current applied catalog is unavailable"
        )
    try:
        valid = (
            canonical_normalized_definition_sha256(
                snapshot.normalized_definition_json
            )
            == snapshot.definition_sha256
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TileCapacityPreflightInputError(
            "current catalog definition evidence is invalid"
        )
    return snapshot


def _require_complete_strategy_generation(
    db: Session,
    snapshot: ReferenceCatalogSnapshot,
) -> None:
    try:
        current_mirror_strategies(
            db,
            provider_key=snapshot.provider_key,
            snapshot_id=snapshot.id,
        )
    except MirrorStrategyError as error:
        raise TileCapacityPreflightInputError(
            "current mirror strategy generation is incomplete"
        ) from error


def _latest_strategy_generation(
    snapshot: ReferenceCatalogSnapshot,
):
    return (
        select(func.max(ReferenceLayerMirrorStrategy.generation))
        .where(
            ReferenceLayerMirrorStrategy.provider_key
            == snapshot.provider_key,
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot.id,
        )
        .scalar_subquery()
    )


def _selected_strategies(
    db: Session,
    *,
    snapshot: ReferenceCatalogSnapshot,
    layer_id: int | None,
    source_id: int | None,
    source_key: str | None,
) -> list[
    tuple[
        ReferenceLayerMirrorStrategy,
        ReferenceLayerSource,
        ReferenceLayer,
    ]
]:
    _require_complete_strategy_generation(db, snapshot)
    latest_generation = _latest_strategy_generation(snapshot)
    query = (
        select(
            ReferenceLayerMirrorStrategy,
            ReferenceLayerSource,
            ReferenceLayer,
        )
        .join(
            ReferenceLayerSource,
            ReferenceLayerSource.id
            == ReferenceLayerMirrorStrategy.source_id,
        )
        .join(
            ReferenceLayer,
            (ReferenceLayer.id == ReferenceLayerMirrorStrategy.layer_id)
            & (
                ReferenceLayer.provider_key
                == ReferenceLayerMirrorStrategy.provider_key
            ),
        )
        .where(
            ReferenceLayerMirrorStrategy.provider_key
            == snapshot.provider_key,
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot.id,
            ReferenceLayerMirrorStrategy.generation == latest_generation,
            ReferenceLayerMirrorStrategy.strategy == "tiles",
        )
        .order_by(
            ReferenceLayerMirrorStrategy.layer_id,
            ReferenceLayerSource.id,
        )
    )
    if layer_id is not None:
        query = query.where(ReferenceLayerMirrorStrategy.layer_id == layer_id)
    if source_id is not None:
        query = query.where(ReferenceLayerSource.id == source_id)
    if source_key is not None:
        query = query.where(ReferenceLayerSource.source_key == source_key)
    return list(db.execute(query).tuples())


def _styles_by_layer(
    db: Session,
    *,
    snapshot: ReferenceCatalogSnapshot,
    layer_ids: Sequence[int],
) -> dict[int, tuple[ReferenceLayerStyle, ...]]:
    result: dict[int, list[ReferenceLayerStyle]] = defaultdict(list)
    for style in db.scalars(
        select(ReferenceLayerStyle)
        .where(
            ReferenceLayerStyle.provider_key == snapshot.provider_key,
            ReferenceLayerStyle.last_seen_snapshot_id == snapshot.id,
            ReferenceLayerStyle.layer_id.in_(sorted(set(layer_ids))),
            ReferenceLayerStyle.status.in_(("active", "degraded")),
        )
        .order_by(
            ReferenceLayerStyle.layer_id,
            ReferenceLayerStyle.sort_order,
            ReferenceLayerStyle.id,
        )
    ):
        result[style.layer_id].append(style)
    return {key: tuple(value) for key, value in result.items()}


def _capture_preflight_fence(
    db: Session,
    *,
    snapshot: ReferenceCatalogSnapshot,
    layer_id: int | None,
    source_id: int | None,
    source_key: str | None,
) -> TileCapacityPreflightFence:
    _require_complete_strategy_generation(db, snapshot)
    snapshot_rows = db.execute(
        select(
            ReferenceCatalogSnapshot.id.label("catalog_snapshot_id"),
            ReferenceCatalogSnapshot.definition_sha256.label(
                "catalog_definition_sha256"
            ),
            ReferenceCatalogSnapshot.normalized_definition_json.label(
                "normalized_definition_json"
            ),
        ).where(
            ReferenceCatalogSnapshot.provider_key == snapshot.provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    ).mappings().all()
    if len(snapshot_rows) != 1:
        raise TileCapacityPreflightInputError(
            "current applied catalog changed during preflight"
        )
    snapshot_row = snapshot_rows[0]
    try:
        catalog_definition_sha256 = (
            canonical_normalized_definition_sha256(
                snapshot_row["normalized_definition_json"]
            )
        )
    except (TypeError, ValueError):
        catalog_definition_sha256 = None
    if (
        snapshot_row["catalog_snapshot_id"] != snapshot.id
        or snapshot_row["catalog_definition_sha256"]
        != snapshot.definition_sha256
        or catalog_definition_sha256 != snapshot.definition_sha256
    ):
        raise TileCapacityPreflightInputError(
            "current applied catalog changed during preflight"
        )

    latest_generation = _latest_strategy_generation(snapshot)
    query = (
        select(
            ReferenceLayerMirrorStrategy.id.label("strategy_id"),
            ReferenceLayerMirrorStrategy.provider_key.label("provider_key"),
            ReferenceLayerMirrorStrategy.layer_id.label("layer_id"),
            ReferenceLayerMirrorStrategy.catalog_snapshot_id.label(
                "catalog_snapshot_id"
            ),
            ReferenceLayerMirrorStrategy.catalog_definition_sha256.label(
                "strategy_catalog_definition_sha256"
            ),
            ReferenceLayerMirrorStrategy.strategy.label("strategy"),
            ReferenceLayerMirrorStrategy.source_id.label(
                "strategy_source_id"
            ),
            ReferenceLayerMirrorStrategy.strategy_reason_code.label(
                "strategy_reason_code"
            ),
            ReferenceLayerMirrorStrategy.strategy_reason.label(
                "strategy_reason"
            ),
            ReferenceLayerMirrorStrategy.evidence_json.label(
                "strategy_evidence_json"
            ),
            ReferenceLayerMirrorStrategy.evidence_sha256.label(
                "strategy_evidence_sha256"
            ),
            ReferenceLayerMirrorStrategy.generation.label(
                "strategy_generation"
            ),
            ReferenceLayerSource.id.label("source_id"),
            ReferenceLayerSource.provider_key.label("source_provider_key"),
            ReferenceLayerSource.layer_id.label("source_layer_id"),
            ReferenceLayerSource.source_key.label("source_key"),
            ReferenceLayerSource.protocol.label("source_protocol"),
            ReferenceLayerSource.target_kind.label("source_target_kind"),
            ReferenceLayerSource.endpoint_url.label("source_endpoint_url"),
            ReferenceLayerSource.remote_name.label("source_remote_name"),
            ReferenceLayerSource.source_format.label("source_format"),
            ReferenceLayerSource.sync_strategy.label(
                "source_sync_strategy"
            ),
            ReferenceLayerSource.config_json.label("source_config_json"),
            ReferenceLayerSource.definition_sha256.label(
                "source_definition_sha256"
            ),
            ReferenceLayerSource.enabled.label("source_enabled"),
            ReferenceLayerSource.is_primary.label("source_is_primary"),
            ReferenceLayerSource.priority.label("source_priority"),
            ReferenceLayer.id.label("joined_layer_id"),
            ReferenceLayer.provider_key.label("layer_provider_key"),
            ReferenceLayer.last_seen_snapshot_id.label(
                "layer_last_seen_snapshot_id"
            ),
            ReferenceLayer.source_key.label("layer_source_key"),
            ReferenceLayer.node_type.label("layer_node_type"),
            ReferenceLayer.status.label("layer_status"),
        )
        .join(
            ReferenceLayerSource,
            ReferenceLayerSource.id
            == ReferenceLayerMirrorStrategy.source_id,
        )
        .join(
            ReferenceLayer,
            (ReferenceLayer.id == ReferenceLayerMirrorStrategy.layer_id)
            & (
                ReferenceLayer.provider_key
                == ReferenceLayerMirrorStrategy.provider_key
            ),
        )
        .where(
            ReferenceLayerMirrorStrategy.provider_key
            == snapshot.provider_key,
            ReferenceLayerMirrorStrategy.catalog_snapshot_id == snapshot.id,
            ReferenceLayerMirrorStrategy.generation == latest_generation,
            ReferenceLayerMirrorStrategy.strategy == "tiles",
        )
        .order_by(
            ReferenceLayerMirrorStrategy.layer_id,
            ReferenceLayerSource.id,
        )
    )
    if layer_id is not None:
        query = query.where(
            ReferenceLayerMirrorStrategy.layer_id == layer_id
        )
    if source_id is not None:
        query = query.where(ReferenceLayerSource.id == source_id)
    if source_key is not None:
        query = query.where(ReferenceLayerSource.source_key == source_key)
    strategy_records = [dict(row) for row in db.execute(query).mappings()]
    if not strategy_records:
        raise TileCapacityPreflightInputError(
            "current tile strategy set changed during preflight"
        )
    layer_ids = sorted(
        {int(record["joined_layer_id"]) for record in strategy_records}
    )
    source_ids = sorted(
        {int(record["source_id"]) for record in strategy_records}
    )

    style_records = [
        dict(row)
        for row in db.execute(
            select(
                ReferenceLayerStyle.id.label("style_id"),
                ReferenceLayerStyle.provider_key.label(
                    "style_provider_key"
                ),
                ReferenceLayerStyle.layer_id.label("style_layer_id"),
                ReferenceLayerStyle.last_seen_snapshot_id.label(
                    "style_last_seen_snapshot_id"
                ),
                ReferenceLayerStyle.source_key.label("style_source_key"),
                ReferenceLayerStyle.remote_name.label("style_remote_name"),
                ReferenceLayerStyle.sort_order.label("style_sort_order"),
                ReferenceLayerStyle.is_default.label("style_is_default"),
                ReferenceLayerStyle.status.label("style_status"),
            )
            .where(
                ReferenceLayerStyle.provider_key == snapshot.provider_key,
                ReferenceLayerStyle.last_seen_snapshot_id == snapshot.id,
                ReferenceLayerStyle.layer_id.in_(layer_ids),
                ReferenceLayerStyle.status.in_(("active", "degraded")),
            )
            .order_by(
                ReferenceLayerStyle.layer_id,
                ReferenceLayerStyle.sort_order,
                ReferenceLayerStyle.id,
            )
        ).mappings()
    ]
    authorization_records = [
        dict(row)
        for row in db.execute(
            select(
                ReferenceMirrorAuthorizationReview.id.label("review_id"),
                ReferenceMirrorAuthorizationReview.source_id.label(
                    "review_source_id"
                ),
                ReferenceMirrorAuthorizationReview.source_definition_sha256.label(
                    "review_source_definition_sha256"
                ),
                ReferenceMirrorAuthorizationReview.review_sha256.label(
                    "review_sha256"
                ),
                ReferenceMirrorAuthorizationReview.document_sha256.label(
                    "review_document_sha256"
                ),
                ReferenceMirrorAuthorizationReview.decision.label(
                    "review_decision"
                ),
                ReferenceMirrorAuthorizationReview.canonical_origin.label(
                    "review_canonical_origin"
                ),
                ReferenceMirrorAuthorizationReview.allowed_origins_json.label(
                    "review_allowed_origins"
                ),
                ReferenceMirrorAuthorizationReview.allow_metadata_probe.label(
                    "review_allow_metadata_probe"
                ),
                ReferenceMirrorAuthorizationReview.allow_dataset_download.label(
                    "review_allow_dataset_download"
                ),
                ReferenceMirrorAuthorizationReview.allow_local_storage.label(
                    "review_allow_local_storage"
                ),
                ReferenceMirrorAuthorizationReview.allow_local_service.label(
                    "review_allow_local_service"
                ),
                ReferenceMirrorAuthorizationReview.allow_bulk_tile_seed.label(
                    "review_allow_bulk_tile_seed"
                ),
                ReferenceMirrorAuthorizationReview.supersedes_review_id.label(
                    "review_supersedes_id"
                ),
                ReferenceMirrorAuthorizationReview.supersedes_review_sha256.label(
                    "review_supersedes_sha256"
                ),
            )
            .where(
                ReferenceMirrorAuthorizationReview.source_id.in_(source_ids)
            )
            .order_by(
                ReferenceMirrorAuthorizationReview.source_id,
                ReferenceMirrorAuthorizationReview.reviewed_at,
                ReferenceMirrorAuthorizationReview.id,
            )
        ).mappings()
    ]
    selection = {
        "catalog_snapshot_id": snapshot.id,
        "catalog_definition_sha256": snapshot.definition_sha256,
        "strategies": strategy_records,
        "styles": style_records,
    }
    selection_sha256 = _canonical_state_sha256(selection)
    state_sha256 = _canonical_state_sha256(
        {
            **selection,
            "authorization_reviews": authorization_records,
        }
    )
    return TileCapacityPreflightFence(
        catalog_snapshot_id=snapshot.id,
        catalog_definition_sha256=snapshot.definition_sha256,
        strategy_count=len(strategy_records),
        style_count=len(style_records),
        authorization_review_count=len(authorization_records),
        selection_sha256=selection_sha256,
        state_sha256=state_sha256,
    )


def _model_selection_sha256(
    snapshot: ReferenceCatalogSnapshot,
    selected: Sequence[
        tuple[
            ReferenceLayerMirrorStrategy,
            ReferenceLayerSource,
            ReferenceLayer,
        ]
    ],
    styles_by_layer: dict[int, tuple[ReferenceLayerStyle, ...]],
) -> str:
    strategies = [
        {
            "strategy_id": strategy.id,
            "provider_key": strategy.provider_key,
            "layer_id": strategy.layer_id,
            "catalog_snapshot_id": strategy.catalog_snapshot_id,
            "strategy_catalog_definition_sha256": (
                strategy.catalog_definition_sha256
            ),
            "strategy": strategy.strategy,
            "strategy_source_id": strategy.source_id,
            "strategy_reason_code": strategy.strategy_reason_code,
            "strategy_reason": strategy.strategy_reason,
            "strategy_evidence_json": strategy.evidence_json,
            "strategy_evidence_sha256": strategy.evidence_sha256,
            "strategy_generation": strategy.generation,
            "source_id": source.id,
            "source_provider_key": source.provider_key,
            "source_layer_id": source.layer_id,
            "source_key": source.source_key,
            "source_protocol": source.protocol,
            "source_target_kind": source.target_kind,
            "source_endpoint_url": source.endpoint_url,
            "source_remote_name": source.remote_name,
            "source_format": source.source_format,
            "source_sync_strategy": source.sync_strategy,
            "source_config_json": source.config_json,
            "source_definition_sha256": source.definition_sha256,
            "source_enabled": source.enabled,
            "source_is_primary": source.is_primary,
            "source_priority": source.priority,
            "joined_layer_id": layer.id,
            "layer_provider_key": layer.provider_key,
            "layer_last_seen_snapshot_id": layer.last_seen_snapshot_id,
            "layer_source_key": layer.source_key,
            "layer_node_type": layer.node_type,
            "layer_status": layer.status,
        }
        for strategy, source, layer in selected
    ]
    styles = [
        {
            "style_id": style.id,
            "style_provider_key": style.provider_key,
            "style_layer_id": style.layer_id,
            "style_last_seen_snapshot_id": style.last_seen_snapshot_id,
            "style_source_key": style.source_key,
            "style_remote_name": style.remote_name,
            "style_sort_order": style.sort_order,
            "style_is_default": style.is_default,
            "style_status": style.status,
        }
        for layer_key in sorted(styles_by_layer)
        for style in styles_by_layer[layer_key]
    ]
    return _canonical_state_sha256(
        {
            "catalog_snapshot_id": snapshot.id,
            "catalog_definition_sha256": snapshot.definition_sha256,
            "strategies": strategies,
            "styles": styles,
        }
    )


def _require_preflight_fence_current(
    db: Session,
    *,
    expected: TileCapacityPreflightFence,
    snapshot: ReferenceCatalogSnapshot,
    layer_id: int | None,
    source_id: int | None,
    source_key: str | None,
) -> None:
    current = _capture_preflight_fence(
        db,
        snapshot=snapshot,
        layer_id=layer_id,
        source_id=source_id,
        source_key=source_key,
    )
    if current != expected:
        raise TileCapacityPreflightInputError(
            "catalog, source, style or authorization changed during preflight"
        )


def _canonical_state_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise TileCapacityPreflightInputError(
            "current tile strategy evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _tile_document_variants(
    resolution: TileSourceResolution,
    styles: Sequence[ReferenceLayerStyle],
) -> tuple[TileDocumentVariant, ...]:
    document = resolution.document
    if not styles:
        return (
            TileDocumentVariant(
                style_source_key=None,
                style_remote_name=None,
                is_default=True,
                document=deepcopy(document),
            ),
        )
    defaults = [item for item in styles if item.is_default]
    if len(defaults) != 1:
        raise TileCapacityPreflightError(
            "active baked styles have no unique default",
            code="tile_default_style_missing",
        )
    protocol = document.get("protocol")
    if protocol not in {"wms_tiles", "wmts"}:
        raise TileCapacityPreflightError(
            "styled layer cannot be reproduced by this tile protocol",
            code="tile_style_unverifiable",
        )
    probe = resolution.probe
    if probe is None or not probe.available:
        raise TileCapacityPreflightError(
            "styled tile source has no successful capabilities probe",
            code="tile_style_unverifiable",
        )
    raw_advertised = probe.metadata.get("styles", [])
    if protocol == "wms_tiles":
        if not isinstance(raw_advertised, list) or any(
            not isinstance(item, str) for item in raw_advertised
        ):
            raise TileCapacityPreflightError(
                "WMS style advertisement is malformed",
                code="tile_style_unverifiable",
            )
        advertised = set(raw_advertised)
    else:
        if not isinstance(raw_advertised, list):
            raise TileCapacityPreflightError(
                "WMTS style advertisement is malformed",
                code="tile_style_unverifiable",
            )
        names = [
            item.get("name")
            for item in raw_advertised
            if isinstance(item, dict)
        ]
        if len(names) != len(raw_advertised) or any(
            not isinstance(item, str) for item in names
        ):
            raise TileCapacityPreflightError(
                "WMTS style advertisement is malformed",
                code="tile_style_unverifiable",
            )
        advertised = set(names)
    remote_names = [item.remote_name for item in styles]
    if (
        any(not item for item in remote_names)
        or len(set(remote_names)) != len(remote_names)
        or any(item not in advertised for item in remote_names)
    ):
        raise TileCapacityPreflightError(
            "catalog styles are not all advertised by the tile source",
            code="tile_style_unverifiable",
        )

    variants: list[TileDocumentVariant] = []
    for style in styles:
        styled = deepcopy(document)
        descriptor = styled.get("descriptor")
        if not isinstance(descriptor, dict):
            raise TileCapacityPreflightError(
                "tile source descriptor is malformed",
                code="tile_descriptor_invalid",
            )
        descriptor["style"] = style.remote_name
        kvp = descriptor.get("kvp")
        if not isinstance(kvp, dict):
            raise TileCapacityPreflightError(
                "tile source request descriptor is malformed",
                code="tile_descriptor_invalid",
            )
        kvp["styles" if protocol == "wms_tiles" else "style"] = (
            style.remote_name
        )
        variants.append(
            TileDocumentVariant(
                style_source_key=style.source_key,
                style_remote_name=style.remote_name,
                is_default=style.is_default,
                document=styled,
            )
        )
    return tuple(variants)


def _local_blocker(
    snapshot: ReferenceCatalogSnapshot,
    strategy: ReferenceLayerMirrorStrategy,
    source: ReferenceLayerSource,
    layer: ReferenceLayer,
) -> str | None:
    if (
        strategy.catalog_definition_sha256 != snapshot.definition_sha256
        or layer.last_seen_snapshot_id != snapshot.id
    ):
        return "tile_strategy_stale"
    if layer.node_type != "layer" or layer.status not in {
        "active",
        "degraded",
    }:
        return "tile_layer_blocked"
    if not source.enabled:
        return "tile_source_disabled"
    if (
        source.provider_key != snapshot.provider_key
        or source.layer_id != layer.id
        or not source.is_primary
        or source.target_kind != "tiles"
        or source.protocol not in {"wmts", "xyz", "wms_tiles"}
        or source.sync_strategy != "tile_seed"
    ):
        return "tile_source_incompatible"
    return None


def _base_row(
    strategy: ReferenceLayerMirrorStrategy,
    source: ReferenceLayerSource,
    layer: ReferenceLayer,
) -> dict[str, Any]:
    return {
        "provider_key": source.provider_key,
        "layer_id": layer.id,
        "layer_source_key": layer.source_key,
        "strategy_id": strategy.id,
        "source_id": source.id,
        "source_key": source.source_key,
        "source_protocol": source.protocol,
        "source_is_primary": source.is_primary,
        "status": "pending",
        "authorization_status": "unchecked",
        "authorization_review_id": None,
        "authorization_review_sha256": None,
        "archive_count": 0,
        "projected_tile_count": 0,
        "sample_count": 0,
        "sample_bytes": 0,
        "largest_tile_bytes": 0,
        "projected_payload_bytes": 0,
        "projected_archive_bytes": 0,
        "variants": [],
        "errors": [],
    }


def _variant_row(
    variant: TileDocumentVariant,
    result: TileSeedPreflight,
) -> dict[str, Any]:
    return {
        "style_source_key": variant.style_source_key,
        "style_remote_name": variant.style_remote_name,
        "is_default": variant.is_default,
        "tile_count": result.tile_count,
        "sample_count": result.sample_count,
        "sample_bytes": result.sample_bytes,
        "largest_tile_bytes": result.largest_tile_bytes,
        "projected_payload_bytes": result.projected_payload_bytes,
        "projected_archive_bytes": result.projected_archive_bytes,
        "projection_schema": result.projection_schema,
    }


def _successful_row(variants: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "success",
        "archive_count": len(variants),
        "projected_tile_count": sum(item["tile_count"] for item in variants),
        "sample_count": sum(item["sample_count"] for item in variants),
        "sample_bytes": sum(item["sample_bytes"] for item in variants),
        "largest_tile_bytes": max(
            (item["largest_tile_bytes"] for item in variants),
            default=0,
        ),
        "projected_payload_bytes": sum(
            item["projected_payload_bytes"] for item in variants
        ),
        "projected_archive_bytes": sum(
            item["projected_archive_bytes"] for item in variants
        ),
        "variants": list(variants),
        "errors": [],
    }


def _summary(
    rows: Sequence[dict[str, Any]],
    *,
    capacity: ReferenceStorageCapacity,
    errors: Counter[str],
    authorized_error_count: int,
) -> dict[str, Any]:
    projected_archive_bytes = sum(
        row["projected_archive_bytes"] for row in rows
    )
    margin = capacity.margin_after(projected_archive_bytes)
    capacity_exceeded = margin < 0
    if capacity_exceeded:
        errors["aggregate_capacity_exceeded"] += 1
    statuses = Counter(row["status"] for row in rows)
    ready = (
        statuses.get("success", 0) == len(rows)
        and authorized_error_count == 0
        and not capacity_exceeded
    )
    return {
        "strategy_count": len(rows),
        "successful_strategy_count": statuses.get("success", 0),
        "unauthorized_count": statuses.get("unauthorized", 0),
        "blocked_count": statuses.get("blocked", 0),
        "authorized_error_count": authorized_error_count,
        "archive_count": sum(row["archive_count"] for row in rows),
        "projected_tile_count": sum(
            row["projected_tile_count"] for row in rows
        ),
        "sample_count": sum(row["sample_count"] for row in rows),
        "sample_bytes": sum(row["sample_bytes"] for row in rows),
        "projected_payload_bytes": sum(
            row["projected_payload_bytes"] for row in rows
        ),
        "projected_archive_bytes": projected_archive_bytes,
        "storage_used_bytes": capacity.used_bytes,
        "storage_quota_bytes": capacity.quota_bytes,
        "storage_quota_available_bytes": capacity.quota_available_bytes,
        "storage_free_bytes": capacity.free_bytes,
        "storage_min_free_bytes": capacity.min_free_bytes,
        "storage_available_bytes": capacity.available_bytes,
        "capacity_margin_bytes": margin,
        "capacity_exceeded": capacity_exceeded,
        "projection_complete": statuses.get("success", 0) == len(rows),
        "ready_for_bulk_seed": ready,
        "error_count": sum(errors.values()),
        "errors_by_code": dict(sorted(errors.items())),
    }


def _error(code: str, summary: str) -> dict[str, str]:
    return {"code": code, "summary": summary}


def _validate_inputs(
    *,
    provider_key: str,
    layer_id: int | None,
    source_id: int | None,
    source_key: str | None,
    sample_limit: int,
    concurrency: int,
    max_archive_bytes: int,
    max_tile_count: int,
    store: ReferenceBlobStore,
) -> None:
    if (
        not isinstance(provider_key, str)
        or _PROVIDER_RE.fullmatch(provider_key) is None
    ):
        raise TileCapacityPreflightInputError("provider key is invalid")
    for label, value in (("layer id", layer_id), ("source id", source_id)):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise TileCapacityPreflightInputError(f"{label} is invalid")
    if source_key is not None and (
        not isinstance(source_key, str)
        or not source_key
        or len(source_key) > 255
        or any(ord(character) < 32 for character in source_key)
    ):
        raise TileCapacityPreflightInputError("source key is invalid")
    if (
        isinstance(sample_limit, bool)
        or not isinstance(sample_limit, int)
        or not 1 <= sample_limit <= _MAX_SAMPLE_LIMIT
    ):
        raise TileCapacityPreflightInputError(
            "sample limit must be between 1 and 256"
        )
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= _MAX_CONCURRENCY
    ):
        raise TileCapacityPreflightInputError(
            "concurrency must be between 1 and 16"
        )
    if (
        isinstance(max_archive_bytes, bool)
        or not isinstance(max_archive_bytes, int)
        or not 1 <= max_archive_bytes <= store.max_blob_bytes
    ):
        raise TileCapacityPreflightInputError(
            "archive byte limit is invalid"
        )
    if (
        isinstance(max_tile_count, bool)
        or not isinstance(max_tile_count, int)
        or max_tile_count <= 0
    ):
        raise TileCapacityPreflightInputError("tile count limit is invalid")
