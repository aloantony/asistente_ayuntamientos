"""Read-only aggregate capacity preflight for current tile mirror strategies."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from io import BytesIO
import re
from typing import Any, Protocol

from sqlalchemy import select
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
        raise AcquisitionValidationError(
            "tile capabilities did not pass their protocol probe",
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

    # Exactly one point-in-time snapshot is shared by every projection.
    capacity = store.inspect_capacity()
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

    summary = _summary(
        rows,
        capacity=capacity,
        errors=errors,
        authorized_error_count=authorized_error_count,
    )
    return {
        "ok": True,
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
        "summary": summary,
        "sources": rows,
    }


def tile_capacity_preflight_exit_code(report: dict[str, Any]) -> int:
    """Return non-zero only for authorized failures or aggregate deficit."""

    summary = report.get("summary")
    if not isinstance(summary, dict):
        return 1
    if summary.get("authorized_error_count", 0) > 0:
        return 1
    if summary.get("capacity_exceeded") is True:
        return 1
    return 0


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
