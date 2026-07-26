"""Operational orchestration for immutable local reference-layer mirrors.

The database is the durable queue.  Every network, GDAL, tile-seeding and
GeoServer operation runs without retaining the coordination session that
claimed the job.  A small background supervisor renews the run lease through
fresh sessions and every durable transition is fenced by that lease.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
import time
from typing import Any, Literal, Protocol, TypeAlias, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.db.session import SessionLocal
from app.reference_layers.acquisition import (
    AcquisitionResult,
    ConditionalRequest,
    ReferenceAcquisitionError,
    ReferenceAcquisitionPipeline,
    persist_acquisition_result,
)
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.delivery_builder import (
    BuiltDeliveryVersion,
    DeliveryBuildError,
    DeliveryContinuityError,
    PreparedDelivery,
    PreparedDeliveryAsset,
    create_delivery_version,
)
from app.reference_layers import geo_ingest
from app.reference_layers.geo_ingest import (
    GeoDatabaseTarget,
    GeoIngestError,
    RasterIngestResult,
    TileArchiveInspection,
    VectorIngestResult,
)
from app.reference_layers.geoserver_admin import (
    GeoServerAdminClient,
    GeoServerAdminConflictError,
    GeoServerAdminError,
    GeoServerAdminUnavailableError,
    InvalidGeoServerPublicationError,
    LayerSmokeResult,
    UnsafeGeoServerAdminConfigurationError,
)
from app.reference_layers.local_tile_archive import (
    LocalTileArchiveError,
    LocalTileArchiveRenderer,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    bind_sync_run_authorization,
    require_bound_sync_run_authorization,
    require_version_local_service_authorization,
)
from app.reference_layers.mirror_lifecycle import (
    AppliedMirrorBootstrap,
    MirrorLeaseLostError,
    MirrorLifecycleError,
    MirrorPromotionConflict,
    SyncRunLease,
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
    claim_next_sync_run,
    enqueue_due_sources,
    finish_sync_run,
    heartbeat_sync_run,
    promote_delivery_version,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAsset,
    ReferenceDeliveryVersion,
    ReferenceDeliveryVersionArtifact,
    ReferenceLayer,
    ReferenceLayerDeliveryState,
    ReferenceLayerSource,
    ReferenceLayerStyle,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.style_parity import (
    StyleParityError,
    StyleParityPlanResult,
    persist_style_parity_plan,
    require_complete_style_parity,
)
from app.reference_layers.tile_seed import (
    TileContentSample,
    TileSeedError,
    TileSeedResult,
    iter_tile_coordinates,
    parse_tile_source_document,
    sample_tile_source,
    seed_tile_archive,
    tile_content_sample_sha256,
)

logger = logging.getLogger(__name__)

_MAX_JSON_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_FAILURE_SUMMARY = 4096
_MAX_STATS_ITEMS = 64
_STYLE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.ogc.sld+xml",
        "application/xml",
        "text/xml",
    }
)


class MirrorOrchestrationError(RuntimeError):
    """A classified worker-stage failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MirrorWorkerStopping(MirrorOrchestrationError):
    def __init__(self) -> None:
        super().__init__(
            "reference worker is stopping",
            code="worker_stopping",
            retryable=True,
        )


class SessionFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Session]: ...


@dataclass(frozen=True)
class RunContext:
    lease: SyncRunLease
    source: ReferenceLayerSource
    run: ReferenceSyncRun
    layer: ReferenceLayer
    styles: tuple[ReferenceLayerStyle, ...]
    conditional: ConditionalRequest | None
    active_version_id: int | None
    active_source_id: int | None
    existing_version_id: int | None
    active_manifest_sha256: str | None = None


@dataclass(frozen=True)
class PersistedRunArtifact:
    artifact_id: int
    artifact_kind: str
    roles: frozenset[str]
    media_type: str
    storage_backend: str
    storage_key: str
    size_bytes: int
    sha256: str
    metadata_json: dict[str, Any]


@dataclass(frozen=True)
class StylePublication:
    catalog_style_id: int
    style_name: str
    storage_key: str
    sha256: str
    asset_kind: Literal["style_sld", "style_package"] = "style_sld"


@dataclass(frozen=True)
class ResolvedStyleMaterial:
    style_artifact: PersistedRunArtifact
    effective_artifact: PersistedRunArtifact
    parity_kind: Literal["exact", "adapted"]
    resources: tuple[PersistedRunArtifact, ...]


@dataclass(frozen=True)
class GeoServerPublicationPlan:
    delivery_kind: Literal["vector", "raster"]
    layer_name: str
    title: str
    primary_storage_key: str
    declared_srs: str
    table_name: str | None
    store_name: str | None
    styles: tuple[StylePublication, ...]
    smoke_style_names: tuple[str | None, ...]
    legend_available: bool
    identify_available: bool


@dataclass(frozen=True)
class TilePublicationAsset:
    storage_key: str
    sha256: str
    expected_tile_count: int
    expected_coordinate_sha256: str
    smoke_z: int
    smoke_x: int
    smoke_y: int
    catalog_style_source_key: str | None = None


@dataclass(frozen=True)
class TilePublicationPlan:
    assets: tuple[TilePublicationAsset, ...]
    requires_full_inspection: bool


@dataclass(frozen=True)
class TileContentCheck:
    unchanged: bool
    sample_count: int
    remote_sha256: str
    local_sha256: str

    def stats(self) -> dict[str, Any]:
        return {
            "tile_content_check_schema": "reference-tile-content-check/v1",
            "tile_content_unchanged": self.unchanged,
            "tile_content_sample_count": self.sample_count,
            "tile_content_remote_sha256": self.remote_sha256,
            "tile_content_local_sha256": self.local_sha256,
        }


PublicationPlan: TypeAlias = GeoServerPublicationPlan | TilePublicationPlan


@dataclass(frozen=True)
class MaterializedDelivery:
    prepared: PreparedDelivery
    publication: PublicationPlan


@dataclass(frozen=True)
class ClassifiedFailure:
    code: str
    summary: str
    retryable: bool
    outcome: Literal["failed", "rejected"]
    stats_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class FailureHandlingResult:
    run_id: int
    terminal_outcome: Literal["failed", "rejected"]
    followup: Literal["retry", "fallback", "none", "lease_lost"]
    source_id: int | None = None
    delay_seconds: int | None = None


@dataclass(frozen=True)
class WorkerResult:
    state: Literal[
        "idle",
        "cancelled",
        "unchanged",
        "succeeded",
        "failed",
        "rejected",
        "lease_lost",
    ]
    run_id: int | None
    version_id: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class SourceReconciliation:
    provider_key: str
    created_count: int
    updated_count: int
    deactivated_count: int


Materializer = Callable[
    [RunContext, AcquisitionResult, tuple[PersistedRunArtifact, ...], "LeaseSupervisor"],
    MaterializedDelivery,
]
Publisher = Callable[
    [RunContext, PublicationPlan, "LeaseSupervisor"],
    dict[str, Any] | None,
]
TileContentChecker = Callable[
    [
        RunContext,
        AcquisitionResult,
        tuple[PersistedRunArtifact, ...],
        TilePublicationPlan,
        "LeaseSupervisor",
    ],
    TileContentCheck,
]
FailureHandler = Callable[
    [SessionFactory, SyncRunLease, ClassifiedFailure],
    FailureHandlingResult,
]
def build_reference_blob_store(config: Settings = settings) -> ReferenceBlobStore:
    """Build the worker's quota-enforcing content-addressed store."""

    return ReferenceBlobStore(
        config.reference_storage_root,
        max_blob_bytes=config.reference_blob_max_bytes,
        quota_bytes=config.reference_storage_quota_bytes,
        min_free_bytes=config.reference_storage_min_free_bytes,
    )


