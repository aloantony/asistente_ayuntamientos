"""Daily, review-gated checks for locally adapted official MITECO styles.

The committed JSON files remain the only inputs used to author local SLDs.
Live responses are metadata observations: exact baseline bytes are recorded as
unchanged, while any other strict JSON body is staged in the local CAS and
blocks new delivery promotion until an exact human review is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import logging
import re
from time import monotonic
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.db.session import SessionLocal
from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    ReferenceBlobStoreError,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_current_source_metadata_probe_authorization,
)
from app.reference_layers.models import (
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceStyleObservedVersion,
    ReferenceStyleUpdateCheck,
    ReferenceStyleUpdateReview,
)
from app.reference_layers.reviewed_style_evidence import (
    MAX_EVIDENCE_BYTES,
    ObservedMitecoStyleDocument,
    ReviewedMitecoStyleWatchTarget,
    ReviewedStyleEvidenceError,
    observe_miteco_style_document,
    reviewed_miteco_style_watch_target,
)
from app.reference_layers.safe_download import (
    HTTPSDownloadPolicy,
    HTTPSDownloadResult,
    SafeDownloadError,
    SafeHTTPSDownloader,
)
from app.reference_layers.source_discovery import (
    SourceCandidate,
    SourceDiscoveryError,
    reviewed_local_style_recipe,
)


logger = logging.getLogger(__name__)

STYLE_CHECK_INTERVAL_SECONDS = 86_400
STYLE_REVIEW_SCHEMA = "siur-style-update-review-v1"
MAX_STYLE_REVIEW_DOCUMENT_BYTES = 65_536
_STYLE_WATCHER_LOCK_DOMAIN = b"asistente/reference-style-watcher/v1\0"
_STYLE_PERSISTENCE_LOCK_TIMEOUT_MS = 5_000
_SUCCESS_STATUSES = ("unchanged", "style_review_required")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_REVIEW_KEYS = frozenset(
    {
        "schema_version",
        "provider_key",
        "layer_id",
        "source_id",
        "source_definition_sha256",
        "profile",
        "official_style_url",
        "baseline_raw_sha256",
        "baseline_semantic_sha256",
        "observed_version_id",
        "observed_raw_sha256",
        "observed_semantic_sha256",
        "decision",
        "reviewer",
        "reviewed_at",
        "rationale",
    }
)
_STYLE_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/vnd.mapbox.style+json",
        "text/json",
    }
)


class OfficialStyleWatcherError(RuntimeError):
    """A bounded operational or integrity failure."""

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


class OfficialStyleReviewRequiredError(RuntimeError):
    """Stable promotion blocker for an unreviewed live style candidate."""

    code = "style_review_required"
    retryable = False


class StyleUpdateReviewDocumentError(ValueError):
    """An explicit style-review document is malformed or stale."""


class StyleDownloader(Protocol):
    def download(
        self,
        url: str,
        sink: BytesIO,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
    ) -> HTTPSDownloadResult: ...


@dataclass(frozen=True)
class StyleWatchTarget:
    provider_key: str
    layer_id: int
    source_id: int
    source_definition_sha256: str
    profile: str
    official_url: str
    baseline_raw_sha256: str
    baseline_semantic_sha256: str


@dataclass(frozen=True)
class StyleUpdateCheckOutcome:
    disposition: Literal[
        "recorded",
        "duplicate",
        "not_due",
        "lock_busy",
    ]
    status: Literal[
        "unchanged",
        "style_review_required",
        "error",
    ] | None
    check_id: int | None
    source_id: int
    observed_version_id: int | None
    next_check_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "status": self.status,
            "check_id": self.check_id,
            "source_id": self.source_id,
            "observed_version_id": self.observed_version_id,
            "next_check_at": (
                self.next_check_at.isoformat()
                if self.next_check_at is not None
                else None
            ),
        }


@dataclass(frozen=True)
class _AuthorizationBinding:
    review_id: int
    review_sha256: str
    allow_local_storage: bool


@dataclass(frozen=True)
class _ResponseEvidence:
    http_status: int | None = None
    not_modified: bool = False
    final_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    size_bytes: int = 0
    raw_sha256: str | None = None
    redirect_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class StyleUpdateReviewEvidence:
    raw_document: bytes
    document_sha256: str
    review_sha256: str
    provider_key: str
    layer_id: int
    source_id: int
    source_definition_sha256: str
    profile: str
    official_style_url: str
    baseline_raw_sha256: str
    baseline_semantic_sha256: str
    observed_version_id: int
    observed_raw_sha256: str
    observed_semantic_sha256: str
    decision: Literal["retain_vendored", "vendor_update_required"]
    reviewer: str
    reviewed_at: datetime
    rationale: str


@dataclass(frozen=True)
class StyleUpdateReviewPlan:
    evidence: StyleUpdateReviewEvidence
    already_applied_id: int | None

    def public_summary(self, *, applied: bool = False) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "apply" if applied else "dry-run",
            "applied": applied,
            "schema_version": STYLE_REVIEW_SCHEMA,
            "provider_key": self.evidence.provider_key,
            "layer_id": self.evidence.layer_id,
            "source_id": self.evidence.source_id,
            "source_definition_sha256": (
                self.evidence.source_definition_sha256
            ),
            "profile": self.evidence.profile,
            "official_style_url": self.evidence.official_style_url,
            "observed_version_id": self.evidence.observed_version_id,
            "observed_raw_sha256": self.evidence.observed_raw_sha256,
            "observed_semantic_sha256": (
                self.evidence.observed_semantic_sha256
            ),
            "decision": self.evidence.decision,
            "review_sha256": self.evidence.review_sha256,
            "document_sha256": self.evidence.document_sha256,
            "already_applied": self.already_applied_id is not None,
            "review_id": self.already_applied_id,
        }


def build_official_style_downloader(
    target: StyleWatchTarget,
) -> SafeHTTPSDownloader:
    """Build a pinned downloader for one exact allowlisted live URL."""

    parts = urlsplit(target.official_url)
    origin = f"{parts.scheme}://{parts.hostname}"
    if parts.port not in {None, 443}:
        origin = f"{origin}:{parts.port}"
    return SafeHTTPSDownloader(
        HTTPSDownloadPolicy(
            allowed_origins=(origin,),
            max_response_bytes=MAX_EVIDENCE_BYTES,
            timeout_seconds=60.0,
            dns_timeout_seconds=3.0,
            connect_timeout_seconds=10.0,
            idle_timeout_seconds=20.0,
            max_redirects=0,
            allowed_content_types=_STYLE_CONTENT_TYPES,
            user_agent="AsistenteAyuntamientos/official-style-watcher",
        )
    )


def scheduled_style_check_key(
    target: StyleWatchTarget,
    at: datetime,
) -> str:
    moment = _aware_utc(at)
    return (
        f"scheduled:{target.source_id}:"
        f"{target.source_definition_sha256[:16]}:{moment.date().isoformat()}"
    )


def style_watch_target_for_source(
    source: ReferenceLayerSource,
) -> StyleWatchTarget | None:
    """Recognize only exact source-discovery allowlisted flood profiles."""

    config = source.config_json
    if not isinstance(config, dict):
        return None
    equivalence = config.get("reviewed_equivalence")
    if not isinstance(equivalence, dict):
        return None
    profile = equivalence.get("profile")
    if not isinstance(profile, str):
        return None
    target = reviewed_miteco_style_watch_target(profile)
    if target is None:
        return None
    if source.endpoint_url is None or source.remote_name is None:
        raise OfficialStyleWatcherError(
            "reviewed style source identity is incomplete",
            code="style_source_invalid",
        )
    candidate = SourceCandidate(
        protocol=cast(Any, source.protocol),
        target_kind=cast(Any, source.target_kind),
        endpoint_url=source.endpoint_url,
        remote_name=source.remote_name,
        sync_strategy=cast(Any, source.sync_strategy),
        priority=source.priority,
        config=config,
        source_key=source.source_key,
        definition_sha256=source.definition_sha256,
    )
    try:
        recipe = reviewed_local_style_recipe(candidate)
    except SourceDiscoveryError as error:
        raise OfficialStyleWatcherError(
            "reviewed style source no longer matches its allowlist",
            code=error.code,
        ) from error
    reference = recipe.style_reference if recipe is not None else None
    if (
        recipe is None
        or recipe.style_kind != "flood_polygons"
        or not isinstance(reference, dict)
        or reference.get("url") != target.official_url
        or reference.get("local_evidence_sha256")
        != target.baseline_raw_sha256
    ):
        raise OfficialStyleWatcherError(
            "reviewed style source does not match its committed baseline",
            code="style_source_invalid",
        )
    return _style_watch_target(source, target)


def check_official_style_update(
    db: Session,
    *,
    source_id: int,
    store: ReferenceBlobStore,
    trigger_kind: Literal["scheduled", "manual"] = "scheduled",
    idempotency_key: str | None = None,
    force: bool = False,
    checked_at: datetime | None = None,
    interval_seconds: int = STYLE_CHECK_INTERVAL_SECONDS,
    downloader: StyleDownloader | None = None,
) -> StyleUpdateCheckOutcome:
    """Conditionally probe one style and persist immutable review evidence."""

    at = _aware_utc(checked_at or datetime.now(timezone.utc))
    interval = _validated_interval(interval_seconds)
    if trigger_kind not in {"scheduled", "manual"}:
        raise ValueError("trigger_kind must be scheduled or manual")

    try:
        source = db.get(ReferenceLayerSource, source_id)
        if source is None:
            raise ValueError("reference style source does not exist")
        target = style_watch_target_for_source(source)
        if target is None:
            raise ValueError("reference source has no watched official style")
        key = _validated_idempotency_key(
            idempotency_key
            or (
                scheduled_style_check_key(target, at)
                if trigger_kind == "scheduled"
                else ""
            )
        )
        duplicate = _check_by_key(db, target, key)
        if duplicate is not None:
            outcome = _outcome_from_check("duplicate", duplicate)
            db.commit()
            return outcome
        latest = _latest_check(db, target)
        if not force and latest is not None and latest.next_check_at > at:
            outcome = _outcome_from_check("not_due", latest)
            db.commit()
            return outcome

        authorization_failure: MirrorAuthorizationError | None = None
        binding: _AuthorizationBinding | None = None
        try:
            review = require_current_source_metadata_probe_authorization(
                db,
                source=source,
            )
            binding = _authorization_binding(review)
        except MirrorAuthorizationError as error:
            authorization_failure = error

        validator = _current_conditional_validator(db, target)
        request_etag = validator.response_etag if validator is not None else None
        request_last_modified = (
            validator.response_last_modified
            if validator is not None
            else None
        )
        validator_id = validator.id if validator is not None else None
        db.commit()
    except Exception:
        db.rollback()
        raise

    if db.in_transaction():
        db.rollback()
        raise RuntimeError(
            "official style download cannot start inside a database transaction"
        )

    started = monotonic()
    response: _ResponseEvidence | None = None
    observed_document: ObservedMitecoStyleDocument | None = None
    failure: BaseException | None = authorization_failure
    body = b""
    raw_result: HTTPSDownloadResult | None = None

    if failure is None:
        sink = BytesIO()
        try:
            actual_downloader = downloader or build_official_style_downloader(
                target
            )
            raw_result = actual_downloader.download(
                target.official_url,
                sink,
                etag=request_etag,
                last_modified=request_last_modified,
                accept="application/json",
            )
            body = sink.getvalue()
            response = _validate_download_result(
                target,
                raw_result,
                body,
                validator_id=validator_id,
            )
            if not response.not_modified:
                observed_document = observe_miteco_style_document(
                    target.profile,
                    body,
                )
        except (
            SafeDownloadError,
            ReviewedStyleEvidenceError,
            OfficialStyleWatcherError,
        ) as error:
            failure = error
            response = _response_from_failure(
                error,
                target=target,
                result=raw_result,
                body=body or sink.getvalue(),
            )

    check_id = _reserve_style_check_id(db)
    try:
        _acquire_style_persistence_lock(db, target)
    except OfficialStyleWatcherError as error:
        if (
            error.code == "style_persistence_lock_timeout"
            and _is_changed_200_response(target, response)
        ):
            return _record_contended_changed_response(
                db,
                target=target,
                check_id=check_id,
                original_key=key,
                trigger_kind=trigger_kind,
                checked_at=at,
                retry_at=at + min(interval, timedelta(minutes=5)),
                duration_ms=_elapsed_ms(started),
                request_etag=request_etag,
                request_last_modified=request_last_modified,
                response=cast(_ResponseEvidence, response),
                binding=binding,
                error=error,
            )
        raise

    try:
        duplicate = _check_by_key(db, target, key)
        if duplicate is not None:
            collision_key = _concurrent_response_key(
                key,
                duplicate=duplicate,
                response=response,
                failure=failure,
            )
            if collision_key is None:
                outcome = _outcome_from_check("duplicate", duplicate)
                db.commit()
                return outcome
            key = collision_key
            collision = _check_by_key(db, target, key)
            if collision is not None:
                outcome = _outcome_from_check("duplicate", collision)
                db.commit()
                return outcome

        current_source = db.get(ReferenceLayerSource, source_id)
        current_target = (
            style_watch_target_for_source(current_source)
            if current_source is not None
            else None
        )
        if current_target != target:
            failure = OfficialStyleWatcherError(
                "official style source changed during its check",
                code="style_source_changed",
                retryable=False,
            )
            binding = None
        elif binding is not None:
            try:
                current_review = (
                    require_current_source_metadata_probe_authorization(
                        db,
                        source=current_source,
                    )
                )
                current_binding = _authorization_binding(current_review)
                if (
                    current_binding.review_id != binding.review_id
                    or current_binding.review_sha256
                    != binding.review_sha256
                ):
                    raise MirrorAuthorizationError(
                        "metadata authorization changed during style probe",
                        code="mirror_authorization_changed",
                    )
                binding = current_binding
            except MirrorAuthorizationError as error:
                failure = error
                binding = None

        if failure is not None:
            check = _record_error_check(
                db,
                target=target,
                check_id=check_id,
                key=key,
                trigger_kind=trigger_kind,
                checked_at=at,
                next_check_at=at + interval,
                duration_ms=_elapsed_ms(started),
                request_etag=request_etag,
                request_last_modified=request_last_modified,
                response=response or _ResponseEvidence(),
                binding=binding,
                error=failure,
            )
            db.commit()
            return _outcome_from_check("recorded", check)

        assert response is not None and binding is not None
        observed: ReferenceStyleObservedVersion | None
        if response.not_modified:
            current_validator = _current_conditional_validator(db, target)
            if (
                current_validator is None
                or current_validator.id != validator_id
            ):
                raise OfficialStyleWatcherError(
                    "HTTP 304 validator became stale during the style check",
                    code="stale_not_modified",
                    retryable=False,
                )
            validator = (
                db.get(ReferenceStyleUpdateCheck, validator_id)
                if validator_id is not None
                else None
            )
            if (
                validator is None
                or validator.source_id != target.source_id
                or validator.source_definition_sha256
                != target.source_definition_sha256
                or validator.status not in _SUCCESS_STATUSES
            ):
                raise OfficialStyleWatcherError(
                    "HTTP 304 has no exact prior style observation",
                    code="orphan_not_modified",
                    retryable=False,
                )
            observed = (
                db.get(
                    ReferenceStyleObservedVersion,
                    validator.observed_version_id,
                )
                if validator.observed_version_id is not None
                else None
            )
            if observed is not None:
                _verify_observed_blob(store, observed)
        else:
            assert observed_document is not None
            observed = _persist_candidate_if_changed(
                db,
                store=store,
                target=target,
                response=response,
                document=observed_document,
                body=body,
                binding=binding,
                checked_at=at,
            )

        status: Literal["unchanged", "style_review_required"] = (
            "unchanged"
            if observed is None or _candidate_is_resolved(db, observed)
            else "style_review_required"
        )
        check = ReferenceStyleUpdateCheck(
            id=check_id,
            provider_key=target.provider_key,
            layer_id=target.layer_id,
            source_id=target.source_id,
            source_definition_sha256=target.source_definition_sha256,
            profile=target.profile,
            idempotency_key=key,
            trigger_kind=trigger_kind,
            source_url=target.official_url,
            baseline_raw_sha256=target.baseline_raw_sha256,
            baseline_semantic_sha256=target.baseline_semantic_sha256,
            observed_version_id=observed.id if observed is not None else None,
            authorization_review_id=binding.review_id,
            authorization_review_sha256=binding.review_sha256,
            status=status,
            checked_at=at,
            next_check_at=at + interval,
            duration_ms=_elapsed_ms(started),
            request_etag=request_etag,
            request_last_modified=request_last_modified,
            http_status=response.http_status,
            not_modified=response.not_modified,
            response_final_url=response.final_url,
            response_etag=response.etag,
            response_last_modified=response.last_modified,
            response_size_bytes=response.size_bytes,
            response_raw_sha256=response.raw_sha256,
            response_redirect_chain_json=list(response.redirect_chain),
        )
        db.add(check)
        db.commit()
        db.refresh(check)
        return _outcome_from_check("recorded", check)
    except OfficialStyleWatcherError as error:
        db.rollback()
        # Validation failures discovered only while resolving a 304 must still
        # become durable evidence.  Reacquire the same transaction lock.
        _acquire_style_persistence_lock(db, target)
        duplicate = _check_by_key(db, target, key)
        if duplicate is not None:
            outcome = _outcome_from_check("duplicate", duplicate)
            db.commit()
            return outcome
        check = _record_error_check(
            db,
            target=target,
            check_id=check_id,
            key=key,
            trigger_kind=trigger_kind,
            checked_at=at,
            next_check_at=at + interval,
            duration_ms=_elapsed_ms(started),
            request_etag=request_etag,
            request_last_modified=request_last_modified,
            response=response or _ResponseEvidence(),
            binding=binding,
            error=error,
        )
        db.commit()
        return _outcome_from_check("recorded", check)
    except Exception:
        db.rollback()
        raise


def require_official_style_promotion_allowed(
    db: Session,
    *,
    source: ReferenceLayerSource,
    store: ReferenceBlobStore | None,
) -> None:
    """Fail closed on an unreviewed or physically unverifiable candidate."""

    target = style_watch_target_for_source(source)
    if target is None:
        return
    latest = _latest_conclusive_check(db, target)
    if latest is None:
        return
    if latest.status == "error":
        raise OfficialStyleReviewRequiredError(
            "official style changed but its candidate could not be validated "
            "and retained"
        )
    if latest.observed_version_id is None:
        if latest.status == "unchanged":
            return
        raise OfficialStyleReviewRequiredError(
            "official style candidate evidence is unavailable"
        )
    observed = db.get(
        ReferenceStyleObservedVersion,
        latest.observed_version_id,
    )
    if observed is None or store is None:
        raise OfficialStyleReviewRequiredError(
            "official style candidate evidence is unavailable"
        )
    try:
        _verify_observed_blob(store, observed)
    except OfficialStyleWatcherError as error:
        raise OfficialStyleReviewRequiredError(
            "official style candidate evidence failed integrity validation"
        ) from error
    if _candidate_is_resolved(db, observed):
        return
    raise OfficialStyleReviewRequiredError(
        "official style changed and requires explicit review"
    )


def pending_official_style_review_status(
    db: Session,
    *,
    store: ReferenceBlobStore,
    source_id: int | None = None,
) -> dict[str, object]:
    """List current promotion blockers and complete operator templates."""

    if source_id is not None and (
        isinstance(source_id, bool)
        or not isinstance(source_id, int)
        or source_id <= 0
    ):
        raise StyleUpdateReviewDocumentError("source_id is invalid")
    query = select(ReferenceLayerSource).order_by(ReferenceLayerSource.id)
    if source_id is None:
        query = query.where(ReferenceLayerSource.enabled.is_(True))
    else:
        query = query.where(ReferenceLayerSource.id == source_id)
    sources = tuple(db.scalars(query))
    if source_id is not None and not sources:
        raise StyleUpdateReviewDocumentError(
            "reference style source does not exist"
        )

    pending: list[dict[str, object]] = []
    for source in sources:
        target = style_watch_target_for_source(source)
        if target is None:
            continue
        successful = _latest_successful_check(db, target)
        if (
            successful is None
            or successful.observed_version_id is None
        ):
            continue
        observed = db.get(
            ReferenceStyleObservedVersion,
            successful.observed_version_id,
        )
        if observed is None:
            raise StyleUpdateReviewDocumentError(
                "pending style candidate evidence is inconsistent"
            )
        resolved = _candidate_is_resolved(db, observed)
        try:
            _verify_observed_blob(store, observed)
            candidate_integrity = "verified"
            candidate_integrity_error = None
        except OfficialStyleWatcherError as error:
            candidate_integrity = "invalid"
            candidate_integrity_error = error.code
        if resolved and candidate_integrity == "verified":
            continue
        latest = _latest_check(db, target)
        review = db.scalar(
            select(ReferenceStyleUpdateReview).where(
                ReferenceStyleUpdateReview.observed_version_id
                == observed.id
            )
        )
        if review is None:
            review_state = "unreviewed"
        elif stored_style_update_review_is_valid(review):
            review_state = review.decision
        else:
            review_state = "invalid_review"
        template: dict[str, object] | None = None
        if (
            review is None
            and successful.status == "style_review_required"
            and candidate_integrity == "verified"
        ):
            template = {
                "schema_version": STYLE_REVIEW_SCHEMA,
                "provider_key": target.provider_key,
                "layer_id": target.layer_id,
                "source_id": target.source_id,
                "source_definition_sha256": (
                    target.source_definition_sha256
                ),
                "profile": target.profile,
                "official_style_url": target.official_url,
                "baseline_raw_sha256": target.baseline_raw_sha256,
                "baseline_semantic_sha256": (
                    target.baseline_semantic_sha256
                ),
                "observed_version_id": observed.id,
                "observed_raw_sha256": observed.raw_sha256,
                "observed_semantic_sha256": observed.semantic_sha256,
                "decision": None,
                "reviewer": None,
                "reviewed_at": None,
                "rationale": None,
            }
        pending.append(
            {
                "provider_key": target.provider_key,
                "layer_id": target.layer_id,
                "source_id": target.source_id,
                "source_definition_sha256": (
                    target.source_definition_sha256
                ),
                "profile": target.profile,
                "official_style_url": target.official_url,
                "baseline_raw_sha256": target.baseline_raw_sha256,
                "baseline_semantic_sha256": (
                    target.baseline_semantic_sha256
                ),
                "latest_check_id": (
                    latest.id if latest is not None else successful.id
                ),
                "latest_check_status": (
                    latest.status
                    if latest is not None
                    else successful.status
                ),
                "candidate_check_id": successful.id,
                "observed_version_id": observed.id,
                "observed_raw_sha256": observed.raw_sha256,
                "observed_semantic_sha256": observed.semantic_sha256,
                "observed_at": _utc_isoformat(observed.retrieved_at),
                "candidate_integrity": candidate_integrity,
                "candidate_integrity_error": candidate_integrity_error,
                "blocking_reason": (
                    "style_candidate_integrity"
                    if candidate_integrity != "verified"
                    else "style_review_required"
                ),
                "review_state": review_state,
                "review_id": review.id if review is not None else None,
                "review_sha256": (
                    review.review_sha256 if review is not None else None
                ),
                "review_document_template": template,
            }
        )
    return {
        "ok": True,
        "mode": "status",
        "source_id": source_id,
        "pending_count": len(pending),
        "pending_candidates": pending,
    }


def parse_style_update_review(
    document: bytes,
) -> StyleUpdateReviewEvidence:
    """Parse one exact, strict review document without touching the network."""

    if (
        not isinstance(document, bytes)
        or not 1 <= len(document) <= MAX_STYLE_REVIEW_DOCUMENT_BYTES
    ):
        raise StyleUpdateReviewDocumentError(
            "style review document exceeds its byte limit"
        )
    value = _strict_json_object(document)
    if set(value) != _REVIEW_KEYS:
        raise StyleUpdateReviewDocumentError(
            "style review document does not match its strict schema"
        )
    if value.get("schema_version") != STYLE_REVIEW_SCHEMA:
        raise StyleUpdateReviewDocumentError(
            "style review schema version is unsupported"
        )
    provider_key = _required_text(value, "provider_key", 64)
    profile = _required_text(value, "profile", 128)
    official_url = _required_https_url(
        value.get("official_style_url"),
        "official_style_url",
    )
    source_definition_sha256 = _required_sha256(
        value.get("source_definition_sha256"),
        "source_definition_sha256",
    )
    baseline_raw_sha256 = _required_sha256(
        value.get("baseline_raw_sha256"),
        "baseline_raw_sha256",
    )
    baseline_semantic_sha256 = _required_sha256(
        value.get("baseline_semantic_sha256"),
        "baseline_semantic_sha256",
    )
    observed_raw_sha256 = _required_sha256(
        value.get("observed_raw_sha256"),
        "observed_raw_sha256",
    )
    observed_semantic_sha256 = _required_sha256(
        value.get("observed_semantic_sha256"),
        "observed_semantic_sha256",
    )
    layer_id = _positive_integer(value.get("layer_id"), "layer_id")
    source_id = _positive_integer(value.get("source_id"), "source_id")
    observed_version_id = _positive_integer(
        value.get("observed_version_id"),
        "observed_version_id",
    )
    decision = value.get("decision")
    if decision not in {"retain_vendored", "vendor_update_required"}:
        raise StyleUpdateReviewDocumentError(
            "style review decision is invalid"
        )
    reviewer = _required_text(value, "reviewer", 255)
    rationale = _required_text(value, "rationale", 4096)
    reviewed_at = _review_datetime(value.get("reviewed_at"))
    review_sha256 = _canonical_json_sha256(value)
    return StyleUpdateReviewEvidence(
        raw_document=document,
        document_sha256=hashlib.sha256(document).hexdigest(),
        review_sha256=review_sha256,
        provider_key=provider_key,
        layer_id=layer_id,
        source_id=source_id,
        source_definition_sha256=source_definition_sha256,
        profile=profile,
        official_style_url=official_url,
        baseline_raw_sha256=baseline_raw_sha256,
        baseline_semantic_sha256=baseline_semantic_sha256,
        observed_version_id=observed_version_id,
        observed_raw_sha256=observed_raw_sha256,
        observed_semantic_sha256=observed_semantic_sha256,
        decision=cast(Any, decision),
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        rationale=rationale,
    )


def plan_style_update_review(
    db: Session,
    document: bytes,
    *,
    store: ReferenceBlobStore,
    lock: bool = False,
) -> StyleUpdateReviewPlan:
    """Validate one review against the exact current pending candidate."""

    evidence = parse_style_update_review(document)
    query = select(ReferenceLayerSource).where(
        ReferenceLayerSource.id == evidence.source_id,
        ReferenceLayerSource.provider_key == evidence.provider_key,
        ReferenceLayerSource.layer_id == evidence.layer_id,
    )
    source = db.scalar(query)
    if source is None:
        raise StyleUpdateReviewDocumentError(
            "style review source does not exist"
        )
    target = style_watch_target_for_source(source)
    if target is None:
        raise StyleUpdateReviewDocumentError(
            "style review source is not currently watched"
        )
    if lock:
        db.scalar(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _style_watcher_lock_key(target)},
        )
        db.scalar(
            text(
                "SELECT set_config("
                "'lock_timeout', '0', true"
                ")"
            )
        )
        current_source = db.scalar(query.with_for_update())
        current_target = (
            style_watch_target_for_source(current_source)
            if current_source is not None
            else None
        )
        if current_target != target:
            raise StyleUpdateReviewDocumentError(
                "style review source changed while acquiring its lock"
            )
        assert current_source is not None and current_target is not None
        source = current_source
        target = current_target
    observed = db.get(
        ReferenceStyleObservedVersion,
        evidence.observed_version_id,
    )
    if (
        observed is None
        or not _review_matches_candidate(evidence, target, observed)
    ):
        raise StyleUpdateReviewDocumentError(
            "style review does not match the exact staged candidate"
        )
    try:
        _verify_observed_blob(store, observed)
    except OfficialStyleWatcherError as error:
        raise StyleUpdateReviewDocumentError(
            "style candidate CAS evidence failed integrity validation"
        ) from error
    existing = db.scalar(
        select(ReferenceStyleUpdateReview).where(
            ReferenceStyleUpdateReview.observed_version_id == observed.id
        )
    )
    if existing is not None:
        if (
            existing.document_sha256 != evidence.document_sha256
            or existing.review_sha256 != evidence.review_sha256
            or not stored_style_update_review_is_valid(existing)
        ):
            raise StyleUpdateReviewDocumentError(
                "style candidate already has a different review"
            )
        return StyleUpdateReviewPlan(evidence, existing.id)
    latest = _latest_successful_check(db, target)
    if (
        latest is None
        or latest.status != "style_review_required"
        or latest.observed_version_id != observed.id
    ):
        raise StyleUpdateReviewDocumentError(
            "style candidate is no longer the current promotion blocker"
        )
    return StyleUpdateReviewPlan(evidence, None)


def apply_style_update_review(
    db: Session,
    document: bytes,
    *,
    store: ReferenceBlobStore,
    expected_review_sha256: str,
    expected_document_sha256: str,
) -> ReferenceStyleUpdateReview:
    """Append a human decision bound to one exact staged candidate."""

    expected_review = _required_sha256(
        expected_review_sha256,
        "expected_review_sha256",
    )
    expected_document = _required_sha256(
        expected_document_sha256,
        "expected_document_sha256",
    )
    try:
        plan = plan_style_update_review(
            db,
            document,
            store=store,
            lock=True,
        )
        evidence = plan.evidence
        if evidence.review_sha256 != expected_review:
            raise StyleUpdateReviewDocumentError(
                "style review hash changed after dry-run"
            )
        if evidence.document_sha256 != expected_document:
            raise StyleUpdateReviewDocumentError(
                "style review document hash changed after dry-run"
            )
        if plan.already_applied_id is not None:
            existing = db.get(
                ReferenceStyleUpdateReview,
                plan.already_applied_id,
            )
            if (
                existing is None
                or not stored_style_update_review_is_valid(existing)
            ):
                raise StyleUpdateReviewDocumentError(
                    "stored style review is invalid"
                )
            db.commit()
            return existing
        review = ReferenceStyleUpdateReview(
            provider_key=evidence.provider_key,
            layer_id=evidence.layer_id,
            source_id=evidence.source_id,
            source_definition_sha256=evidence.source_definition_sha256,
            profile=evidence.profile,
            source_url=evidence.official_style_url,
            baseline_raw_sha256=evidence.baseline_raw_sha256,
            baseline_semantic_sha256=evidence.baseline_semantic_sha256,
            observed_version_id=evidence.observed_version_id,
            observed_raw_sha256=evidence.observed_raw_sha256,
            observed_semantic_sha256=evidence.observed_semantic_sha256,
            decision=evidence.decision,
            reviewer=evidence.reviewer,
            reviewed_at=evidence.reviewed_at,
            rationale=evidence.rationale,
            reviewed_document=evidence.raw_document,
            document_size_bytes=len(evidence.raw_document),
            document_sha256=evidence.document_sha256,
            review_sha256=evidence.review_sha256,
        )
        db.add(review)
        db.flush()
        db.commit()
        return review
    except Exception:
        db.rollback()
        raise


def _review_matches_candidate(
    evidence: StyleUpdateReviewEvidence,
    target: StyleWatchTarget,
    observed: ReferenceStyleObservedVersion,
) -> bool:
    return (
        evidence.provider_key == target.provider_key
        and evidence.layer_id == target.layer_id
        and evidence.source_id == target.source_id
        and evidence.source_definition_sha256
        == target.source_definition_sha256
        and evidence.profile == target.profile
        and evidence.official_style_url == target.official_url
        and evidence.baseline_raw_sha256 == target.baseline_raw_sha256
        and evidence.baseline_semantic_sha256
        == target.baseline_semantic_sha256
        and observed.provider_key == target.provider_key
        and observed.layer_id == target.layer_id
        and observed.source_id == target.source_id
        and observed.source_definition_sha256
        == target.source_definition_sha256
        and observed.profile == target.profile
        and observed.source_url == target.official_url
        and evidence.observed_version_id == observed.id
        and evidence.observed_raw_sha256 == observed.raw_sha256
        and evidence.observed_semantic_sha256 == observed.semantic_sha256
        and evidence.reviewed_at >= _aware_utc(observed.retrieved_at)
    )


def stored_style_update_review_is_valid(
    review: ReferenceStyleUpdateReview,
) -> bool:
    try:
        evidence = parse_style_update_review(bytes(review.reviewed_document))
    except (StyleUpdateReviewDocumentError, TypeError, ValueError):
        return False
    return (
        review.document_size_bytes == len(evidence.raw_document)
        and review.document_sha256 == evidence.document_sha256
        and review.review_sha256 == evidence.review_sha256
        and review.provider_key == evidence.provider_key
        and review.layer_id == evidence.layer_id
        and review.source_id == evidence.source_id
        and review.source_definition_sha256
        == evidence.source_definition_sha256
        and review.profile == evidence.profile
        and review.source_url == evidence.official_style_url
        and review.baseline_raw_sha256 == evidence.baseline_raw_sha256
        and review.baseline_semantic_sha256
        == evidence.baseline_semantic_sha256
        and review.observed_version_id == evidence.observed_version_id
        and review.observed_raw_sha256 == evidence.observed_raw_sha256
        and review.observed_semantic_sha256
        == evidence.observed_semantic_sha256
        and review.decision == evidence.decision
        and review.reviewer == evidence.reviewer
        and _aware_utc(review.reviewed_at) == evidence.reviewed_at
        and review.rationale == evidence.rationale
    )


def run_official_style_update_check_job(
    *,
    session_factory=SessionLocal,
    config: Settings = settings,
) -> dict[str, object]:
    """Scheduler entrypoint; due state remains per source and per exact URL."""

    with session_factory() as db:
        sources = tuple(
            db.scalars(
                select(ReferenceLayerSource)
                .where(
                    ReferenceLayerSource.enabled.is_(True),
                    ReferenceLayerSource.is_primary.is_(True),
                )
                .order_by(ReferenceLayerSource.id)
            )
        )
        source_ids = tuple(
            source.id
            for source in sources
            if style_watch_target_for_source(source) is not None
        )
        db.commit()
    store = ReferenceBlobStore(
        config.reference_storage_root,
        max_blob_bytes=config.reference_blob_max_bytes,
        quota_bytes=config.reference_storage_quota_bytes,
        min_free_bytes=config.reference_storage_min_free_bytes,
    )
    outcomes: list[StyleUpdateCheckOutcome] = []
    try:
        for source_id in source_ids:
            with session_factory() as db:
                outcomes.append(
                    check_official_style_update(
                        db,
                        source_id=source_id,
                        store=store,
                    )
                )
    finally:
        store.close()
    return {
        "source_count": len(source_ids),
        "recorded_count": sum(
            item.disposition == "recorded" for item in outcomes
        ),
        "review_required_count": sum(
            item.status == "style_review_required" for item in outcomes
        ),
        "error_count": sum(item.status == "error" for item in outcomes),
        "checks": [item.as_dict() for item in outcomes],
    }


def _style_watch_target(
    source: ReferenceLayerSource,
    target: ReviewedMitecoStyleWatchTarget,
) -> StyleWatchTarget:
    return StyleWatchTarget(
        provider_key=source.provider_key,
        layer_id=source.layer_id,
        source_id=source.id,
        source_definition_sha256=source.definition_sha256,
        profile=target.profile,
        official_url=target.official_url,
        baseline_raw_sha256=target.baseline_raw_sha256,
        baseline_semantic_sha256=target.baseline_semantic_sha256,
    )


def _authorization_binding(
    review: ReferenceMirrorAuthorizationReview,
) -> _AuthorizationBinding:
    return _AuthorizationBinding(
        review_id=review.id,
        review_sha256=review.review_sha256,
        allow_local_storage=review.allow_local_storage,
    )


def _validate_download_result(
    target: StyleWatchTarget,
    result: HTTPSDownloadResult,
    body: bytes,
    *,
    validator_id: int | None,
) -> _ResponseEvidence:
    if (
        result.source_url != target.official_url
        or result.final_url != target.official_url
        or result.redirects != 0
        or result.redirect_chain != (target.official_url,)
    ):
        raise OfficialStyleWatcherError(
            "style downloader returned an unexpected URL chain",
            code="style_download_result_mismatch",
            retryable=False,
        )
    if result.not_modified:
        if (
            validator_id is None
            or result.status_code != 304
            or body
            or result.size_bytes != 0
            or result.sha256 is not None
        ):
            raise OfficialStyleWatcherError(
                "style downloader returned an invalid HTTP 304",
                code="style_download_result_mismatch",
                retryable=False,
            )
        raw_sha256 = None
    else:
        raw_sha256 = hashlib.sha256(body).hexdigest()
        if (
            result.status_code != 200
            or not body
            or result.size_bytes != len(body)
            or result.sha256 != raw_sha256
            or len(body) > MAX_EVIDENCE_BYTES
            or result.content_type not in _STYLE_CONTENT_TYPES
        ):
            raise OfficialStyleWatcherError(
                "style downloader returned inconsistent response evidence",
                code="style_download_result_mismatch",
                retryable=False,
            )
    return _ResponseEvidence(
        http_status=result.status_code,
        not_modified=result.not_modified,
        final_url=result.final_url,
        etag=result.etag,
        last_modified=result.last_modified,
        size_bytes=result.size_bytes,
        raw_sha256=raw_sha256,
        redirect_chain=result.redirect_chain,
    )


def _response_from_failure(
    error: BaseException,
    *,
    target: StyleWatchTarget,
    result: HTTPSDownloadResult | None,
    body: bytes,
) -> _ResponseEvidence:
    status_code = getattr(error, "status_code", None)
    if result is not None:
        status_code = result.status_code
    safe_body = body[:MAX_EVIDENCE_BYTES]
    return _ResponseEvidence(
        http_status=(
            status_code
            if isinstance(status_code, int) and 100 <= status_code <= 599
            else None
        ),
        final_url=(
            target.official_url
            if result is not None
            and result.final_url == target.official_url
            else None
        ),
        etag=(
            result.etag
            if result is not None and result.final_url == target.official_url
            else None
        ),
        last_modified=(
            result.last_modified
            if result is not None and result.final_url == target.official_url
            else None
        ),
        size_bytes=len(safe_body),
        raw_sha256=(
            hashlib.sha256(safe_body).hexdigest() if safe_body else None
        ),
        redirect_chain=(
            result.redirect_chain
            if result is not None
            and result.redirect_chain == (target.official_url,)
            else ()
        ),
    )


def _persist_candidate_if_changed(
    db: Session,
    *,
    store: ReferenceBlobStore,
    target: StyleWatchTarget,
    response: _ResponseEvidence,
    document: ObservedMitecoStyleDocument,
    body: bytes,
    binding: _AuthorizationBinding,
    checked_at: datetime,
) -> ReferenceStyleObservedVersion | None:
    if (
        document.raw_sha256 == target.baseline_raw_sha256
        and document.matches_vendored_bytes
    ):
        return None
    if not binding.allow_local_storage:
        raise OfficialStyleWatcherError(
            "authorization allows probing but not candidate retention",
            code="mirror_authorization_storage_restricted",
            retryable=False,
        )
    existing = db.scalar(
        select(ReferenceStyleObservedVersion).where(
            ReferenceStyleObservedVersion.source_id == target.source_id,
            ReferenceStyleObservedVersion.source_definition_sha256
            == target.source_definition_sha256,
            ReferenceStyleObservedVersion.source_url == target.official_url,
            ReferenceStyleObservedVersion.raw_sha256 == document.raw_sha256,
        )
    )
    if existing is not None:
        _verify_observed_blob(store, existing)
        if (
            existing.provider_key != target.provider_key
            or existing.layer_id != target.layer_id
            or existing.profile != target.profile
            or existing.semantic_sha256 != document.semantic_sha256
            or existing.size_bytes != len(body)
        ):
            raise OfficialStyleWatcherError(
                "stored style candidate identity is inconsistent",
                code="style_candidate_integrity",
            )
        return existing
    try:
        blob = store.put_stream(
            BytesIO(body),
            max_bytes=MAX_EVIDENCE_BYTES,
            expected_sha256=document.raw_sha256,
            expected_size=document.size_bytes,
        )
    except ReferenceBlobStoreError as error:
        raise OfficialStyleWatcherError(
            "style candidate could not be retained in local storage",
            code="style_candidate_storage",
            retryable=True,
        ) from error
    observed = ReferenceStyleObservedVersion(
        provider_key=target.provider_key,
        layer_id=target.layer_id,
        source_id=target.source_id,
        source_definition_sha256=target.source_definition_sha256,
        profile=target.profile,
        source_url=target.official_url,
        final_url=response.final_url or target.official_url,
        raw_sha256=document.raw_sha256,
        semantic_sha256=document.semantic_sha256,
        size_bytes=document.size_bytes,
        storage_backend=blob.storage_backend,
        storage_key=blob.storage_key,
        semantic_summary_json={
            "schema": "siur-official-style-observation/v1",
            "strict_json": True,
            "matches_vendored_bytes": document.matches_vendored_bytes,
            "matches_vendored_semantics": (
                document.matches_vendored_semantics
            ),
            "baseline_raw_sha256": target.baseline_raw_sha256,
            "baseline_semantic_sha256": target.baseline_semantic_sha256,
        },
        retrieved_at=checked_at,
    )
    db.add(observed)
    db.flush()
    return observed


def _verify_observed_blob(
    store: ReferenceBlobStore,
    observed: ReferenceStyleObservedVersion,
) -> None:
    try:
        with store.open_blob(observed.storage_key) as stream:
            body = stream.read(MAX_EVIDENCE_BYTES + 1)
    except (OSError, ReferenceBlobStoreError) as error:
        raise OfficialStyleWatcherError(
            "stored style candidate is unavailable",
            code="style_candidate_integrity",
        ) from error
    if (
        len(body) != observed.size_bytes
        or len(body) > MAX_EVIDENCE_BYTES
        or hashlib.sha256(body).hexdigest() != observed.raw_sha256
    ):
        raise OfficialStyleWatcherError(
            "stored style candidate failed its CAS identity",
            code="style_candidate_integrity",
        )


def _candidate_is_resolved(
    db: Session,
    observed: ReferenceStyleObservedVersion,
) -> bool:
    review = db.scalar(
        select(ReferenceStyleUpdateReview).where(
            ReferenceStyleUpdateReview.observed_version_id == observed.id
        )
    )
    return bool(
        review is not None
        and review.decision == "retain_vendored"
        and stored_style_update_review_is_valid(review)
        and review.provider_key == observed.provider_key
        and review.layer_id == observed.layer_id
        and review.source_id == observed.source_id
        and review.source_definition_sha256
        == observed.source_definition_sha256
        and review.profile == observed.profile
        and review.source_url == observed.source_url
        and review.observed_raw_sha256 == observed.raw_sha256
        and review.observed_semantic_sha256 == observed.semantic_sha256
    )


def _record_error_check(
    db: Session,
    *,
    target: StyleWatchTarget,
    check_id: int,
    key: str,
    trigger_kind: str,
    checked_at: datetime,
    next_check_at: datetime,
    duration_ms: int,
    request_etag: str | None,
    request_last_modified: str | None,
    response: _ResponseEvidence,
    binding: _AuthorizationBinding | None,
    error: BaseException,
) -> ReferenceStyleUpdateCheck:
    code = str(getattr(error, "code", "style_check_error"))[:64]
    message = str(error).strip()[:4096] or "official style check failed"
    check = ReferenceStyleUpdateCheck(
        id=check_id,
        provider_key=target.provider_key,
        layer_id=target.layer_id,
        source_id=target.source_id,
        source_definition_sha256=target.source_definition_sha256,
        profile=target.profile,
        idempotency_key=key,
        trigger_kind=trigger_kind,
        source_url=target.official_url,
        baseline_raw_sha256=target.baseline_raw_sha256,
        baseline_semantic_sha256=target.baseline_semantic_sha256,
        observed_version_id=None,
        authorization_review_id=(
            binding.review_id if binding is not None else None
        ),
        authorization_review_sha256=(
            binding.review_sha256 if binding is not None else None
        ),
        status="error",
        checked_at=checked_at,
        next_check_at=next_check_at,
        duration_ms=duration_ms,
        request_etag=request_etag,
        request_last_modified=request_last_modified,
        http_status=response.http_status,
        not_modified=False,
        response_final_url=response.final_url,
        response_etag=response.etag,
        response_last_modified=response.last_modified,
        response_size_bytes=response.size_bytes,
        response_raw_sha256=response.raw_sha256,
        response_redirect_chain_json=list(response.redirect_chain),
        error_code=code,
        error_message=message,
        error_retryable=bool(getattr(error, "retryable", False)),
    )
    db.add(check)
    db.flush()
    return check


def _latest_check(
    db: Session,
    target: StyleWatchTarget,
) -> ReferenceStyleUpdateCheck | None:
    return db.scalar(
        select(ReferenceStyleUpdateCheck)
        .where(
            ReferenceStyleUpdateCheck.source_id == target.source_id,
            ReferenceStyleUpdateCheck.source_definition_sha256
            == target.source_definition_sha256,
            ReferenceStyleUpdateCheck.source_url == target.official_url,
        )
        .order_by(ReferenceStyleUpdateCheck.id.desc())
        .limit(1)
    )


def _latest_successful_check(
    db: Session,
    target: StyleWatchTarget,
) -> ReferenceStyleUpdateCheck | None:
    return db.scalar(
        select(ReferenceStyleUpdateCheck)
        .where(
            ReferenceStyleUpdateCheck.source_id == target.source_id,
            ReferenceStyleUpdateCheck.source_definition_sha256
            == target.source_definition_sha256,
            ReferenceStyleUpdateCheck.source_url == target.official_url,
            ReferenceStyleUpdateCheck.status.in_(_SUCCESS_STATUSES),
        )
        .order_by(ReferenceStyleUpdateCheck.id.desc())
        .limit(1)
    )


def _latest_conclusive_check(
    db: Session,
    target: StyleWatchTarget,
) -> ReferenceStyleUpdateCheck | None:
    """Use response order; a 304 inherits state and cannot clear a raw change."""

    return db.scalar(
        select(ReferenceStyleUpdateCheck)
        .where(
            ReferenceStyleUpdateCheck.source_id == target.source_id,
            ReferenceStyleUpdateCheck.source_definition_sha256
            == target.source_definition_sha256,
            ReferenceStyleUpdateCheck.profile == target.profile,
            ReferenceStyleUpdateCheck.source_url == target.official_url,
            ReferenceStyleUpdateCheck.baseline_raw_sha256
            == target.baseline_raw_sha256,
            ReferenceStyleUpdateCheck.baseline_semantic_sha256
            == target.baseline_semantic_sha256,
            or_(
                and_(
                    ReferenceStyleUpdateCheck.status.in_(
                        _SUCCESS_STATUSES
                    ),
                    ReferenceStyleUpdateCheck.http_status == 200,
                ),
                and_(
                    ReferenceStyleUpdateCheck.status == "error",
                    ReferenceStyleUpdateCheck.http_status == 200,
                    ReferenceStyleUpdateCheck.not_modified.is_(False),
                    ReferenceStyleUpdateCheck.response_final_url
                    == target.official_url,
                    ReferenceStyleUpdateCheck.response_size_bytes > 0,
                    ReferenceStyleUpdateCheck.response_raw_sha256.is_not(
                        None
                    ),
                    ReferenceStyleUpdateCheck.response_raw_sha256
                    != target.baseline_raw_sha256,
                ),
            ),
        )
        .order_by(ReferenceStyleUpdateCheck.id.desc())
        .limit(1)
    )


def _current_conditional_validator(
    db: Session,
    target: StyleWatchTarget,
) -> ReferenceStyleUpdateCheck | None:
    """Return evidence that can still justify a conditional HTTP request."""

    latest = _latest_conclusive_check(db, target)
    if (
        latest is None
        or latest.status not in _SUCCESS_STATUSES
        or (
            latest.response_etag is None
            and latest.response_last_modified is None
        )
    ):
        return None
    return latest


def _check_by_key(
    db: Session,
    target: StyleWatchTarget,
    key: str,
) -> ReferenceStyleUpdateCheck | None:
    return db.scalar(
        select(ReferenceStyleUpdateCheck).where(
            ReferenceStyleUpdateCheck.source_id == target.source_id,
            ReferenceStyleUpdateCheck.source_definition_sha256
            == target.source_definition_sha256,
            ReferenceStyleUpdateCheck.idempotency_key == key,
        )
    )


def _outcome_from_check(
    disposition: Literal["recorded", "duplicate", "not_due"],
    check: ReferenceStyleUpdateCheck,
) -> StyleUpdateCheckOutcome:
    return StyleUpdateCheckOutcome(
        disposition=disposition,
        status=cast(Any, check.status),
        check_id=check.id,
        source_id=check.source_id,
        observed_version_id=check.observed_version_id,
        next_check_at=check.next_check_at,
    )


def _style_watcher_lock_key(target: StyleWatchTarget) -> int:
    identity = (
        f"{target.provider_key}\0{target.source_id}\0"
        f"{target.source_definition_sha256}\0{target.official_url}"
    )
    digest = hashlib.sha256(
        _STYLE_WATCHER_LOCK_DOMAIN + identity.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _reserve_style_check_id(db: Session) -> int:
    """Reserve response order before waiting on persistence serialization."""

    value = db.scalar(
        text(
            "SELECT nextval("
            "pg_get_serial_sequence("
            "'reference_style_update_checks', 'id'"
            ")"
            ")"
        )
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        db.rollback()
        raise RuntimeError("official style check sequence is unavailable")
    return value


def _acquire_style_persistence_lock(
    db: Session,
    target: StyleWatchTarget,
) -> None:
    """Wait a bounded time so downloaded evidence is not silently dropped."""

    try:
        db.scalar(
            text(
                "SELECT set_config("
                "'lock_timeout', :lock_timeout, true"
                ")"
            ),
            {
                "lock_timeout": (
                    f"{_STYLE_PERSISTENCE_LOCK_TIMEOUT_MS}ms"
                )
            },
        )
        db.scalar(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _style_watcher_lock_key(target)},
        )
        db.scalar(
            text("SELECT set_config('lock_timeout', '0', true)")
        )
    except DBAPIError as error:
        db.rollback()
        if getattr(error.orig, "sqlstate", None) == "55P03":
            raise OfficialStyleWatcherError(
                "official style evidence persistence lock timed out",
                code="style_persistence_lock_timeout",
                retryable=True,
            ) from error
        raise


def _is_changed_200_response(
    target: StyleWatchTarget,
    response: _ResponseEvidence | None,
) -> bool:
    return bool(
        response is not None
        and response.http_status == 200
        and response.not_modified is False
        and response.final_url == target.official_url
        and response.size_bytes > 0
        and response.raw_sha256 is not None
        and response.raw_sha256 != target.baseline_raw_sha256
    )


def _record_contended_changed_response(
    db: Session,
    *,
    target: StyleWatchTarget,
    check_id: int,
    original_key: str,
    trigger_kind: str,
    checked_at: datetime,
    retry_at: datetime,
    duration_ms: int,
    request_etag: str | None,
    request_last_modified: str | None,
    response: _ResponseEvidence,
    binding: _AuthorizationBinding | None,
    error: OfficialStyleWatcherError,
) -> StyleUpdateCheckOutcome:
    """Persist a fail-closed retry marker when serialization times out."""

    digest = hashlib.sha256(
        _STYLE_WATCHER_LOCK_DOMAIN
        + b"persistence-contention\0"
        + original_key.encode("utf-8")
        + b"\0"
        + cast(str, response.raw_sha256).encode("ascii")
    ).hexdigest()
    key = f"contention:{digest}"
    existing = _check_by_key(db, target, key)
    if existing is not None:
        outcome = _outcome_from_check("duplicate", existing)
        db.commit()
        return outcome
    try:
        check = _record_error_check(
            db,
            target=target,
            check_id=check_id,
            key=key,
            trigger_kind=trigger_kind,
            checked_at=checked_at,
            next_check_at=retry_at,
            duration_ms=duration_ms,
            request_etag=request_etag,
            request_last_modified=request_last_modified,
            response=response,
            binding=binding,
            error=error,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _check_by_key(db, target, key)
        if existing is None:
            raise
        outcome = _outcome_from_check("duplicate", existing)
        db.commit()
        return outcome
    return _outcome_from_check("recorded", check)


def _concurrent_response_key(
    original_key: str,
    *,
    duplicate: ReferenceStyleUpdateCheck,
    response: _ResponseEvidence | None,
    failure: BaseException | None,
) -> str | None:
    """Retain a distinct concurrent HTTP 200 despite a key collision."""

    if (
        response is None
        or response.http_status != 200
        or response.not_modified
        or response.raw_sha256 is None
    ):
        return None
    same_response = (
        duplicate.http_status == 200
        and duplicate.not_modified is False
        and duplicate.response_raw_sha256 == response.raw_sha256
    )
    if same_response and not (
        duplicate.status == "error" and failure is None
    ):
        return None
    digest = hashlib.sha256(
        _STYLE_WATCHER_LOCK_DOMAIN
        + b"concurrent-idempotency\0"
        + original_key.encode("utf-8")
        + b"\0"
        + response.raw_sha256.encode("ascii")
    ).hexdigest()
    return f"concurrent:{digest}"


def _validated_interval(value: int) -> timedelta:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 300 <= value <= 86_400
    ):
        raise ValueError("interval_seconds must be between 300 and 86400")
    return timedelta(seconds=value)


def _validated_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 160 or "\x00" in normalized:
        raise ValueError("idempotency_key is invalid")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("style check timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _strict_json_object(document: bytes) -> dict[str, Any]:
    if document.startswith(b"\xef\xbb\xbf"):
        raise StyleUpdateReviewDocumentError(
            "style review document has a UTF-8 BOM"
        )

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise StyleUpdateReviewDocumentError(
                    "style review document contains a duplicate key"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise StyleUpdateReviewDocumentError(
            f"style review document contains invalid constant {value}"
        )

    try:
        value = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        if isinstance(error, StyleUpdateReviewDocumentError):
            raise
        raise StyleUpdateReviewDocumentError(
            "style review document is not strict UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise StyleUpdateReviewDocumentError(
            "style review document root must be an object"
        )
    return value


def _required_text(
    value: dict[str, Any],
    key: str,
    maximum: int,
) -> str:
    item = value.get(key)
    if (
        not isinstance(item, str)
        or not item.strip()
        or len(item) > maximum
        or "\x00" in item
    ):
        raise StyleUpdateReviewDocumentError(
            f"style review {key} is invalid"
        )
    return item.strip()


def _required_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise StyleUpdateReviewDocumentError(
            f"style review {name} must be a lowercase SHA-256"
        )
    return value


def _required_https_url(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise StyleUpdateReviewDocumentError(
            f"style review {name} is invalid"
        )
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise StyleUpdateReviewDocumentError(
            f"style review {name} is invalid"
        ) from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
        or parts.fragment
    ):
        raise StyleUpdateReviewDocumentError(
            f"style review {name} is invalid"
        )
    return value


def _positive_integer(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 2**63 - 1
    ):
        raise StyleUpdateReviewDocumentError(
            f"style review {name} is invalid"
        )
    return value


def _review_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise StyleUpdateReviewDocumentError(
            "style review reviewed_at is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StyleUpdateReviewDocumentError(
            "style review reviewed_at is invalid"
        ) from error
    return _aware_utc(parsed)


def _canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise StyleUpdateReviewDocumentError(
            "style review document is not canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _utc_isoformat(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "OfficialStyleReviewRequiredError",
    "OfficialStyleWatcherError",
    "MAX_STYLE_REVIEW_DOCUMENT_BYTES",
    "STYLE_CHECK_INTERVAL_SECONDS",
    "STYLE_REVIEW_SCHEMA",
    "StyleUpdateCheckOutcome",
    "StyleUpdateReviewDocumentError",
    "StyleUpdateReviewPlan",
    "apply_style_update_review",
    "build_official_style_downloader",
    "check_official_style_update",
    "parse_style_update_review",
    "pending_official_style_review_status",
    "plan_style_update_review",
    "require_official_style_promotion_allowed",
    "run_official_style_update_check_job",
    "scheduled_style_check_key",
    "stored_style_update_review_is_valid",
    "style_watch_target_for_source",
]
