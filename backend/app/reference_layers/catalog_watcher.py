"""Durable, review-only update checks for the upstream SIUR catalog.

The zero-argument job at the bottom is deliberately small so any deployment
scheduler can invoke it once a day.  This module downloads and records
observations only: applying a changed catalog remains a separate, explicitly
reviewed operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import logging
from time import monotonic
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.reference_layers.catalog import canonical_catalog_sha256
from app.reference_layers.models import (
    ReferenceCatalogObservedVersion,
    ReferenceCatalogSnapshot,
    ReferenceCatalogUpdateCheck,
)
from app.reference_layers.safe_download import (
    HTTPSDownloadPolicy,
    HTTPSDownloadResult,
    SafeDownloadError,
    SafeHTTPSDownloader,
)
from app.reference_layers.siur_settings import (
    DEFAULT_SOURCE_URL,
    SiurSettingsError,
    SiurSettingsLimits,
    analyze_siur_settings,
)


logger = logging.getLogger(__name__)

SIUR_PROVIDER_KEY = "siur"
SIUR_SOURCE_ORIGIN = "https://idecyl.jcyl.es"
SIUR_CATALOG_CHECK_INTERVAL_SECONDS = 86_400
SIUR_CATALOG_MAX_RESPONSE_BYTES = SiurSettingsLimits().max_bytes
_WATCHER_LOCK_DOMAIN = b"asistente/reference-catalog-watcher/v1\0"
_SUCCESS_STATUSES = ("unchanged", "update_available")


class CatalogDownloader(Protocol):
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
class CatalogUpdateCheckOutcome:
    disposition: Literal["recorded", "duplicate", "not_due", "lock_busy"]
    status: Literal["unchanged", "update_available", "error"] | None
    check_id: int | None
    observed_content_sha256: str | None
    next_check_at: datetime | None

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "status": self.status,
            "check_id": self.check_id,
            "observed_content_sha256": self.observed_content_sha256,
            "next_check_at": (
                self.next_check_at.isoformat()
                if self.next_check_at is not None
                else None
            ),
        }


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


class _CatalogDocumentError(ValueError):
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


def build_siur_catalog_downloader() -> SafeHTTPSDownloader:
    """Build the pinned, bounded downloader used by the scheduled job."""

    return SafeHTTPSDownloader(
        HTTPSDownloadPolicy(
            allowed_origins=(SIUR_SOURCE_ORIGIN,),
            max_response_bytes=SIUR_CATALOG_MAX_RESPONSE_BYTES,
            timeout_seconds=120.0,
            dns_timeout_seconds=3.0,
            connect_timeout_seconds=10.0,
            idle_timeout_seconds=30.0,
            max_redirects=3,
            allowed_content_types=frozenset(
                {"application/json", "text/json"}
            ),
            user_agent="AsistenteAyuntamientos/siur-catalog-watcher",
        )
    )


def scheduled_siur_catalog_check_key(at: datetime) -> str:
    """Return the stable UTC idempotency key for one scheduler day."""

    normalized = _aware_utc(at)
    return f"scheduled:{normalized.date().isoformat()}"


def latest_siur_catalog_update_check(
    db: Session,
) -> ReferenceCatalogUpdateCheck | None:
    return db.scalar(
        select(ReferenceCatalogUpdateCheck)
        .where(ReferenceCatalogUpdateCheck.provider_key == SIUR_PROVIDER_KEY)
        .order_by(
            ReferenceCatalogUpdateCheck.checked_at.desc(),
            ReferenceCatalogUpdateCheck.id.desc(),
        )
        .limit(1)
    )


def check_siur_catalog_update(
    db: Session,
    *,
    trigger_kind: Literal["scheduled", "manual"] = "scheduled",
    idempotency_key: str | None = None,
    force: bool = False,
    checked_at: datetime | None = None,
    interval_seconds: int = SIUR_CATALOG_CHECK_INTERVAL_SECONDS,
    downloader: CatalogDownloader | None = None,
) -> CatalogUpdateCheckOutcome:
    """Conditionally check SIUR and persist immutable evidence.

    The function owns the supplied session transaction.  It uses a PostgreSQL
    transaction-scoped advisory lock, commits every recorded result, and
    rolls back before returning a lock-busy disposition or re-raising an
    unexpected programming/database failure.
    """

    at = _aware_utc(checked_at or datetime.now(timezone.utc))
    interval = _validated_interval(interval_seconds)
    if trigger_kind not in {"scheduled", "manual"}:
        raise ValueError("trigger_kind must be scheduled or manual")
    key = _validated_idempotency_key(
        idempotency_key
        or (
            scheduled_siur_catalog_check_key(at)
            if trigger_kind == "scheduled"
            else ""
        )
    )

    acquired = bool(
        db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": _catalog_watcher_lock_key(SIUR_PROVIDER_KEY)},
        )
    )
    if not acquired:
        duplicate = _check_by_idempotency_key(db, key)
        outcome = (
            _outcome_from_check("duplicate", duplicate)
            if duplicate is not None
            else CatalogUpdateCheckOutcome(
                disposition="lock_busy",
                status=None,
                check_id=None,
                observed_content_sha256=None,
                next_check_at=None,
            )
        )
        db.rollback()
        return outcome

    try:
        duplicate = _check_by_idempotency_key(db, key)
        if duplicate is not None:
            outcome = _outcome_from_check("duplicate", duplicate)
            db.commit()
            return outcome

        latest = latest_siur_catalog_update_check(db)
        if (
            not force
            and latest is not None
            and latest.next_check_at > at
        ):
            outcome = _outcome_from_check("not_due", latest)
            db.commit()
            return outcome

        validator_check = db.scalar(
            select(ReferenceCatalogUpdateCheck)
            .where(
                ReferenceCatalogUpdateCheck.provider_key
                == SIUR_PROVIDER_KEY,
                ReferenceCatalogUpdateCheck.status.in_(_SUCCESS_STATUSES),
                ReferenceCatalogUpdateCheck.observed_version_id.is_not(None),
            )
            .order_by(
                ReferenceCatalogUpdateCheck.checked_at.desc(),
                ReferenceCatalogUpdateCheck.id.desc(),
            )
            .limit(1)
        )
        request_etag = (
            validator_check.response_etag
            if validator_check is not None
            else None
        )
        request_last_modified = (
            validator_check.response_last_modified
            if validator_check is not None
            else None
        )
        actual_downloader = downloader or build_siur_catalog_downloader()
        started = monotonic()
        sink = BytesIO()
        response: _ResponseEvidence | None = None

        try:
            result = actual_downloader.download(
                DEFAULT_SOURCE_URL,
                sink,
                etag=request_etag,
                last_modified=request_last_modified,
                accept="application/json",
            )
            response = _validate_download_result(result, sink.getvalue())
            observed = _observed_version_from_response(
                db,
                response=response,
                document=sink.getvalue(),
                validator_check=validator_check,
                checked_at=at,
            )
        except (SafeDownloadError, _CatalogDocumentError) as error:
            response = response or _response_from_error(
                error,
                sink.getvalue(),
            )
            check = _record_error_check(
                db,
                key=key,
                trigger_kind=trigger_kind,
                checked_at=at,
                next_check_at=at + interval,
                duration_ms=_elapsed_ms(started),
                request_etag=request_etag,
                request_last_modified=request_last_modified,
                response=response,
                error=error,
            )
            db.commit()
            return _outcome_from_check("recorded", check)

        assert response is not None
        baseline = _current_siur_snapshot(db)
        status: Literal["unchanged", "update_available"] = (
            "unchanged"
            if baseline is not None
            and baseline.content_sha256 == observed.content_sha256
            else "update_available"
        )
        check = ReferenceCatalogUpdateCheck(
            provider_key=SIUR_PROVIDER_KEY,
            idempotency_key=key,
            trigger_kind=trigger_kind,
            source_url=DEFAULT_SOURCE_URL,
            baseline_snapshot_id=baseline.id if baseline is not None else None,
            observed_version_id=observed.id,
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
    except Exception:
        db.rollback()
        raise


def run_siur_catalog_update_check_job() -> dict[str, object]:
    """Zero-argument RQ/cron entrypoint; safe to invoke more than once daily."""

    from app.db.session import SessionLocal

    at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        outcome = check_siur_catalog_update(
            db,
            trigger_kind="scheduled",
            idempotency_key=scheduled_siur_catalog_check_key(at),
            checked_at=at,
        )
    logger.info(
        "SIUR catalog update check disposition=%s status=%s check_id=%s",
        outcome.disposition,
        outcome.status,
        outcome.check_id,
    )
    return outcome.as_dict()


def _observed_version_from_response(
    db: Session,
    *,
    response: _ResponseEvidence,
    document: bytes,
    validator_check: ReferenceCatalogUpdateCheck | None,
    checked_at: datetime,
) -> ReferenceCatalogObservedVersion:
    if response.not_modified:
        if (
            validator_check is None
            or validator_check.observed_version is None
        ):
            raise _CatalogDocumentError(
                "HTTP 304 has no prior observed catalog version",
                code="orphan_not_modified",
            )
        return validator_check.observed_version

    try:
        analysis = analyze_siur_settings(document)
    except SiurSettingsError as error:
        raise _CatalogDocumentError(
            str(error),
            code="invalid_catalog_document",
        ) from error
    try:
        raw_catalog = json.loads(document.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _CatalogDocumentError(
            "settings is not strict UTF-8 JSON",
            code="invalid_catalog_document",
        ) from error
    if not isinstance(raw_catalog, dict):
        raise _CatalogDocumentError(
            "settings root must be a JSON object",
            code="unsupported_catalog_root",
        )

    content_sha256 = canonical_catalog_sha256(raw_catalog)
    observed = db.scalar(
        select(ReferenceCatalogObservedVersion).where(
            ReferenceCatalogObservedVersion.provider_key == SIUR_PROVIDER_KEY,
            ReferenceCatalogObservedVersion.content_sha256 == content_sha256,
        )
    )
    if observed is None:
        observed = ReferenceCatalogObservedVersion(
            provider_key=SIUR_PROVIDER_KEY,
            source_url=DEFAULT_SOURCE_URL,
            final_url=response.final_url or DEFAULT_SOURCE_URL,
            content_sha256=content_sha256,
            raw_sha256=response.raw_sha256 or hashlib.sha256(document).hexdigest(),
            size_bytes=len(document),
            raw_catalog_json=raw_catalog,
            analysis_json=_analysis_summary(analysis),
            retrieved_at=checked_at,
        )
        db.add(observed)
        db.flush()
    return observed


def _validate_download_result(
    result: HTTPSDownloadResult,
    document: bytes,
) -> _ResponseEvidence:
    if (
        result.source_url != DEFAULT_SOURCE_URL
        or not result.redirect_chain
        or result.redirect_chain[0] != DEFAULT_SOURCE_URL
        or result.redirect_chain[-1] != result.final_url
        or result.redirects != len(result.redirect_chain) - 1
        or any(
            not _is_pinned_siur_url(url)
            for url in result.redirect_chain
        )
    ):
        raise _CatalogDocumentError(
            "downloader returned an unexpected URL chain",
            code="download_result_mismatch",
            retryable=True,
        )
    if result.not_modified:
        if (
            result.status_code != 304
            or document
            or result.size_bytes != 0
            or result.sha256 is not None
        ):
            raise _CatalogDocumentError(
                "invalid HTTP 304 download result",
                code="download_result_mismatch",
                retryable=True,
            )
        raw_sha256 = None
    else:
        calculated_sha256 = hashlib.sha256(document).hexdigest()
        if (
            result.status_code != 200
            or not document
            or result.size_bytes != len(document)
            or result.sha256 != calculated_sha256
        ):
            raise _CatalogDocumentError(
                "downloaded catalog evidence is inconsistent",
                code="download_result_mismatch",
                retryable=True,
            )
        raw_sha256 = calculated_sha256
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


def _response_from_error(
    error: SafeDownloadError | _CatalogDocumentError,
    document: bytes,
) -> _ResponseEvidence:
    status_code = getattr(error, "status_code", None)
    return _ResponseEvidence(
        http_status=(
            status_code
            if isinstance(status_code, int) and 100 <= status_code <= 599
            else (200 if document else None)
        ),
        final_url=DEFAULT_SOURCE_URL if document else None,
        size_bytes=len(document),
        raw_sha256=hashlib.sha256(document).hexdigest() if document else None,
    )


def _record_error_check(
    db: Session,
    *,
    key: str,
    trigger_kind: str,
    checked_at: datetime,
    next_check_at: datetime,
    duration_ms: int,
    request_etag: str | None,
    request_last_modified: str | None,
    response: _ResponseEvidence,
    error: SafeDownloadError | _CatalogDocumentError,
) -> ReferenceCatalogUpdateCheck:
    baseline = _current_siur_snapshot(db)
    check = ReferenceCatalogUpdateCheck(
        provider_key=SIUR_PROVIDER_KEY,
        idempotency_key=key,
        trigger_kind=trigger_kind,
        source_url=DEFAULT_SOURCE_URL,
        baseline_snapshot_id=baseline.id if baseline is not None else None,
        observed_version_id=None,
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
        error_code=str(getattr(error, "code", "catalog_check_error"))[:64],
        error_message=str(error)[:4096] or "catalog check failed",
        error_retryable=bool(getattr(error, "retryable", False)),
    )
    db.add(check)
    db.flush()
    return check


def _current_siur_snapshot(
    db: Session,
) -> ReferenceCatalogSnapshot | None:
    return db.scalar(
        select(ReferenceCatalogSnapshot)
        .where(
            ReferenceCatalogSnapshot.provider_key == SIUR_PROVIDER_KEY,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
        .limit(1)
    )


def _check_by_idempotency_key(
    db: Session,
    key: str,
) -> ReferenceCatalogUpdateCheck | None:
    return db.scalar(
        select(ReferenceCatalogUpdateCheck).where(
            ReferenceCatalogUpdateCheck.provider_key == SIUR_PROVIDER_KEY,
            ReferenceCatalogUpdateCheck.idempotency_key == key,
        )
    )


def _outcome_from_check(
    disposition: Literal["recorded", "duplicate", "not_due"],
    check: ReferenceCatalogUpdateCheck,
) -> CatalogUpdateCheckOutcome:
    observed_sha256 = (
        check.observed_version.content_sha256
        if check.observed_version is not None
        else None
    )
    return CatalogUpdateCheckOutcome(
        disposition=disposition,
        status=check.status,
        check_id=check.id,
        observed_content_sha256=observed_sha256,
        next_check_at=check.next_check_at,
    )


def _analysis_summary(analysis: Any) -> dict[str, Any]:
    issues = [
        {"path": issue.path, "code": issue.code}
        for issue in analysis.unresolved[:256]
    ]
    return {
        "parser": "siur-settings-v1",
        "service_count": len(analysis.services),
        "top_level_group_count": analysis.top_level_group_count,
        "group_count": analysis.group_count,
        "layer_count": analysis.layer_count,
        "unresolved_count": len(analysis.unresolved),
        "issues": issues,
        "issues_truncated": len(issues) < len(analysis.unresolved),
    }


def _catalog_watcher_lock_key(provider_key: str) -> int:
    digest = hashlib.sha256(
        _WATCHER_LOCK_DOMAIN + provider_key.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _is_pinned_siur_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "idecyl.jcyl.es"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.fragment
    )


def _validated_interval(value: int) -> timedelta:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 300 <= value <= 604_800
    ):
        raise ValueError("interval_seconds must be between 300 and 604800")
    return timedelta(seconds=value)


def _validated_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or "\x00" in normalized:
        raise ValueError("idempotency_key is invalid")
    return normalized


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("catalog check timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


__all__ = [
    "CatalogUpdateCheckOutcome",
    "SIUR_CATALOG_CHECK_INTERVAL_SECONDS",
    "build_siur_catalog_downloader",
    "check_siur_catalog_update",
    "latest_siur_catalog_update_check",
    "run_siur_catalog_update_check_job",
    "scheduled_siur_catalog_check_key",
]