class LeaseSupervisor:
    """Renew one lease using independent, short-lived sessions."""

    def __init__(
        self,
        session_factory: SessionFactory,
        lease: SyncRunLease,
        *,
        lease_seconds: int,
        heartbeat_seconds: float,
        stop_event: threading.Event | None = None,
        heartbeat: Callable[..., SyncRunLease] = heartbeat_sync_run,
    ) -> None:
        self._session_factory = session_factory
        self._lease = lease
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._stop_event = stop_event
        self._heartbeat = heartbeat
        self._lock = threading.Lock()
        self._thread_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._last_heartbeat = 0.0

    @property
    def lease(self) -> SyncRunLease:
        with self._lock:
            return self._lease

    def __enter__(self) -> "LeaseSupervisor":
        self.pulse(force=True)
        self._thread = threading.Thread(
            target=self._run,
            name=f"reference-heartbeat-{self._lease.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._thread_stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=min(5.0, self._heartbeat_seconds))

    def pulse(self, *, force: bool = False) -> SyncRunLease:
        if self._stop_event is not None and self._stop_event.is_set():
            raise MirrorWorkerStopping()
        with self._lock:
            if self._error is not None:
                if isinstance(self._error, BaseException):
                    raise MirrorLeaseLostError(
                        "reference worker could not maintain its sync-run lease"
                    ) from self._error
            monotonic_now = time.monotonic()
            if (
                not force
                and monotonic_now - self._last_heartbeat
                < self._heartbeat_seconds
            ):
                return self._lease
            try:
                with self._session_factory() as db:
                    renewed = self._heartbeat(
                        db,
                        self._lease,
                        lease_seconds=self._lease_seconds,
                    )
            except BaseException as error:
                self._error = error
                raise
            self._lease = renewed
            self._last_heartbeat = monotonic_now
            return renewed

    def tile_heartbeat(self, completed: int, total: int) -> None:
        del completed, total
        self.pulse()

    def _run(self) -> None:
        while not self._thread_stop.wait(self._heartbeat_seconds):
            try:
                self.pulse(force=True)
            except BaseException as error:
                with self._lock:
                    self._error = error
                self._thread_stop.set()
                return


class MirrorRunProcessor:
    """Claim and process reference mirror runs one at a time."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = cast(SessionFactory, SessionLocal),
        store: ReferenceBlobStore | None = None,
        acquisition: ReferenceAcquisitionPipeline | None = None,
        geoserver: GeoServerAdminClient | None = None,
        config: Settings = settings,
        stop_event: threading.Event | None = None,
        materializer: Materializer | None = None,
        publisher: Publisher | None = None,
        tile_content_checker: TileContentChecker | None = None,
        failure_handler: FailureHandler | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.stop_event = stop_event
        self.store = store or build_reference_blob_store(config)
        self.acquisition = acquisition or ReferenceAcquisitionPipeline(self.store)
        self.geoserver = geoserver
        self.materializer = materializer or self._materialize_delivery
        self.publisher = publisher or self._publish_and_smoke
        self.tile_content_checker = tile_content_checker or (
            self._check_active_tile_content
        )
        self.failure_handler = failure_handler or (
            lambda factory, lease, failure: handle_run_failure(
                factory,
                lease,
                failure,
            )
        )

    def close(self) -> None:
        self.store.close()

    def process_next(self) -> WorkerResult:
        with self.session_factory() as db:
            lease = claim_next_sync_run(
                db,
                lease_seconds=self.config.reference_mirror_lease_seconds,
            )
        if lease is None:
            return WorkerResult("idle", None)
        return self.process_lease(lease)

    def process_lease(self, lease: SyncRunLease) -> WorkerResult:
        current_lease = lease
        tile_check_stats: dict[str, Any] = {}
        try:
            context = load_run_context(self.session_factory, lease)
            with LeaseSupervisor(
                self.session_factory,
                lease,
                lease_seconds=self.config.reference_mirror_lease_seconds,
                heartbeat_seconds=self.config.reference_mirror_heartbeat_seconds,
                stop_event=self.stop_event,
            ) as supervisor:
                current_lease = supervisor.lease
                if context.existing_version_id is not None:
                    revalidate_run_authorization(
                        self.session_factory,
                        context,
                    )
                    publication = load_existing_publication(
                        self.session_factory,
                        context,
                        context.existing_version_id,
                    )
                    revalidate_run_authorization(
                        self.session_factory,
                        context,
                    )
                    publication_stats = (
                        self.publisher(context, publication, supervisor) or {}
                    )
                    supervisor.pulse(force=True)
                    revalidate_run_authorization(
                        self.session_factory,
                        context,
                    )
                    promote_existing_delivery(
                        self.session_factory,
                        context,
                        supervisor.lease,
                        version_id=context.existing_version_id,
                        observed_manifest_sha256=(
                            context.run.observed_manifest_sha256
                        ),
                        observed_etag=context.run.observed_etag,
                        observed_last_modified=(
                            context.run.observed_last_modified
                        ),
                        observed_version=context.run.observed_version,
                        stats={
                            **dict(context.run.stats_json),
                            "delivery_kind": context.source.target_kind,
                            "delivery_version_id": (
                                context.existing_version_id
                            ),
                            "resumed_existing_version": True,
                            **publication_stats,
                        },
                    )
                    return WorkerResult(
                        "succeeded",
                        lease.run_id,
                        version_id=context.existing_version_id,
                    )

                revalidate_run_authorization(
                    self.session_factory,
                    context,
                )
                acquired = self.acquisition.acquire(
                    context.source,
                    run=context.run,
                    conditional=context.conditional,
                )
                supervisor.pulse(force=True)
                revalidate_run_authorization(
                    self.session_factory,
                    context,
                    acquired=acquired,
                )
                persisted = persist_run_acquisition(
                    self.session_factory,
                    supervisor.lease,
                    acquired,
                )
                if acquired.not_modified:
                    if (
                        context.conditional is None
                        or context.active_version_id is None
                        or context.active_source_id != context.source.id
                    ):
                        raise MirrorOrchestrationError(
                            "not-modified cannot establish an initial local delivery",
                            code="unexpected_not_modified",
                        )
                    finish_unchanged(
                        self.session_factory,
                        supervisor.lease,
                        acquired,
                    )
                    return WorkerResult("unchanged", lease.run_id)

                persist_run_style_parity(
                    self.session_factory,
                    context,
                    persisted,
                    acquired,
                )
                revalidate_run_authorization(
                    self.session_factory,
                    context,
                    acquired=acquired,
                )

                if (
                    context.source.target_kind == "tiles"
                    and context.run.check_mode == "conditional"
                    and context.active_version_id is not None
                    and context.active_source_id == context.source.id
                    and context.active_manifest_sha256 is not None
                    and acquired.manifest_sha256
                    == context.active_manifest_sha256
                ):
                    publication = load_active_tile_publication(
                        self.session_factory,
                        context,
                        context.active_version_id,
                    )
                    content_check = self.tile_content_checker(
                        context,
                        acquired,
                        persisted,
                        publication,
                        supervisor,
                    )
                    tile_check_stats = content_check.stats()
                    if content_check.unchanged:
                        revalidate_run_authorization(
                            self.session_factory,
                            context,
                            acquired=acquired,
                        )
                        publication_stats = (
                            self.publisher(context, publication, supervisor)
                            or {}
                        )
                        supervisor.pulse(force=True)
                        revalidate_run_authorization(
                            self.session_factory,
                            context,
                            acquired=acquired,
                        )
                        finish_unchanged(
                            self.session_factory,
                            supervisor.lease,
                            acquired,
                            extra_stats={
                                **tile_check_stats,
                                **publication_stats,
                                "reused_active_tile_archive": True,
                            },
                        )
                        return WorkerResult(
                            "unchanged",
                            lease.run_id,
                            version_id=context.active_version_id,
                        )

                materialized = self.materializer(
                    context,
                    acquired,
                    persisted,
                    supervisor,
                )
                supervisor.pulse(force=True)
                revalidate_run_authorization(
                    self.session_factory,
                    context,
                    acquired=acquired,
                )
                built = persist_delivery_version(
                    self.session_factory,
                    supervisor.lease,
                    materialized.prepared,
                )
                revalidate_run_authorization(
                    self.session_factory,
                    context,
                    acquired=acquired,
                )
                publication_stats = (
                    self.publisher(
                        context,
                        materialized.publication,
                        supervisor,
                    )
                    or {}
                )
                supervisor.pulse(force=True)
                revalidate_run_authorization(
                    self.session_factory,
                    context,
                    acquired=acquired,
                )
                promote_existing_delivery(
                    self.session_factory,
                    context,
                    supervisor.lease,
                    version_id=built.version_id,
                    observed_manifest_sha256=acquired.manifest_sha256,
                    observed_etag=acquired.observed_etag,
                    observed_last_modified=acquired.observed_last_modified,
                    observed_version=acquired.observed_version,
                    stats=_bounded_stats(
                        {
                            **acquired.stats,
                            **tile_check_stats,
                            "total_bytes": acquired.total_bytes,
                            "delivery_kind": materialized.prepared.delivery_kind,
                            "delivery_version_id": built.version_id,
                            **publication_stats,
                        }
                    ),
                )
                logger.info(
                    "Reference mirror run promoted",
                    extra={
                        "run_id": lease.run_id,
                        "source_id": lease.source_id,
                        "version_id": built.version_id,
                    },
                )
                return WorkerResult(
                    "succeeded",
                    lease.run_id,
                    version_id=built.version_id,
                )
        except MirrorWorkerStopping:
            # Do not terminalize the run: lifecycle 0037 would correctly
            # interpret any failed/rejected terminal state as a consumed
            # source and enqueue a fallback. Leaving the fenced run alive lets
            # the same run be reclaimed after lease expiry with all durable
            # acquisition/version checkpoints intact.
            logger.info(
                "Reference mirror worker stopped with run left reclaimable",
                extra={"run_id": lease.run_id, "source_id": lease.source_id},
            )
            return WorkerResult(
                "cancelled",
                lease.run_id,
                error_code="worker_stopping",
            )
        except MirrorLeaseLostError:
            logger.warning(
                "Reference mirror worker lost its lease",
                extra={"run_id": lease.run_id, "source_id": lease.source_id},
            )
            return WorkerResult("lease_lost", lease.run_id, error_code="lease_lost")
        except Exception as error:
            failure = classify_worker_failure(error)
            try:
                handled = self.failure_handler(
                    self.session_factory,
                    current_lease,
                    failure,
                )
            except MirrorLeaseLostError:
                return WorkerResult(
                    "lease_lost",
                    lease.run_id,
                    error_code="lease_lost",
                )
            logger.warning(
                "Reference mirror run ended with a classified failure",
                extra={
                    "run_id": lease.run_id,
                    "source_id": lease.source_id,
                    "error_code": failure.code,
                    "followup": handled.followup,
                },
            )
            return WorkerResult(
                cast(Any, handled.terminal_outcome),
                lease.run_id,
                error_code=failure.code,
            )

    def _materialize_delivery(
        self,
        context: RunContext,
        acquired: AcquisitionResult,
        artifacts: tuple[PersistedRunArtifact, ...],
        supervisor: LeaseSupervisor,
    ) -> MaterializedDelivery:
        if context.source.target_kind == "vector":
            return materialize_vector_delivery(
                self.session_factory,
                self.store,
                context,
                acquired,
                artifacts,
                supervisor,
                database_url=self.config.database_url,
                max_source_bytes=self.config.reference_geo_max_source_bytes,
                timeout_seconds=self.config.reference_geo_timeout_seconds,
            )
        if context.source.target_kind == "raster":
            return materialize_raster_delivery(
                self.store,
                context,
                acquired,
                artifacts,
                supervisor,
                max_source_bytes=self.config.reference_geo_max_source_bytes,
                max_output_bytes=min(
                    self.config.reference_blob_max_bytes,
                    self.config.reference_geo_max_source_bytes,
                    geo_ingest.DEFAULT_MAX_RASTER_OUTPUT_BYTES,
                ),
                timeout_seconds=self.config.reference_geo_timeout_seconds,
            )
        if context.source.target_kind == "tiles":
            return materialize_tile_delivery(
                self.store,
                context,
                acquired,
                artifacts,
                supervisor,
                max_archive_bytes=self.config.reference_tile_archive_max_bytes,
                max_tiles=self.config.reference_tile_max_count,
                concurrency=self.config.reference_tile_concurrency,
                batch_size=self.config.reference_tile_batch_size,
            )
        raise MirrorOrchestrationError(
            "reference source target kind is unsupported",
            code="unsupported_target_kind",
        )

    def _check_active_tile_content(
        self,
        context: RunContext,
        acquired: AcquisitionResult,
        artifacts: tuple[PersistedRunArtifact, ...],
        publication: TilePublicationPlan,
        supervisor: LeaseSupervisor,
    ) -> TileContentCheck:
        supervisor.pulse(force=True)
        result = compare_active_tile_content(
            self.store,
            context,
            acquired,
            artifacts,
            publication,
            sample_limit=self.config.reference_tile_change_check_samples,
            concurrency=self.config.reference_tile_concurrency,
        )
        supervisor.pulse(force=True)
        return result

    def _publish_and_smoke(
        self,
        context: RunContext,
        publication: PublicationPlan,
        supervisor: LeaseSupervisor,
    ) -> dict[str, Any]:
        if isinstance(publication, TilePublicationPlan):
            return {
                "local_operation_smoke": smoke_tile_publication(
                    self.store,
                    publication,
                    supervisor,
                )
            }
        client = self.geoserver or GeoServerAdminClient()
        return {
            "local_operation_smoke": publish_geoserver_delivery(
                self.store,
                client,
                publication,
                supervisor,
            )
        }


def reconcile_reference_sources_once(
    session_factory: SessionFactory = cast(SessionFactory, SessionLocal),
) -> tuple[SourceReconciliation, ...]:
    """Idempotently reconcile mirror sources for every current catalog."""

    with session_factory() as db:
        providers = tuple(
            db.scalars(
                select(ReferenceCatalogSnapshot.provider_key)
                .where(
                    ReferenceCatalogSnapshot.is_current.is_(True),
                    ReferenceCatalogSnapshot.status == "applied",
                )
                .order_by(ReferenceCatalogSnapshot.provider_key)
            )
        )
    reconciled: list[SourceReconciliation] = []
    for provider_key in providers:
        with session_factory() as db:
            plan = build_mirror_bootstrap_plan(
                db,
                provider_key=provider_key,
            )
            applied: AppliedMirrorBootstrap = apply_mirror_bootstrap_plan(
                db,
                plan,
            )
        reconciled.append(
            SourceReconciliation(
                provider_key=provider_key,
                created_count=applied.created_count,
                updated_count=applied.updated_count,
                deactivated_count=applied.deactivated_count,
            )
        )
    return tuple(reconciled)


def enqueue_reference_sources_once(
    session_factory: SessionFactory = cast(SessionFactory, SessionLocal),
    *,
    limit: int | None = None,
    config: Settings = settings,
    reconcile: bool = True,
) -> tuple[int, ...]:
    """Reconcile catalogs, then run the lifecycle's locked scheduler poll."""

    if reconcile:
        reconcile_reference_sources_once(session_factory)
    with session_factory() as db:
        return enqueue_due_sources(
            db,
            limit=config.reference_mirror_enqueue_limit if limit is None else limit,
        )


def load_run_context(
    session_factory: SessionFactory,
    lease: SyncRunLease,
) -> RunContext:
    """Load a detached, immutable view of a live run without retaining a session."""

    with session_factory() as db:
        run = db.scalar(
            select(ReferenceSyncRun)
            .where(
                ReferenceSyncRun.id == lease.run_id,
                ReferenceSyncRun.source_id == lease.source_id,
                ReferenceSyncRun.status == "running",
                ReferenceSyncRun.lease_token == lease.token,
                ReferenceSyncRun.attempt_no == lease.attempt_no,
            )
            .with_for_update()
        )
        source = db.scalar(
            select(ReferenceLayerSource)
            .where(ReferenceLayerSource.id == lease.source_id)
            .with_for_update()
        )
        if run is None or source is None:
            raise MirrorLeaseLostError("sync-run lease is no longer current")
        if not source.enabled:
            raise MirrorOrchestrationError(
                "reference source was disabled after the run was queued",
                code="source_disabled",
            )
        if source.definition_sha256 != run.source_definition_sha256:
            raise MirrorOrchestrationError(
                "reference source changed after the run was queued",
                code="source_definition_changed",
            )
        bind_sync_run_authorization(
            db,
            run=run,
            source=source,
        )
        db.commit()
        layer = db.scalar(
            select(ReferenceLayer).where(
                ReferenceLayer.id == source.layer_id,
                ReferenceLayer.provider_key == source.provider_key,
                ReferenceLayer.node_type == "layer",
                ReferenceLayer.status.in_(("active", "degraded")),
            )
        )
        if layer is None:
            raise MirrorOrchestrationError(
                "reference layer is not current and renderable",
                code="layer_not_renderable",
            )
        styles = tuple(
            db.scalars(
                select(ReferenceLayerStyle)
                .where(
                    ReferenceLayerStyle.provider_key == source.provider_key,
                    ReferenceLayerStyle.layer_id == source.layer_id,
                    ReferenceLayerStyle.last_seen_snapshot_id
                    == layer.last_seen_snapshot_id,
                    ReferenceLayerStyle.status.in_(("active", "degraded")),
                )
                .order_by(
                    ReferenceLayerStyle.sort_order,
                    ReferenceLayerStyle.id,
                )
            )
        )
        state = db.scalar(
            select(ReferenceLayerDeliveryState).where(
                ReferenceLayerDeliveryState.provider_key == source.provider_key,
                ReferenceLayerDeliveryState.layer_id == source.layer_id,
                ReferenceLayerDeliveryState.status == "active",
            )
        )
        active_version_id = state.active_version_id if state is not None else None
        active_source_id = None
        active_manifest_sha256 = None
        active_definition_matches = False
        if active_version_id is not None:
            active_version = db.get(ReferenceDeliveryVersion, active_version_id)
            active_run = (
                None
                if active_version is None
                else db.get(ReferenceSyncRun, active_version.sync_run_id)
            )
            if active_version is not None:
                active_source_id = active_version.source_id
            active_definition_matches = bool(
                active_run is not None
                and active_run.source_id == source.id
                and active_run.source_definition_sha256
                == run.source_definition_sha256
            )
            if active_definition_matches and active_run is not None:
                active_manifest_sha256 = active_run.observed_manifest_sha256
                latest_unchanged = db.scalar(
                    select(ReferenceSyncRun.observed_manifest_sha256)
                    .where(
                        ReferenceSyncRun.source_id == source.id,
                        ReferenceSyncRun.id > active_run.id,
                        ReferenceSyncRun.status == "unchanged",
                        ReferenceSyncRun.source_definition_sha256
                        == run.source_definition_sha256,
                        ReferenceSyncRun.expected_active_generation
                        == state.generation,
                        ReferenceSyncRun.observed_manifest_sha256.is_not(None),
                    )
                    .order_by(ReferenceSyncRun.id.desc())
                    .limit(1)
                )
                if latest_unchanged is not None:
                    active_manifest_sha256 = latest_unchanged
        existing_version_id = db.scalar(
            select(ReferenceDeliveryVersion.id).where(
                ReferenceDeliveryVersion.sync_run_id == run.id,
                ReferenceDeliveryVersion.source_id == source.id,
            )
        )
        conditional = None
        if (
            run.check_mode == "conditional"
            and active_version_id is not None
            and active_source_id == source.id
            and active_definition_matches
        ):
            artifact = db.scalar(
                select(ReferenceSourceArtifact)
                .join(
                    ReferenceDeliveryVersionArtifact,
                    and_(
                        ReferenceDeliveryVersionArtifact.artifact_id
                        == ReferenceSourceArtifact.id,
                        ReferenceDeliveryVersionArtifact.source_id
                        == ReferenceSourceArtifact.source_id,
                    ),
                )
                .where(
                    ReferenceDeliveryVersionArtifact.version_id
                    == active_version_id,
                    ReferenceDeliveryVersionArtifact.role == "input",
                    ReferenceSourceArtifact.source_id == source.id,
                    ReferenceSourceArtifact.source_url.is_not(None),
                    or_(
                        ReferenceSourceArtifact.upstream_etag.is_not(None),
                        ReferenceSourceArtifact.upstream_last_modified.is_not(None),
                    ),
                )
                .order_by(ReferenceSourceArtifact.retrieved_at.desc())
                .limit(1)
            )
            conditional = ConditionalRequest.from_artifact(artifact)
            # A dataset-only 304 cannot prove that an independently served
            # GetStyles response and its graphics are unchanged. Styled WCS
            # sources therefore take a full snapshot so style parity is
            # evaluated on every scheduled check.
            if styles:
                conditional = None
        context = RunContext(
            lease=lease,
            source=source,
            run=run,
            layer=layer,
            styles=styles,
            conditional=conditional,
            active_version_id=active_version_id,
            active_source_id=active_source_id,
            existing_version_id=existing_version_id,
            active_manifest_sha256=active_manifest_sha256,
        )
        db.expunge_all()
        return context


def revalidate_run_authorization(
    session_factory: SessionFactory,
    context: RunContext,
    *,
    acquired: AcquisitionResult | None = None,
) -> None:
    """Fail closed between every network, materialization and publish stage."""

    with session_factory() as db:
        run = db.scalar(
            select(ReferenceSyncRun).where(
                ReferenceSyncRun.id == context.run.id,
                ReferenceSyncRun.source_id == context.source.id,
                ReferenceSyncRun.status == "running",
                ReferenceSyncRun.lease_token == context.lease.token,
                ReferenceSyncRun.attempt_no == context.lease.attempt_no,
            )
        )
        source = db.get(ReferenceLayerSource, context.source.id)
        if run is None or source is None:
            raise MirrorLeaseLostError("sync-run lease is no longer current")
        require_bound_sync_run_authorization(
            db,
            run=run,
            source=source,
            acquired=acquired,
        )


def persist_run_acquisition(
    session_factory: SessionFactory,
    lease: SyncRunLease,
    result: AcquisitionResult,
) -> tuple[PersistedRunArtifact, ...]:
    """Persist acquired CAS identities under the current fencing token."""

    with session_factory() as db:
        source = db.get(ReferenceLayerSource, lease.source_id)
        run = db.get(ReferenceSyncRun, lease.run_id)
        if source is None or run is None:
            raise MirrorLeaseLostError("sync-run source disappeared")
        persist_acquisition_result(
            db,
            source=source,
            run=run,
            result=result,
            lease_token=lease.token,
        )
        # This is a resumable, non-terminal checkpoint.  A worker may have
        # already created the immutable delivery when SIGTERM or a renderer
        # outage interrupts publication; the reclaiming worker must promote
        # with the exact upstream observations and acquisition statistics,
        # rather than silently replacing them with empty values.
        run.observed_etag = result.observed_etag
        run.observed_last_modified = result.observed_last_modified
        run.observed_version = result.observed_version
        run.observed_manifest_sha256 = result.manifest_sha256
        run.stats_json = _bounded_stats(
            {
                **result.stats,
                "total_bytes": result.total_bytes,
            }
        )
        db.commit()
        rows = db.execute(
            select(ReferenceSourceArtifact, ReferenceSyncRunArtifact.role)
            .join(
                ReferenceSyncRunArtifact,
                and_(
                    ReferenceSyncRunArtifact.artifact_id
                    == ReferenceSourceArtifact.id,
                    ReferenceSyncRunArtifact.source_id
                    == ReferenceSourceArtifact.source_id,
                ),
            )
            .where(
                ReferenceSyncRunArtifact.source_id == lease.source_id,
                ReferenceSyncRunArtifact.run_id == lease.run_id,
            )
            .order_by(ReferenceSourceArtifact.id, ReferenceSyncRunArtifact.role)
        ).all()
        grouped: dict[int, tuple[ReferenceSourceArtifact, set[str]]] = {}
        for artifact, role in rows:
            current = grouped.get(artifact.id)
            if current is None:
                grouped[artifact.id] = (artifact, {role})
            else:
                current[1].add(role)
        return tuple(
            PersistedRunArtifact(
                artifact_id=artifact.id,
                artifact_kind=artifact.artifact_kind,
                roles=frozenset(roles),
                media_type=artifact.media_type,
                storage_backend=artifact.storage_backend,
                storage_key=artifact.storage_key,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                metadata_json=dict(artifact.metadata_json),
            )
            for artifact, roles in grouped.values()
        )


def persist_run_style_parity(
    session_factory: SessionFactory,
    context: RunContext,
    artifacts: tuple[PersistedRunArtifact, ...],
    acquired: AcquisitionResult,
) -> StyleParityPlanResult:
    """Persist style evidence even when its strict completeness gate fails."""

    try:
        with session_factory() as db:
            plan = persist_style_parity_plan(
                db,
                provider_key=context.source.provider_key,
                layer_id=context.source.layer_id,
                catalog_snapshot_id=context.layer.last_seen_snapshot_id,
                source_id=context.source.id,
                sync_run_id=context.run.id,
                delivery_kind=context.source.target_kind,
                styles=context.styles,
                artifacts=artifacts,
                probe=acquired.probe,
            )
            db.commit()
        require_complete_style_parity(plan)
        return plan
    except StyleParityError as error:
        raise MirrorOrchestrationError(
            str(error),
            code=error.code,
            retryable=False,
        ) from error


def persist_delivery_version(
    session_factory: SessionFactory,
    lease: SyncRunLease,
    prepared: PreparedDelivery,
) -> BuiltDeliveryVersion:
    with session_factory() as db:
        return create_delivery_version(db, lease=lease, prepared=prepared)


def finish_unchanged(
    session_factory: SessionFactory,
    lease: SyncRunLease,
    acquired: AcquisitionResult,
    *,
    extra_stats: Mapping[str, Any] | None = None,
) -> None:
    stats = dict(acquired.stats)
    if extra_stats is not None:
        stats.update(extra_stats)
    with session_factory() as db:
        finish_sync_run(
            db,
            lease,
            outcome="unchanged",
            observed_etag=acquired.observed_etag,
            observed_last_modified=acquired.observed_last_modified,
            observed_version=acquired.observed_version,
            observed_manifest_sha256=acquired.manifest_sha256,
            stats_json=_bounded_stats(stats),
        )


def promote_existing_delivery(
    session_factory: SessionFactory,
    context: RunContext,
    lease: SyncRunLease,
    *,
    version_id: int,
    observed_manifest_sha256: str | None,
    stats: dict[str, Any],
    observed_etag: str | None = None,
    observed_last_modified: datetime | None = None,
    observed_version: str | None = None,
) -> None:
    with session_factory() as db:
        promote_delivery_version(
            db,
            version_id=version_id,
            lease=lease,
            expected_generation=context.run.expected_active_generation,
            reason="Validated immutable local mirror delivery",
            observed_etag=observed_etag,
            observed_last_modified=observed_last_modified,
            observed_version=observed_version,
            observed_manifest_sha256=observed_manifest_sha256,
            stats_json=_bounded_stats(stats),
        )


def classify_worker_failure(error: BaseException) -> ClassifiedFailure:
    """Map exceptions to bounded, non-secret durable failure information."""

    if isinstance(error, MirrorWorkerStopping):
        return ClassifiedFailure(
            error.code,
            "Worker shutdown interrupted local mirror processing.",
            True,
            "failed",
        )
    if isinstance(error, MirrorOrchestrationError):
        return ClassifiedFailure(
            error.code[:64],
            _safe_summary(str(error)),
            error.retryable,
            "failed" if error.retryable else "rejected",
        )
    if isinstance(error, MirrorAuthorizationError):
        return ClassifiedFailure(
            error.code[:64],
            _safe_summary(str(error)),
            False,
            "rejected",
        )
    if isinstance(error, ReferenceAcquisitionError):
        return ClassifiedFailure(
            error.code[:64],
            "The reviewed upstream source could not produce a valid snapshot.",
            error.retryable,
            "failed" if error.retryable else "rejected",
        )
    if isinstance(error, LocalTileArchiveError):
        return ClassifiedFailure(
            "local_tile_delivery_rejected",
            "The local tile archive failed its delivery smoke check.",
            False,
            "rejected",
        )
    if isinstance(error, (ReferenceBlobStoreError, OSError)):
        return ClassifiedFailure(
            "local_storage_unavailable",
            "Local reference artifact storage was unavailable.",
            True,
            "failed",
        )
    if isinstance(error, TileSeedError):
        return ClassifiedFailure(
            "tile_seed_rejected",
            "The reviewed tile source did not produce a complete local pyramid.",
            False,
            "rejected",
        )
    if isinstance(error, GeoIngestError):
        return ClassifiedFailure(
            "geodata_ingest_rejected",
            "The acquired geodata did not pass local normalization and validation.",
            False,
            "rejected",
        )
    if isinstance(error, GeoServerAdminUnavailableError):
        return ClassifiedFailure(
            "local_renderer_unavailable",
            "The local renderer was unavailable during publication checks.",
            True,
            "failed",
        )
    if isinstance(
        error,
        (
            GeoServerAdminConflictError,
            InvalidGeoServerPublicationError,
            UnsafeGeoServerAdminConfigurationError,
        ),
    ):
        return ClassifiedFailure(
            "local_renderer_rejected",
            "The local renderer rejected the immutable publication description.",
            False,
            "rejected",
        )
    if isinstance(error, GeoServerAdminError):
        return ClassifiedFailure(
            "local_renderer_failed",
            "The local renderer could not validate the immutable publication.",
            True,
            "failed",
        )
    if isinstance(error, DeliveryContinuityError):
        return ClassifiedFailure(
            "delivery_continuity_rejected",
            "The candidate delivery is structurally incompatible with the "
            "active local version.",
            False,
            "rejected",
            {"delivery_continuity": error.evidence},
        )
    if isinstance(error, (DeliveryBuildError, MirrorPromotionConflict)):
        return ClassifiedFailure(
            "delivery_integrity_rejected",
            "The local delivery failed an immutable database integrity check.",
            False,
            "rejected",
        )
    if isinstance(error, MirrorLifecycleError):
        return ClassifiedFailure(
            "mirror_lifecycle_rejected",
            "The mirror lifecycle rejected an invalid state transition.",
            False,
            "rejected",
        )
    return ClassifiedFailure(
        "unexpected_worker_error",
        "The reference worker encountered an unexpected internal error.",
        True,
        "failed",
    )


def handle_run_failure(
    session_factory: SessionFactory,
    lease: SyncRunLease,
    failure: ClassifiedFailure,
) -> FailureHandlingResult:
    """Finish once; lifecycle 0037 atomically owns every fallback decision."""

    with session_factory() as db:
        finish_sync_run(
            db,
            lease,
            outcome=failure.outcome,
            error_code=failure.code,
            error_summary=failure.summary,
            stats_json=_bounded_stats(
                {
                    "retryable_classification": failure.retryable,
                    **(failure.stats_json or {}),
                }
            ),
        )
    with session_factory() as db:
        child = db.scalar(
            select(ReferenceSyncRun).where(
                ReferenceSyncRun.parent_run_id == lease.run_id
            )
        )
        if child is None:
            return FailureHandlingResult(
                lease.run_id,
                failure.outcome,
                "none",
            )
        return FailureHandlingResult(
            lease.run_id,
            failure.outcome,
            "fallback",
            source_id=child.source_id,
        )
def materialize_vector_delivery(
    session_factory: SessionFactory,
    store: ReferenceBlobStore,
    context: RunContext,
    acquired: AcquisitionResult,
    artifacts: tuple[PersistedRunArtifact, ...],
    supervisor: LeaseSupervisor,
    *,
    database_url: str,
    max_source_bytes: int,
    timeout_seconds: int,
) -> MaterializedDelivery:
    """Normalize all ordered vector pages through the multipage ingest API."""

    datasets = _ordered_dataset_artifacts(artifacts)
    if not datasets:
        raise MirrorOrchestrationError(
            "vector acquisition produced no non-empty dataset artifact",
            code="vector_dataset_missing",
        )
    formats = {
        str(item.metadata_json.get("data_format", "")).strip().casefold()
        for item in datasets
    }
    if len(formats) != 1:
        raise MirrorOrchestrationError(
            "vector pages do not share one input format",
            code="vector_format_inconsistent",
        )
    data_format = next(iter(formats))
    input_driver = {
        "geojson": "GeoJSON",
        "json": "GeoJSON",
        "flatgeobuf": "FlatGeobuf",
    }.get(data_format)
    if len(datasets) > 1 and input_driver is None:
        raise MirrorOrchestrationError(
            "multipage vector format has no safe local ingest driver",
            code="vector_multipage_format_unsupported",
        )
    style_assets = _resolve_local_sld_artifacts(context, artifacts)
    supervisor.pulse(force=True)
    database = GeoDatabaseTarget.from_url(database_url)
    inputs = [
        (
            store.resolve_blob(item.storage_key),
            item.sha256,
            _optional_input_layer(context.source, item),
        )
        for item in datasets
    ]
    ingest_many = getattr(geo_ingest, "ingest_vector_artifacts", None)
    if ingest_many is None:
        if len(inputs) != 1:
            raise MirrorOrchestrationError(
                "multipage vector ingest helper is unavailable",
                code="vector_multipage_ingest_unavailable",
                retryable=True,
            )
        with session_factory() as db:
            result = geo_ingest.ingest_vector_artifact(
                db,
                database=database,
                source_path=inputs[0][0],
                input_sha256=inputs[0][1],
                provider_key=context.source.provider_key,
                layer_id=context.source.layer_id,
                run_id=context.run.id,
                input_layer=inputs[0][2],
                minimum_features=1,
                timeout_seconds=timeout_seconds,
            )
    else:
        with session_factory() as db:
            result = cast(Callable[..., VectorIngestResult], ingest_many)(
                db,
                database=database,
                artifacts=inputs,
                provider_key=context.source.provider_key,
                layer_id=context.source.layer_id,
                run_id=context.run.id,
                input_driver=input_driver,
                minimum_features=1,
                max_source_bytes=min(
                    max_source_bytes,
                    getattr(
                        geo_ingest,
                        "MAX_VECTOR_SOURCE_BYTES",
                        max_source_bytes,
                    ),
                ),
                timeout_seconds=timeout_seconds,
            )
    supervisor.pulse(force=True)
    return _geoserver_materialization(
        context,
        acquired,
        artifacts,
        style_assets,
        vector=result,
        raster=None,
    )


def materialize_raster_delivery(
    store: ReferenceBlobStore,
    context: RunContext,
    acquired: AcquisitionResult,
    artifacts: tuple[PersistedRunArtifact, ...],
    supervisor: LeaseSupervisor,
    *,
    max_source_bytes: int,
    max_output_bytes: int,
    timeout_seconds: int,
) -> MaterializedDelivery:
    datasets = _ordered_dataset_artifacts(artifacts)
    if len(datasets) != 1:
        raise MirrorOrchestrationError(
            "raster acquisition must produce exactly one dataset artifact",
            code="raster_dataset_ambiguous",
        )
    dataset = datasets[0]
    data_format = dataset.metadata_json.get("data_format")
    input_driver = {
        "geotiff": "GTiff",
        "tiff": "GTiff",
    }.get(data_format.strip().casefold() if isinstance(data_format, str) else "")
    if input_driver is None:
        raise MirrorOrchestrationError(
            "raster artifact has no allowlisted local input driver",
            code="raster_format_unsupported",
        )
    style_assets = _resolve_local_sld_artifacts(context, artifacts)
    result = geo_ingest.ingest_raster_artifact(
        store,
        source_path=store.resolve_blob(dataset.storage_key),
        input_sha256=dataset.sha256,
        input_driver=input_driver,
        timeout_seconds=timeout_seconds,
        max_source_bytes=min(
            max_source_bytes,
            getattr(
                geo_ingest,
                "MAX_RASTER_SOURCE_BYTES",
                max_source_bytes,
            ),
        ),
        max_output_bytes=max_output_bytes,
    )
    supervisor.pulse(force=True)
    return _geoserver_materialization(
        context,
        acquired,
        artifacts,
        style_assets,
        vector=None,
        raster=result,
    )


def _geoserver_materialization(
    context: RunContext,
    acquired: AcquisitionResult,
    artifacts: tuple[PersistedRunArtifact, ...],
    style_assets: Mapping[int, ResolvedStyleMaterial],
    *,
    vector: VectorIngestResult | None,
    raster: RasterIngestResult | None,
) -> MaterializedDelivery:
    if (vector is None) == (raster is None):
        raise ValueError("exactly one geodata result is required")
    digest = vector.content_sha256 if vector is not None else raster.blob.sha256
    delivery_kind: Literal["vector", "raster"] = (
        "vector" if vector is not None else "raster"
    )
    layer_name = _versioned_name(
        f"l{context.layer.id}_{delivery_kind}_r{context.run.id}",
        digest,
    )
    style_publications: list[StylePublication] = []
    prepared_styles: list[PreparedDeliveryAsset] = []
    style_map: dict[str, str] = {}
    default_style_name = None
    for style in context.styles:
        material = style_assets[style.id]
        artifact = material.effective_artifact
        style_name = _versioned_name(
            f"l{context.layer.id}_s{style.id}",
            artifact.sha256,
        )
        style_map[str(style.id)] = style_name
        if style.is_default:
            default_style_name = style_name
        style_publications.append(
            StylePublication(
                catalog_style_id=style.id,
                style_name=style_name,
                storage_key=artifact.storage_key,
                sha256=artifact.sha256,
                asset_kind=(
                    "style_package"
                    if material.parity_kind == "adapted"
                    else "style_sld"
                ),
            )
        )
        prepared_styles.append(
            PreparedDeliveryAsset(
                asset_key=(
                    f"style-source-{style.id}"
                    if material.parity_kind == "adapted"
                    else f"style-{style.id}"
                ),
                asset_kind="style_sld",
                is_primary=False,
                storage_backend="filesystem",
                storage_key=material.style_artifact.storage_key,
                media_type="application/vnd.ogc.sld+xml",
                sha256=material.style_artifact.sha256,
                size_bytes=material.style_artifact.size_bytes,
                metadata_json={
                    "catalog_style_id": style.id,
                    "catalog_style_source_key": style.source_key,
                    "style_name": style_name,
                    "parity_kind": material.parity_kind,
                    "effective": material.parity_kind == "exact",
                },
            )
        )
        if material.parity_kind == "adapted":
            prepared_styles.append(
                PreparedDeliveryAsset(
                    asset_key=f"style-{style.id}",
                    asset_kind="style_package",
                    is_primary=False,
                    storage_backend="filesystem",
                    storage_key=artifact.storage_key,
                    media_type="application/zip",
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    metadata_json={
                        "catalog_style_id": style.id,
                        "catalog_style_source_key": style.source_key,
                        "style_name": style_name,
                        "parity_kind": "adapted",
                        "effective": True,
                        "resource_sha256": [
                            item.sha256 for item in material.resources
                        ],
                    },
                )
            )
    if context.styles and default_style_name is None:
        raise MirrorOrchestrationError(
            "catalog styles have no unique active default",
            code="local_default_style_missing",
        )

    renderer_metadata = {
        "renderer": "geoserver",
        "layer_name": layer_name,
        "default_style_name": default_style_name,
        "styles": style_map,
        "identify_available": bool(
            context.layer.queryable and delivery_kind == "vector"
        ),
        "legend_available": bool(style_publications),
    }
    if vector is not None:
        primary = PreparedDeliveryAsset(
            asset_key="primary",
            asset_kind="vector_table",
            is_primary=True,
            storage_backend="postgres",
            storage_key=vector.storage_key,
            media_type="application/x-postgis-table",
            sha256=vector.content_sha256,
            size_bytes=None,
            metadata_json={
                **renderer_metadata,
                "table_name": vector.table_name,
                "datastore_name": "siur_postgis",
                "schema_name": "reference_data",
            },
        )
        prepared = PreparedDelivery(
            delivery_kind="vector",
            source_version=acquired.observed_version,
            content_sha256=vector.content_sha256,
            reference_at=acquired.observed_last_modified,
            crs=vector.crs,
            bounds_json=vector.bounds_json,
            feature_count=vector.feature_count,
            validation_json=vector.validation_json,
            input_artifact_ids=_input_artifact_ids(artifacts),
            assets=(primary, *prepared_styles),
            supporting_artifacts=_supporting_artifact_links(artifacts),
        )
        publication = GeoServerPublicationPlan(
            delivery_kind="vector",
            layer_name=layer_name,
            title=context.layer.title,
            primary_storage_key=vector.storage_key,
            declared_srs=vector.crs,
            table_name=vector.table_name,
            store_name=None,
            styles=tuple(style_publications),
            smoke_style_names=(
                tuple(item.style_name for item in style_publications)
                if style_publications
                else (None,)
            ),
            legend_available=bool(style_publications),
            identify_available=bool(context.layer.queryable),
        )
        return MaterializedDelivery(prepared, publication)

    assert raster is not None
    store_name = _versioned_name(
        f"l{context.layer.id}_raster_store_r{context.run.id}",
        raster.blob.sha256,
    )
    primary = PreparedDeliveryAsset(
        asset_key="primary",
        asset_kind="raster_cog",
        is_primary=True,
        storage_backend="filesystem",
        storage_key=raster.blob.storage_key,
        media_type="image/tiff",
        sha256=raster.blob.sha256,
        size_bytes=raster.blob.size_bytes,
        metadata_json={
            **renderer_metadata,
            "store_name": store_name,
        },
    )
    prepared = PreparedDelivery(
        delivery_kind="raster",
        source_version=acquired.observed_version,
        content_sha256=raster.blob.sha256,
        reference_at=acquired.observed_last_modified,
        crs=raster.inspection.crs,
        bounds_json=raster.inspection.bounds_json,
        feature_count=None,
        validation_json=raster.validation_json,
        input_artifact_ids=_input_artifact_ids(artifacts),
        assets=(primary, *prepared_styles),
        supporting_artifacts=_supporting_artifact_links(artifacts),
    )
    publication = GeoServerPublicationPlan(
        delivery_kind="raster",
        layer_name=layer_name,
        title=context.layer.title,
        primary_storage_key=raster.blob.storage_key,
        declared_srs=raster.inspection.crs,
        table_name=None,
        store_name=store_name,
        styles=tuple(style_publications),
        smoke_style_names=(
            tuple(item.style_name for item in style_publications)
            if style_publications
            else (None,)
        ),
        legend_available=bool(style_publications),
        identify_available=False,
    )
    return MaterializedDelivery(prepared, publication)


def materialize_tile_delivery(
    store: ReferenceBlobStore,
    context: RunContext,
    acquired: AcquisitionResult,
    artifacts: tuple[PersistedRunArtifact, ...],
    supervisor: LeaseSupervisor,
    *,
    max_archive_bytes: int,
    max_tiles: int,
    concurrency: int,
    batch_size: int,
    seed: Callable[..., TileSeedResult] = seed_tile_archive,
    inspect: Callable[..., TileArchiveInspection] = geo_ingest.inspect_tile_archive,
) -> MaterializedDelivery:
    descriptor_artifacts = [
        item
        for item in artifacts
        if "input" in item.roles
        and item.artifact_kind == "metadata"
        and item.metadata_json.get("schema") == "reference-tile-source/v1"
    ]
    if len(descriptor_artifacts) != 1:
        raise MirrorOrchestrationError(
            "tile acquisition has no unique reviewed source descriptor",
            code="tile_descriptor_ambiguous",
        )
    source_document = _read_json_blob(store, descriptor_artifacts[0])
    documents = _tile_documents_for_catalog_styles(
        source_document,
        context,
        acquired,
    )
    prepared_assets: list[PreparedDeliveryAsset] = []
    publication_assets: list[TilePublicationAsset] = []
    validations: list[dict[str, Any]] = []
    archive_schemas: list[dict[str, Any]] = []
    primary_index = None
    for index, (style, document) in enumerate(documents):
        descriptor = parse_tile_source_document(document)
        if descriptor.estimated_tile_count > max_tiles:
            raise MirrorOrchestrationError(
                "reviewed tile coverage exceeds the deployment tile limit",
                code="tile_count_limit",
            )
        seeded = seed(
            store,
            source_document=document,
            max_archive_bytes=max_archive_bytes,
            heartbeat=supervisor.tile_heartbeat,
            concurrency=concurrency,
            batch_size=batch_size,
        )
        supervisor.pulse(force=True)
        inspection = inspect(
            store,
            storage_key=seeded.blob.storage_key,
            archive_sha256=seeded.blob.sha256,
            expected_tile_count=seeded.tile_count,
            expected_coordinate_sha256=seeded.coordinate_sha256,
            max_tiles=max_tiles,
        )
        coordinate = next(iter_tile_coordinates(descriptor), None)
        if coordinate is None:
            raise MirrorOrchestrationError(
                "reviewed tile coverage is empty",
                code="tile_coverage_empty",
            )
        is_primary = style is None or style.is_default
        if is_primary:
            if primary_index is not None:
                raise MirrorOrchestrationError(
                    "tile delivery has multiple default styles",
                    code="tile_default_style_ambiguous",
                )
            primary_index = index
        metadata: dict[str, Any] = {
            "renderer": "tile_archive",
            "expected_tile_count": seeded.tile_count,
            "expected_coordinate_sha256": seeded.coordinate_sha256,
            "min_zoom": inspection.min_zoom,
            "max_zoom": inspection.max_zoom,
            "image_format": inspection.image_format,
            "smoke_coordinate": [coordinate.z, coordinate.x, coordinate.y],
        }
        if style is not None:
            metadata["catalog_style_id"] = style.id
            metadata["catalog_style_source_key"] = style.source_key
        prepared_assets.append(
            PreparedDeliveryAsset(
                asset_key=(
                    "primary" if style is None else f"tiles-style-{style.id}"
                ),
                asset_kind="tile_archive",
                is_primary=is_primary,
                storage_backend="filesystem",
                storage_key=seeded.blob.storage_key,
                media_type="application/vnd.mapbox.mbtiles",
                sha256=seeded.blob.sha256,
                size_bytes=seeded.blob.size_bytes,
                metadata_json=metadata,
            )
        )
        publication_assets.append(
            TilePublicationAsset(
                storage_key=seeded.blob.storage_key,
                sha256=seeded.blob.sha256,
                expected_tile_count=seeded.tile_count,
                expected_coordinate_sha256=seeded.coordinate_sha256,
                smoke_z=coordinate.z,
                smoke_x=coordinate.x,
                smoke_y=coordinate.y,
                catalog_style_source_key=(
                    style.source_key if style is not None else None
                ),
            )
        )
        validations.append(inspection.validation_json)
        archive_schemas.append(
            {
                "catalog_style_source_key": (
                    style.source_key if style is not None else None
                ),
                "image_format": inspection.image_format,
                "min_zoom": inspection.min_zoom,
                "max_zoom": inspection.max_zoom,
            }
        )
    if primary_index is None:
        raise MirrorOrchestrationError(
            "tile delivery has no unique default style",
            code="tile_default_style_missing",
        )
    expected_style_keys: tuple[str | None, ...] = (
        tuple(style.source_key for style in context.styles)
        if context.styles
        else (None,)
    )
    actual_style_keys = tuple(
        item.catalog_style_source_key for item in publication_assets
    )
    complete_style_coverage = bool(
        len(actual_style_keys) == len(expected_style_keys)
        and len(set(actual_style_keys)) == len(actual_style_keys)
        and set(actual_style_keys) == set(expected_style_keys)
    )
    if not complete_style_coverage:
        raise MirrorOrchestrationError(
            "baked tile archives do not cover every required style",
            code="tile_style_coverage_incomplete",
        )
    primary = prepared_assets[primary_index]
    validation = {
        "schema_version": "reference-delivery-validation/v1",
        "passed": True,
        "kind": "tiles",
        "checks": {
            "archive_count": len(prepared_assets),
            "catalog_style_count": len(expected_style_keys),
            "complete_style_coverage": complete_style_coverage,
            "required_style_source_keys": [
                item or "__implicit_default__"
                for item in expected_style_keys
            ],
            "archives": validations,
            "data_schema": {
                "schema_version": "reference-tiles-schema/v1",
                "archives": archive_schemas,
            },
        },
    }
    prepared = PreparedDelivery(
        delivery_kind="tiles",
        source_version=acquired.observed_version,
        content_sha256=primary.sha256,
        reference_at=acquired.observed_last_modified,
        crs="EPSG:3857",
        bounds_json=documents[0][1]["descriptor"]["bounds"],
        feature_count=None,
        validation_json=validation,
        input_artifact_ids=_input_artifact_ids(artifacts),
        assets=tuple(prepared_assets),
        supporting_artifacts=_supporting_artifact_links(artifacts),
    )
    return MaterializedDelivery(
        prepared,
        TilePublicationPlan(
            tuple(publication_assets),
            requires_full_inspection=False,
        ),
    )


def publish_geoserver_delivery(
    store: ReferenceBlobStore,
    client: GeoServerAdminClient,
    plan: GeoServerPublicationPlan,
    supervisor: LeaseSupervisor,
) -> dict[str, Any]:
    """Idempotently publish fixed local resources, then render-smoke each style."""

    client.health()
    supervisor.pulse()
    if plan.delivery_kind == "vector":
        if plan.table_name is None or plan.store_name is not None:
            raise MirrorOrchestrationError(
                "vector publication plan is malformed",
                code="publication_plan_invalid",
            )
        client.publish_versioned_table(
            datastore_name="siur_postgis",
            schema_name="reference_data",
            table_name=plan.table_name,
            layer_name=plan.layer_name,
            title=plan.title,
            declared_srs=plan.declared_srs,
        )
    else:
        if plan.store_name is None or plan.table_name is not None:
            raise MirrorOrchestrationError(
                "raster publication plan is malformed",
                code="publication_plan_invalid",
            )
        client.publish_local_geotiff(
            store_name=plan.store_name,
            layer_name=plan.layer_name,
            artifact_relative_path=plan.primary_storage_key,
            title=plan.title,
        )
    supervisor.pulse()
    for style in plan.styles:
        payload = _read_blob_bytes(
            store,
            storage_key=style.storage_key,
            expected_sha256=style.sha256,
            max_bytes=(
                64 * 1024 * 1024
                if style.asset_kind == "style_package"
                else 1024 * 1024
            ),
        )
        if style.asset_kind == "style_package":
            client.publish_immutable_sld_package(
                style_name=style.style_name,
                package=payload,
            )
        else:
            client.publish_immutable_sld(
                style_name=style.style_name,
                sld=payload,
            )
        client.ensure_layer_style(
            layer_name=plan.layer_name,
            style_name=style.style_name,
        )
        supervisor.pulse()
    smoke_results: list[LayerSmokeResult] = []
    for style_name in plan.smoke_style_names:
        smoke_results.append(
            client.smoke_layer(
                layer_name=plan.layer_name,
                style_name=style_name,
                legend_available=plan.legend_available,
                identify_available=plan.identify_available,
                z=0,
                x=0,
                y=0,
                pixel_x=128,
                pixel_y=128,
            )
        )
        supervisor.pulse()
    return {
        "schema_version": "reference-local-operation-smoke/v1",
        "renderer": "geoserver",
        "transport": "numeric_loopback_http",
        "delivery_kind": plan.delivery_kind,
        "layer_name": plan.layer_name,
        "style_checks": [
            _geoserver_style_smoke_evidence(result)
            for result in smoke_results
        ],
    }


def smoke_tile_publication(
    store: ReferenceBlobStore,
    plan: TilePublicationPlan,
    supervisor: LeaseSupervisor,
    *,
    inspect: Callable[..., TileArchiveInspection] = geo_ingest.inspect_tile_archive,
) -> dict[str, Any]:
    renderer = LocalTileArchiveRenderer(store.root)
    checks: list[dict[str, Any]] = []
    for asset in plan.assets:
        if plan.requires_full_inspection:
            inspect(
                store,
                storage_key=asset.storage_key,
                archive_sha256=asset.sha256,
                expected_tile_count=asset.expected_tile_count,
                expected_coordinate_sha256=asset.expected_coordinate_sha256,
            )
            supervisor.pulse(force=True)
        response = renderer.render_tile(
            storage_key=asset.storage_key,
            archive_sha256=asset.sha256,
            z=asset.smoke_z,
            x=asset.smoke_x,
            y=asset.smoke_y,
        )
        checks.append(
            {
                "catalog_style_source_key": (
                    asset.catalog_style_source_key
                ),
                "map": {
                    "content_type": response.content_type,
                    "sha256": hashlib.sha256(response.body).hexdigest(),
                    "size_bytes": len(response.body),
                    "z": asset.smoke_z,
                    "x": asset.smoke_x,
                    "y": asset.smoke_y,
                },
            }
        )
        supervisor.pulse()
    return {
        "schema_version": "reference-local-operation-smoke/v1",
        "renderer": "tile_archive",
        "transport": "local_immutable_mbtiles",
        "delivery_kind": "tiles",
        "style_checks": checks,
    }


def _geoserver_style_smoke_evidence(
    result: LayerSmokeResult,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "style_name": result.style_name,
        "map": {
            "content_type": result.image_content_type,
            "sha256": result.image_sha256,
            "size_bytes": result.image_bytes,
            "z": result.z,
            "x": result.x,
            "y": result.y,
        },
    }
    if result.legend_sha256 is not None:
        evidence["legend"] = {
            "content_type": result.legend_content_type,
            "sha256": result.legend_sha256,
            "size_bytes": result.legend_bytes,
        }
    if result.identify_sha256 is not None:
        evidence["identify"] = {
            "content_type": result.identify_content_type,
            "sha256": result.identify_sha256,
            "size_bytes": result.identify_bytes,
            "feature_count": result.identify_feature_count,
            "z": result.z,
            "x": result.x,
            "y": result.y,
            "pixel_x": result.pixel_x,
            "pixel_y": result.pixel_y,
        }
    return evidence


def load_existing_publication(
    session_factory: SessionFactory,
    context: RunContext,
    version_id: int,
    *,
    require_current_run: bool = True,
) -> PublicationPlan:
    """Rebuild a closed publication plan for crash/reclaim resumption."""

    with session_factory() as db:
        predicates = [
            ReferenceDeliveryVersion.id == version_id,
            ReferenceDeliveryVersion.source_id == context.source.id,
            ReferenceDeliveryVersion.provider_key == context.source.provider_key,
            ReferenceDeliveryVersion.layer_id == context.source.layer_id,
        ]
        if require_current_run:
            predicates.append(
                ReferenceDeliveryVersion.sync_run_id == context.run.id
            )
        version = db.scalar(
            select(ReferenceDeliveryVersion).where(*predicates)
        )
        if version is None:
            raise MirrorOrchestrationError(
                "existing delivery version does not belong to the leased run",
                code="existing_delivery_invalid",
            )
        source = db.get(ReferenceLayerSource, version.source_id)
        version_run = db.scalar(
            select(ReferenceSyncRun).where(
                ReferenceSyncRun.id == version.sync_run_id,
                ReferenceSyncRun.source_id == version.source_id,
            )
        )
        if source is None or version_run is None:
            raise MirrorOrchestrationError(
                "existing delivery authorization context is incomplete",
                code="existing_delivery_invalid",
            )
        require_version_local_service_authorization(
            db,
            version=version,
            source=source,
            run=version_run,
        )
        assets = tuple(
            db.scalars(
                select(ReferenceDeliveryAsset)
                .where(ReferenceDeliveryAsset.version_id == version.id)
                .order_by(ReferenceDeliveryAsset.id)
            )
        )
        db.expunge_all()
    primary = [item for item in assets if item.is_primary]
    if len(primary) != 1:
        raise MirrorOrchestrationError(
            "existing delivery has no unique primary asset",
            code="existing_delivery_invalid",
        )
    if version.delivery_kind == "tiles":
        tile_assets: list[TilePublicationAsset] = []
        for item in assets:
            if item.asset_kind != "tile_archive":
                continue
            metadata = _metadata(item.metadata_json)
            coordinate = metadata.get("smoke_coordinate")
            raw_style_key = metadata.get("catalog_style_source_key")
            if raw_style_key is not None and not isinstance(raw_style_key, str):
                raise MirrorOrchestrationError(
                    "existing tile delivery has an invalid style identity",
                    code="existing_delivery_invalid",
                )
            tile_assets.append(
                TilePublicationAsset(
                    storage_key=item.storage_key,
                    sha256=item.sha256,
                    expected_tile_count=_metadata_integer(
                        metadata,
                        "expected_tile_count",
                        minimum=1,
                        maximum=10_000_000,
                    ),
                    expected_coordinate_sha256=_metadata_sha256(
                        metadata,
                        "expected_coordinate_sha256",
                    ),
                    smoke_z=_coordinate_item(coordinate, 0),
                    smoke_x=_coordinate_item(coordinate, 1),
                    smoke_y=_coordinate_item(coordinate, 2),
                    catalog_style_source_key=raw_style_key,
                )
            )
        if not tile_assets:
            raise MirrorOrchestrationError(
                "existing tile delivery has no archives",
                code="existing_delivery_invalid",
            )
        return TilePublicationPlan(
            tuple(tile_assets),
            requires_full_inspection=True,
        )
    if version.delivery_kind not in {"vector", "raster"}:
        raise MirrorOrchestrationError(
            "existing delivery kind is unsupported",
            code="existing_delivery_invalid",
        )
    metadata = _metadata(primary[0].metadata_json)
    layer_name = _metadata_text(metadata, "layer_name")
    style_publications: list[StylePublication] = []
    publication_style_ids: set[int] = set()
    for item in assets:
        if item.asset_kind not in {"style_sld", "style_package"}:
            continue
        style_metadata = _metadata(item.metadata_json)
        if style_metadata.get("effective", True) is not True:
            continue
        catalog_style_id = _metadata_integer(
            style_metadata,
            "catalog_style_id",
            minimum=1,
            maximum=2**31 - 1,
        )
        if catalog_style_id in publication_style_ids:
            raise MirrorOrchestrationError(
                "existing delivery has duplicate effective style assets",
                code="existing_delivery_invalid",
            )
        publication_style_ids.add(catalog_style_id)
        style_publications.append(
            StylePublication(
                catalog_style_id=catalog_style_id,
                style_name=_metadata_text(style_metadata, "style_name"),
                storage_key=item.storage_key,
                sha256=item.sha256,
                asset_kind=cast(Any, item.asset_kind),
            )
        )
    style_map = metadata.get("styles")
    if not isinstance(style_map, dict):
        raise MirrorOrchestrationError(
            "existing GeoServer style map is invalid",
            code="existing_delivery_invalid",
        )
    smoke_names: tuple[str | None, ...] = (
        tuple(item.style_name for item in style_publications)
        if style_publications
        else (None,)
    )
    legend_available = _metadata_boolean(metadata, "legend_available")
    identify_available = _metadata_boolean(metadata, "identify_available")
    if legend_available != bool(style_publications):
        raise MirrorOrchestrationError(
            "existing GeoServer legend availability is inconsistent",
            code="existing_delivery_invalid",
        )
    if identify_available and version.delivery_kind != "vector":
        raise MirrorOrchestrationError(
            "existing raster delivery advertises identify",
            code="existing_delivery_invalid",
        )
    return GeoServerPublicationPlan(
        delivery_kind=cast(Literal["vector", "raster"], version.delivery_kind),
        layer_name=layer_name,
        title=context.layer.title,
        primary_storage_key=primary[0].storage_key,
        declared_srs=version.crs,
        table_name=(
            _metadata_text(metadata, "table_name")
            if version.delivery_kind == "vector"
            else None
        ),
        store_name=(
            _metadata_text(metadata, "store_name")
            if version.delivery_kind == "raster"
            else None
        ),
        styles=tuple(style_publications),
        smoke_style_names=smoke_names,
        legend_available=legend_available,
        identify_available=identify_available,
    )


def load_active_tile_publication(
    session_factory: SessionFactory,
    context: RunContext,
    version_id: int,
) -> TilePublicationPlan:
    with session_factory() as db:
        state = db.scalar(
            select(ReferenceLayerDeliveryState).where(
                ReferenceLayerDeliveryState.provider_key
                == context.source.provider_key,
                ReferenceLayerDeliveryState.layer_id == context.source.layer_id,
                ReferenceLayerDeliveryState.status == "active",
                ReferenceLayerDeliveryState.active_version_id == version_id,
                ReferenceLayerDeliveryState.generation
                == context.run.expected_active_generation,
            )
        )
        if state is None:
            raise MirrorPromotionConflict(
                "active tile delivery changed during conditional validation"
            )
    publication = load_existing_publication(
        session_factory,
        context,
        version_id,
        require_current_run=False,
    )
    if not isinstance(publication, TilePublicationPlan):
        raise MirrorOrchestrationError(
            "active conditional delivery is not a tile archive",
            code="existing_delivery_invalid",
        )
    return publication


def compare_active_tile_content(
    store: ReferenceBlobStore,
    context: RunContext,
    acquired: AcquisitionResult,
    artifacts: tuple[PersistedRunArtifact, ...],
    publication: TilePublicationPlan,
    *,
    sample_limit: int,
    concurrency: int,
    sampler: Callable[..., TileContentSample] = sample_tile_source,
) -> TileContentCheck:
    """Compare deterministic upstream pixels with the active local archives."""

    descriptors = [
        item
        for item in artifacts
        if "input" in item.roles
        and item.artifact_kind == "metadata"
        and item.metadata_json.get("schema") == "reference-tile-source/v1"
    ]
    if len(descriptors) != 1:
        raise MirrorOrchestrationError(
            "tile content check has no unique reviewed source descriptor",
            code="tile_content_check_invalid",
        )
    documents = _tile_documents_for_catalog_styles(
        _read_json_blob(store, descriptors[0]),
        context,
        acquired,
    )
    assets_by_style: dict[str | None, TilePublicationAsset] = {}
    for asset in publication.assets:
        key = asset.catalog_style_source_key
        if key in assets_by_style:
            raise MirrorOrchestrationError(
                "active tile archives have ambiguous style identities",
                code="tile_content_check_invalid",
            )
        assets_by_style[key] = asset
    expected_keys = {
        style.source_key if style is not None else None
        for style, _document in documents
    }
    if set(assets_by_style) != expected_keys:
        raise MirrorOrchestrationError(
            "active tile archives do not match reviewed catalog styles",
            code="tile_content_check_invalid",
        )

    renderer = LocalTileArchiveRenderer(store.root)
    remote_digest = hashlib.sha256(b"reference-tile-content-check/v1\0")
    local_digest = hashlib.sha256(b"reference-tile-content-check/v1\0")
    sample_count = 0
    for style, document in documents:
        key = style.source_key if style is not None else None
        sample = sampler(
            source_document=document,
            sample_limit=sample_limit,
            concurrency=concurrency,
        )
        verified_remote = tile_content_sample_sha256(
            sample.coordinates,
            sample.bodies,
        )
        if verified_remote != sample.content_sha256:
            raise MirrorOrchestrationError(
                "upstream tile sample digest is inconsistent",
                code="tile_content_check_invalid",
            )
        asset = assets_by_style[key]
        local_bodies = tuple(
            renderer.render_tile(
                storage_key=asset.storage_key,
                archive_sha256=asset.sha256,
                z=coordinate.z,
                x=coordinate.x,
                y=coordinate.y,
            ).body
            for coordinate in sample.coordinates
        )
        verified_local = tile_content_sample_sha256(
            sample.coordinates,
            local_bodies,
        )
        key_bytes = (key or "").encode("utf-8")
        framing = len(key_bytes).to_bytes(4, "big") + key_bytes
        remote_digest.update(framing)
        remote_digest.update(bytes.fromhex(verified_remote))
        local_digest.update(framing)
        local_digest.update(bytes.fromhex(verified_local))
        sample_count += len(sample.coordinates)

    remote_sha256 = remote_digest.hexdigest()
    local_sha256 = local_digest.hexdigest()
    return TileContentCheck(
        unchanged=remote_sha256 == local_sha256,
        sample_count=sample_count,
        remote_sha256=remote_sha256,
        local_sha256=local_sha256,
    )


def _resolve_local_sld_artifacts(
    context: RunContext,
    artifacts: tuple[PersistedRunArtifact, ...],
) -> dict[int, ResolvedStyleMaterial]:
    styles_by_id = {item.id: item for item in context.styles}
    styles_by_key = {item.source_key: item for item in context.styles}
    if len(styles_by_key) != len(context.styles):
        raise MirrorOrchestrationError(
            "active catalog style keys are ambiguous",
            code="local_style_ambiguous",
        )
    defaults = [item for item in context.styles if item.is_default]
    if context.styles and len(defaults) != 1:
        raise MirrorOrchestrationError(
            "active catalog styles have no unique default",
            code="local_default_style_missing",
        )
    package_by_key: dict[str, PersistedRunArtifact] = {}
    resources_by_sha: dict[str, PersistedRunArtifact] = {}
    for artifact in artifacts:
        if "style_package" in artifact.roles:
            raw_key = artifact.metadata_json.get(
                "catalog_style_source_key"
            )
            if (
                artifact.artifact_kind != "style_package"
                or artifact.storage_backend != "filesystem"
                or artifact.media_type.casefold() != "application/zip"
                or not isinstance(raw_key, str)
                or raw_key in package_by_key
            ):
                raise MirrorOrchestrationError(
                    "local style package has an unsupported identity",
                    code="local_style_package_invalid",
                )
            package_by_key[raw_key] = artifact
        if "style_resource" in artifact.roles:
            if (
                artifact.artifact_kind != "style_resource"
                or artifact.storage_backend != "filesystem"
                or artifact.sha256 in resources_by_sha
            ):
                raise MirrorOrchestrationError(
                    "local style resource has an unsupported identity",
                    code="local_style_resource_invalid",
                )
            resources_by_sha[artifact.sha256] = artifact

    resolved: dict[int, ResolvedStyleMaterial] = {}
    for artifact in artifacts:
        if "style" not in artifact.roles:
            continue
        if (
            artifact.artifact_kind != "style"
            or artifact.storage_backend != "filesystem"
            or artifact.media_type.casefold() not in _STYLE_MEDIA_TYPES
        ):
            raise MirrorOrchestrationError(
                "local style artifact has an unsupported identity",
                code="local_style_invalid",
            )
        raw_key = artifact.metadata_json.get("catalog_style_source_key")
        raw_id = artifact.metadata_json.get("catalog_style_id")
        by_key = styles_by_key.get(raw_key) if isinstance(raw_key, str) else None
        by_id = (
            styles_by_id.get(raw_id)
            if isinstance(raw_id, int) and not isinstance(raw_id, bool)
            else None
        )
        if by_key is not None and by_id is not None and by_key.id != by_id.id:
            raise MirrorOrchestrationError(
                "portable and local style identities disagree",
                code="local_style_ambiguous",
            )
        style = by_key or by_id
        if style is None:
            raise MirrorOrchestrationError(
                "local style artifact does not map to an active catalog style",
                code="local_style_invalid",
            )
        if style.id in resolved:
            raise MirrorOrchestrationError(
                "active catalog style has multiple local SLD artifacts",
                code="local_style_ambiguous",
            )
        parity_kind = artifact.metadata_json.get("parity_kind", "exact")
        unresolved = artifact.metadata_json.get(
            "unresolved_resources",
            [],
        )
        raw_bindings = artifact.metadata_json.get(
            "resource_bindings",
            [],
        )
        if parity_kind == "missing" or unresolved:
            raise MirrorOrchestrationError(
                "local style has unresolved auxiliary resources",
                code="local_style_resource_missing",
            )
        if parity_kind == "exact":
            if raw_bindings or style.source_key in package_by_key:
                raise MirrorOrchestrationError(
                    "exact local style has contradictory resource evidence",
                    code="local_style_invalid",
                )
            resolved[style.id] = ResolvedStyleMaterial(
                style_artifact=artifact,
                effective_artifact=artifact,
                parity_kind="exact",
                resources=(),
            )
            continue
        if parity_kind != "adapted" or not isinstance(raw_bindings, list):
            raise MirrorOrchestrationError(
                "local style parity classification is invalid",
                code="local_style_invalid",
            )
        package = package_by_key.get(style.source_key)
        if (
            package is None
            or package.metadata_json.get("sld_sha256") != artifact.sha256
        ):
            raise MirrorOrchestrationError(
                "adapted local style has no matching immutable package",
                code="local_style_package_missing",
            )
        style_resources: list[PersistedRunArtifact] = []
        seen_resource_sha: set[str] = set()
        for binding in raw_bindings:
            raw_sha = (
                binding.get("sha256")
                if isinstance(binding, Mapping)
                else None
            )
            resource = (
                resources_by_sha.get(raw_sha)
                if isinstance(raw_sha, str)
                else None
            )
            if resource is None or raw_sha in seen_resource_sha:
                raise MirrorOrchestrationError(
                    "adapted local style resource evidence is incomplete",
                    code="local_style_resource_missing",
                )
            seen_resource_sha.add(raw_sha)
            style_resources.append(resource)
        if not style_resources:
            raise MirrorOrchestrationError(
                "adapted local style has no immutable resources",
                code="local_style_resource_missing",
            )
        resolved[style.id] = ResolvedStyleMaterial(
            style_artifact=artifact,
            effective_artifact=package,
            parity_kind="adapted",
            resources=tuple(style_resources),
        )
    missing = sorted(set(styles_by_id) - set(resolved))
    if missing:
        raise MirrorOrchestrationError(
            "active catalog styles do not all have a local SLD artifact",
            code="local_style_missing",
        )
    return resolved


def _tile_documents_for_catalog_styles(
    source_document: object,
    context: RunContext,
    acquired: AcquisitionResult,
) -> tuple[tuple[ReferenceLayerStyle | None, dict[str, Any]], ...]:
    if not isinstance(source_document, dict):
        raise MirrorOrchestrationError(
            "tile source document is not an object",
            code="tile_descriptor_invalid",
        )
    if not context.styles:
        return ((None, deepcopy(source_document)),)
    defaults = [item for item in context.styles if item.is_default]
    if len(defaults) != 1:
        raise MirrorOrchestrationError(
            "active baked styles have no unique default",
            code="tile_default_style_missing",
        )
    protocol = source_document.get("protocol")
    if protocol not in {"wms_tiles", "wmts"}:
        raise MirrorOrchestrationError(
            "styled catalog layer cannot be reproduced by this tile protocol",
            code="tile_style_unverifiable",
        )
    probe = acquired.probe
    if probe is None or not probe.available:
        raise MirrorOrchestrationError(
            "styled tile source has no successful capabilities probe",
            code="tile_style_unverifiable",
        )
    raw_advertised = probe.metadata.get("styles", [])
    if protocol == "wms_tiles":
        if not isinstance(raw_advertised, list) or any(
            not isinstance(item, str) for item in raw_advertised
        ):
            raise MirrorOrchestrationError(
                "WMS style advertisement is malformed",
                code="tile_style_unverifiable",
            )
        advertised = set(raw_advertised)
    else:
        if not isinstance(raw_advertised, list):
            raise MirrorOrchestrationError(
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
            raise MirrorOrchestrationError(
                "WMTS style advertisement is malformed",
                code="tile_style_unverifiable",
            )
        advertised = set(cast(list[str], names))
    remote_names = [item.remote_name for item in context.styles]
    if (
        any(not isinstance(item, str) or not item for item in remote_names)
        or len(set(remote_names)) != len(remote_names)
        or any(item not in advertised for item in remote_names)
    ):
        raise MirrorOrchestrationError(
            "catalog styles are not all advertised by the probed tile source",
            code="tile_style_unverifiable",
        )
    documents: list[tuple[ReferenceLayerStyle, dict[str, Any]]] = []
    for style in context.styles:
        document = deepcopy(source_document)
        descriptor = document.get("descriptor")
        if not isinstance(descriptor, dict):
            raise MirrorOrchestrationError(
                "tile source descriptor is malformed",
                code="tile_descriptor_invalid",
            )
        descriptor["style"] = style.remote_name
        kvp = descriptor.get("kvp")
        if not isinstance(kvp, dict):
            raise MirrorOrchestrationError(
                "tile source request descriptor is malformed",
                code="tile_descriptor_invalid",
            )
        kvp["styles" if protocol == "wms_tiles" else "style"] = style.remote_name
        documents.append((style, document))
    return tuple(documents)


def _ordered_dataset_artifacts(
    artifacts: tuple[PersistedRunArtifact, ...],
) -> tuple[PersistedRunArtifact, ...]:
    datasets = [
        item
        for item in artifacts
        if "input" in item.roles and item.artifact_kind == "dataset"
    ]
    if not datasets:
        return ()
    indexed: list[tuple[int, PersistedRunArtifact]] = []
    has_page_index = any("page_index" in item.metadata_json for item in datasets)
    if has_page_index:
        for item in datasets:
            raw = item.metadata_json.get("page_index")
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise MirrorOrchestrationError(
                    "vector page ordering metadata is incomplete",
                    code="vector_page_order_invalid",
                )
            indexed.append((raw, item))
        indexed.sort(key=lambda value: value[0])
        if [item[0] for item in indexed] != list(range(len(indexed))):
            raise MirrorOrchestrationError(
                "vector page ordering metadata has gaps or duplicates",
                code="vector_page_order_invalid",
            )
        return tuple(item[1] for item in indexed)
    if len(datasets) > 1:
        raise MirrorOrchestrationError(
            "multiple dataset artifacts have no explicit semantic order",
            code="vector_page_order_invalid",
        )
    return tuple(datasets)


def _optional_input_layer(
    source: ReferenceLayerSource,
    artifact: PersistedRunArtifact,
) -> str | None:
    value = artifact.metadata_json.get("input_layer")
    if value is None:
        value = source.config_json.get("input_layer")
    return value if isinstance(value, str) and value else None


def _input_artifact_ids(
    artifacts: tuple[PersistedRunArtifact, ...],
) -> tuple[int, ...]:
    values = tuple(
        item.artifact_id for item in artifacts if "input" in item.roles
    )
    if not values or len(values) != len(set(values)):
        raise MirrorOrchestrationError(
            "delivery has no unique immutable input artifact set",
            code="delivery_inputs_invalid",
        )
    return values


def _supporting_artifact_links(
    artifacts: tuple[PersistedRunArtifact, ...],
) -> tuple[tuple[int, str], ...]:
    allowed = {"style", "style_package", "style_resource", "metadata"}
    values = tuple(
        sorted(
            (item.artifact_id, role)
            for item in artifacts
            for role in item.roles
            if role in allowed
        )
    )
    if len(values) != len(set(values)):
        raise MirrorOrchestrationError(
            "delivery supporting provenance is ambiguous",
            code="delivery_inputs_invalid",
        )
    return values


def _versioned_name(prefix: str, sha256: str) -> str:
    if (
        len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise MirrorOrchestrationError(
            "versioned resource digest is invalid",
            code="publication_plan_invalid",
        )
    normalized = "".join(
        character if character.isascii() and character.isalnum() else "_"
        for character in prefix.casefold()
    ).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"r_{normalized}"
    normalized = normalized[:53].rstrip("_")
    return f"{normalized}_v_{sha256[:12]}"


def _read_json_blob(
    store: ReferenceBlobStore,
    artifact: PersistedRunArtifact,
) -> object:
    body = _read_blob_bytes(
        store,
        storage_key=artifact.storage_key,
        expected_sha256=artifact.sha256,
        max_bytes=_MAX_JSON_DOCUMENT_BYTES,
    )

    def unique(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise MirrorOrchestrationError(
            "local tile descriptor is not strict JSON",
            code="tile_descriptor_invalid",
        ) from error


def _read_blob_bytes(
    store: ReferenceBlobStore,
    *,
    storage_key: str,
    expected_sha256: str,
    max_bytes: int,
) -> bytes:
    path = store.resolve_blob(storage_key)
    try:
        if path.stat().st_size > max_bytes:
            raise MirrorOrchestrationError(
                "local artifact exceeds its worker-stage limit",
                code="local_artifact_too_large",
            )
        with store.open_blob(storage_key) as stream:
            body = stream.read(max_bytes + 1)
    except OSError as error:
        raise MirrorOrchestrationError(
            "local artifact could not be read",
            code="local_artifact_unavailable",
            retryable=True,
        ) from error
    if not body or len(body) > max_bytes:
        raise MirrorOrchestrationError(
            "local artifact size is invalid",
            code="local_artifact_invalid",
        )
    import hashlib

    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise MirrorOrchestrationError(
            "local artifact bytes do not match their immutable identity",
            code="local_artifact_integrity",
        )
    return body


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 64:
        raise MirrorOrchestrationError(
            "delivery asset metadata is invalid",
            code="existing_delivery_invalid",
        )
    return value


def _metadata_text(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MirrorOrchestrationError(
            "delivery asset text metadata is invalid",
            code="existing_delivery_invalid",
        )
    return value


def _metadata_integer(
    metadata: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = metadata.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise MirrorOrchestrationError(
            "delivery asset integer metadata is invalid",
            code="existing_delivery_invalid",
        )
    return value


def _metadata_boolean(metadata: Mapping[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if not isinstance(value, bool):
        raise MirrorOrchestrationError(
            "delivery asset boolean metadata is invalid",
            code="existing_delivery_invalid",
        )
    return value


def _metadata_sha256(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise MirrorOrchestrationError(
            "delivery asset hash metadata is invalid",
            code="existing_delivery_invalid",
        )
    return value


def _coordinate_item(value: Any, index: int) -> int:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or value[0] < 0
        or value[0] > 22
        or value[1] < 0
        or value[2] < 0
        or value[1] >= 2 ** value[0]
        or value[2] >= 2 ** value[0]
    ):
        raise MirrorOrchestrationError(
            "delivery tile smoke coordinate is invalid",
            code="existing_delivery_invalid",
        )
    return value[index]


def _bounded_stats(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        str(key)[:128]: item
        for key, item in list(value.items())[:_MAX_STATS_ITEMS]
    }
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        return {"stats_invalid": True}
    if len(encoded) > 1024 * 1024:
        return {"stats_truncated": True, "item_count": len(result)}
    return result


def _safe_summary(value: str) -> str:
    normalized = " ".join(
        "".join(
            character if ord(character) >= 32 and ord(character) != 127 else " "
            for character in value
        ).split()
    )
    if not normalized:
        normalized = "Reference mirror processing was rejected."
    return normalized[:_MAX_FAILURE_SUMMARY]
