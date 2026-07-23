"""Fail-closed acquisition of immutable local reference-layer inputs.

The catalog discovery code proposes a source and the probe code proves the
remote collection.  This module performs the next boundary: bounded HTTPS
downloads into private staging files, validation before content-addressed
publication, deterministic manifests and optional append-only database links.

It deliberately does *not* decide whether redistribution is authorized: that
decision belongs to the caller and is neither asserted nor inferred here.  It
also does not promote delivery versions; lifecycle fencing and publication
remain separate concerns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import hashlib
import io
import json
import math
from pathlib import Path
from pathlib import PurePosixPath
import re
import sqlite3
import struct
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
import zipfile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers.blob_store import (
    ReferenceBlobStore,
    StoredReferenceBlob,
)
from app.reference_layers.models import (
    ReferenceLayerSource,
    ReferenceSourceArtifact,
    ReferenceSyncRun,
    ReferenceSyncRunArtifact,
)
from app.reference_layers.safe_download import (
    HTTPSDownloadPolicy,
    HTTPSDownloadResult,
    SafeHTTPSDownloader,
    normalize_https_url,
)
from app.reference_layers.source_discovery import SourceCandidate, candidate_definition
from app.reference_layers.source_probes import (
    MAX_PROBE_BYTES,
    SourceProbe,
    SourceProbeError,
    probe_candidate_document,
)


ArtifactKind = Literal[
    "capabilities", "manifest", "dataset", "style", "metadata", "tile_archive"
]
ArtifactRole = Literal["observation", "input", "style", "metadata"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REMOTE_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,1000}$")
_XYZ_TOKEN_RE = re.compile(r"\{(?:z|x|y|-y)\}", re.IGNORECASE)
_ANY_TEMPLATE_TOKEN_RE = re.compile(r"\{[^{}]+\}")
_MAX_JSON_NODES = 1_000_000
_MAX_JSON_DEPTH = 96
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_TILE_COUNT = 25_000_000
_ABSOLUTE_MAX_TILE_COUNT = 250_000_000
_MAX_ETAG_CHARS = 4096
_MAX_SOURCE_VERSION_CHARS = 2048
_MAX_RUN_STATS_BYTES = 1024 * 1024
_MAX_ARTIFACT_METADATA_BYTES = 4 * 1024 * 1024
_MAX_STYLES_PER_SOURCE = 256
_MAX_SLD_ELEMENTS = 100_000
_MAX_SLD_DEPTH = 64
_MAX_SLD_TEXT_BYTES = 4 * 1024 * 1024
_MAX_SLD_EXTRACTED_BYTES = 2 * MAX_PROBE_BYTES
_STYLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:]{1,255}$")
_STYLE_SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,254}$")
_EXTERNAL_SLD_TEXT_RE = re.compile(
    r"(?:\b(?:https?|ftp|file|data|jar):|\burl\s*\()",
    re.IGNORECASE,
)
_SLD_NAMESPACE = "http://www.opengis.net/sld"
_XML_MEDIA_TYPES = frozenset(
    {
        "application/xml",
        "text/xml",
        "application/vnd.ogc.wfs_xml",
        "application/vnd.ogc.wms_xml",
        "application/vnd.ogc.sld+xml",
        "application/atom+xml",
        "application/octet-stream",
    }
)
_JSON_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/geo+json",
        "application/vnd.geo+json",
        "text/json",
        "application/octet-stream",
    }
)
_RASTER_MEDIA_TYPES = frozenset(
    {
        "image/tiff",
        "image/geotiff",
        "application/geotiff",
        "application/octet-stream",
    }
)


class ReferenceAcquisitionError(RuntimeError):
    """Classified acquisition failure for durable retry/failure policy."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AcquisitionConfigurationError(ReferenceAcquisitionError, ValueError):
    def __init__(self, message: str, *, code: str = "invalid_source") -> None:
        super().__init__(message, code=code, retryable=False)


class AcquisitionValidationError(ReferenceAcquisitionError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_upstream_data",
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)


class AcquisitionLimitError(ReferenceAcquisitionError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, retryable=False)


class AcquisitionPersistenceError(ReferenceAcquisitionError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, retryable=False)


@dataclass(frozen=True)
class AcquisitionLimits:
    """Hard limits applied independently of remote server declarations."""

    max_probe_bytes: int = MAX_PROBE_BYTES
    max_page_bytes: int = 64 * 1024 * 1024
    max_dataset_bytes: int = 20 * 1024 * 1024 * 1024
    max_total_bytes: int = 40 * 1024 * 1024 * 1024
    page_size: int = 2_000
    max_pages: int = 10_000
    max_features: int = 20_000_000
    timeout_seconds: float = 1800.0
    idle_timeout_seconds: float = 90.0
    max_redirects: int = 3

    def __post_init__(self) -> None:
        for name in (
            "max_probe_bytes",
            "max_page_bytes",
            "max_dataset_bytes",
            "max_total_bytes",
            "page_size",
            "max_pages",
            "max_features",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_probe_bytes > self.max_page_bytes:
            raise ValueError("max_probe_bytes cannot exceed max_page_bytes")
        if self.max_page_bytes > self.max_dataset_bytes:
            raise ValueError("max_page_bytes cannot exceed max_dataset_bytes")
        if self.max_dataset_bytes > self.max_total_bytes:
            raise ValueError("max_dataset_bytes cannot exceed max_total_bytes")
        for name in ("timeout_seconds", "idle_timeout_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")


@dataclass(frozen=True)
class ConditionalRequest:
    source_url: str
    etag: str | None = None
    last_modified: str | None = None

    def __post_init__(self) -> None:
        normalize_https_url(self.source_url)
        for name, value in (("etag", self.etag), ("last_modified", self.last_modified)):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 1024
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(f"{name} is not a safe HTTP header value")

    @classmethod
    def from_run(
        cls,
        run: ReferenceSyncRun | None,
        *,
        source_url: str,
    ) -> "ConditionalRequest | None":
        if run is None or (run.observed_etag is None and run.observed_last_modified is None):
            return None
        last_modified = None
        if run.observed_last_modified is not None:
            value = run.observed_last_modified
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            last_modified = format_datetime(value.astimezone(timezone.utc), usegmt=True)
        return cls(
            source_url=source_url,
            etag=run.observed_etag,
            last_modified=last_modified,
        )

    @classmethod
    def from_artifact(
        cls,
        artifact: ReferenceSourceArtifact | None,
    ) -> "ConditionalRequest | None":
        if (
            artifact is None
            or artifact.source_url is None
            or (artifact.upstream_etag is None and artifact.upstream_last_modified is None)
        ):
            return None
        modified = artifact.upstream_last_modified
        return cls(
            source_url=artifact.source_url,
            etag=artifact.upstream_etag,
            last_modified=(
                format_datetime(_aware_utc(modified), usegmt=True)
                if modified is not None
                else None
            ),
        )


@dataclass(frozen=True)
class AcquiredArtifact:
    artifact_kind: ArtifactKind
    role: ArtifactRole
    media_type: str
    blob: StoredReferenceBlob
    source_url: str | None = None
    final_url: str | None = None
    source_version: str | None = None
    upstream_etag: str | None = None
    upstream_last_modified: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        _bounded_optional_text(
            self.source_version,
            "artifact source version",
            _MAX_SOURCE_VERSION_CHARS,
        )
        _bounded_optional_text(
            self.upstream_etag,
            "artifact ETag",
            _MAX_ETAG_CHARS,
        )
        _bounded_json_object(
            self.metadata,
            "artifact metadata",
            _MAX_ARTIFACT_METADATA_BYTES,
        )


@dataclass(frozen=True)
class AcquisitionResult:
    source_key: str
    source_definition_sha256: str
    protocol: str
    target_kind: str
    not_modified: bool
    artifacts: tuple[AcquiredArtifact, ...]
    manifest_sha256: str | None
    probe: SourceProbe | None
    observed_etag: str | None
    observed_last_modified: datetime | None
    observed_version: str | None
    feature_count: int | None
    total_bytes: int
    stats: dict[str, Any]

    def __post_init__(self) -> None:
        _bounded_optional_text(
            self.observed_etag,
            "observed ETag",
            _MAX_ETAG_CHARS,
        )
        _bounded_optional_text(
            self.observed_version,
            "observed source version",
            _MAX_SOURCE_VERSION_CHARS,
        )
        _bounded_json_object(
            self.stats,
            "acquisition stats",
            _MAX_RUN_STATS_BYTES,
        )


class _Downloader(Protocol):
    def download(
        self,
        url: str,
        sink,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
    ) -> HTTPSDownloadResult: ...


DownloaderFactory = Callable[[HTTPSDownloadPolicy], _Downloader]
Validator = Callable[[bytes], Any]
FileValidator = Callable[[Path, int], Any]


@dataclass(frozen=True)
class _Downloaded:
    result: HTTPSDownloadResult
    blob: StoredReferenceBlob | None
    parsed: Any = None


@dataclass(frozen=True)
class _FeaturePage:
    count: int
    number_matched: int | None
    next_url: str | None
    feature_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ArcGISIds:
    object_id_field: str
    object_ids: tuple[int, ...]


@dataclass(frozen=True)
class _StyleSpec:
    catalog_style_source_key: str
    remote_name: str


@dataclass(frozen=True)
class _StyleRequest:
    endpoint_url: str
    layer_name: str
    styles: tuple[_StyleSpec, ...]


@dataclass(frozen=True)
class _ParsedStyleBundle:
    sld_version: str
    standalone_slds: tuple[tuple[str, bytes], ...]


class ReferenceAcquisitionPipeline:
    """Acquire one source without changing run or delivery lifecycle state."""

    def __init__(
        self,
        store: ReferenceBlobStore,
        *,
        limits: AcquisitionLimits | None = None,
        downloader_factory: DownloaderFactory = SafeHTTPSDownloader,
    ) -> None:
        self.store = store
        self.limits = limits or AcquisitionLimits()
        self._downloader_factory = downloader_factory

    def acquire(
        self,
        source: SourceCandidate | ReferenceLayerSource,
        *,
        run: ReferenceSyncRun | None = None,
        conditional: ConditionalRequest | None = None,
    ) -> AcquisitionResult:
        candidate = candidate_from_source_model(source) if isinstance(
            source, ReferenceLayerSource
        ) else source
        _validate_candidate(candidate)
        if isinstance(source, ReferenceLayerSource):
            _validate_run_snapshot(source, run)

        handlers = {
            "wfs": self._acquire_wfs,
            "ogc_api_features": self._acquire_ogc_api,
            "arcgis_rest": self._acquire_arcgis,
            "wcs": self._acquire_wcs,
            "download": self._acquire_download,
            "atom": self._acquire_atom,
            "wmts": self._acquire_tile_source,
            "xyz": self._acquire_tile_source,
            "wms_tiles": self._acquire_tile_source,
        }
        handler = handlers.get(candidate.protocol)
        if handler is None:
            raise AcquisitionConfigurationError(
                f"protocol {candidate.protocol!r} requires manual acquisition",
                code="manual_source",
            )
        return handler(candidate, conditional=conditional)

    def _download(
        self,
        candidate: SourceCandidate,
        url: str,
        *,
        max_bytes: int,
        accept: str,
        allowed_media_types: frozenset[str] | None,
        validator: Validator | None = None,
        file_validator: FileValidator | None = None,
        conditional: ConditionalRequest | None = None,
    ) -> _Downloaded:
        if validator is not None and file_validator is not None:
            raise ValueError("only one artifact validator may be configured")
        requested_url = _require_same_origin(candidate.endpoint_url, url)
        applied_conditional = (
            conditional
            if conditional is not None
            and normalize_https_url(conditional.source_url) == requested_url
            else None
        )
        origin = _origin(candidate.endpoint_url)
        effective_max_bytes = min(
            max_bytes,
            self.store.max_blob_bytes,
            self.limits.max_total_bytes,
        )
        policy = HTTPSDownloadPolicy(
            allowed_origins=(origin,),
            max_response_bytes=effective_max_bytes,
            timeout_seconds=self.limits.timeout_seconds,
            idle_timeout_seconds=self.limits.idle_timeout_seconds,
            max_redirects=self.limits.max_redirects,
            allowed_content_types=allowed_media_types,
        )
        downloader = self._downloader_factory(policy)
        with self.store.stage(max_bytes=effective_max_bytes) as staging:
            result = downloader.download(
                requested_url,
                staging,
                etag=applied_conditional.etag if applied_conditional else None,
                last_modified=(
                    applied_conditional.last_modified if applied_conditional else None
                ),
                accept=accept,
            )
            _validate_download_result(candidate, requested_url, result)
            if result.not_modified:
                if applied_conditional is None:
                    raise AcquisitionValidationError(
                        "upstream returned not-modified for a different resource URL",
                        code="unexpected_not_modified",
                    )
                return _Downloaded(result=result, blob=None)
            if result.sha256 is None:
                raise AcquisitionValidationError(
                    "download result omitted its content digest",
                    code="missing_download_digest",
                )
            parsed = None
            if file_validator is not None:
                parsed = file_validator(staging.staging_path, result.size_bytes)
            elif validator is not None:
                try:
                    payload = Path(staging.staging_path).read_bytes()
                except OSError as exc:
                    raise AcquisitionValidationError(
                        "staged artifact could not be read for validation",
                        code="staging_read_failed",
                    ) from exc
                parsed = validator(payload)
            blob = staging.commit(
                expected_sha256=result.sha256,
                expected_size=result.size_bytes,
            )
        return _Downloaded(result=result, blob=blob, parsed=parsed)

    def _local_json_artifact(
        self,
        payload: dict[str, Any],
        *,
        kind: ArtifactKind,
        role: ArtifactRole,
        metadata: dict[str, Any],
    ) -> AcquiredArtifact:
        encoded = _canonical_json(payload) + b"\n"
        maximum = min(
            _MAX_MANIFEST_BYTES,
            self.store.max_blob_bytes,
            self.limits.max_total_bytes,
        )
        if len(encoded) > maximum:
            raise AcquisitionLimitError(
                "generated acquisition manifest is too large",
                code="manifest_too_large",
            )
        blob = self.store.put_stream(io.BytesIO(encoded), max_bytes=maximum)
        return AcquiredArtifact(
            artifact_kind=kind,
            role=role,
            media_type="application/json",
            blob=blob,
            metadata=metadata,
        )

    def _remote_artifact(
        self,
        downloaded: _Downloaded,
        *,
        kind: ArtifactKind,
        role: ArtifactRole,
        metadata: dict[str, Any],
        source_version: str | None = None,
    ) -> AcquiredArtifact:
        if downloaded.blob is None:
            raise AcquisitionValidationError(
                "a not-modified response has no publishable artifact",
                code="missing_artifact",
            )
        result = downloaded.result
        return AcquiredArtifact(
            artifact_kind=kind,
            role=role,
            media_type=result.content_type or "application/octet-stream",
            blob=downloaded.blob,
            source_url=result.source_url,
            final_url=result.final_url,
            source_version=source_version,
            upstream_etag=result.etag,
            upstream_last_modified=_http_datetime(result.last_modified),
            metadata=metadata,
        )

    def _probe(
        self,
        candidate: SourceCandidate,
    ) -> tuple[SourceProbe, AcquiredArtifact, bytes]:
        url, accept, media_types = _probe_request(candidate)

        def validate(document: bytes) -> tuple[SourceProbe, bytes]:
            try:
                probe = probe_candidate_document(candidate, document)
            except SourceProbeError as exc:
                raise AcquisitionValidationError(
                    "source metadata did not pass its protocol probe",
                    code="invalid_capabilities",
                ) from exc
            if not probe.available or probe.canonical_name is None:
                raise AcquisitionValidationError(
                    probe.reason or "requested collection is unavailable",
                    code="collection_unavailable",
                )
            return probe, document

        downloaded = self._download(
            candidate,
            url,
            max_bytes=self.limits.max_probe_bytes,
            accept=accept,
            allowed_media_types=media_types,
            validator=validate,
        )
        probe, document = cast(tuple[SourceProbe, bytes], downloaded.parsed)
        artifact = self._remote_artifact(
            downloaded,
            kind="capabilities",
            role="observation",
            source_version=probe.service_version,
            metadata={
                "protocol": candidate.protocol,
                "requested_name": candidate.remote_name,
                "canonical_name": probe.canonical_name,
                "fingerprint_sha256": probe.fingerprint_sha256,
                "fingerprint_quality": probe.fingerprint_quality,
                "probe": probe.metadata,
            },
        )
        return probe, artifact, document

    def _acquire_styles(
        self,
        candidate: SourceCandidate,
    ) -> list[AcquiredArtifact]:
        request = _style_request_config(candidate)
        if request is None or not request.styles:
            return []
        if len(request.styles) > _MAX_STYLES_PER_SOURCE:
            raise AcquisitionLimitError(
                "source declares too many catalog styles",
                code="style_count_limit",
            )

        url = _merge_query(
            request.endpoint_url,
            {
                "service": "WMS",
                "request": "GetStyles",
                "version": "1.1.1",
                "layers": request.layer_name,
            },
        )
        downloaded = self._download(
            candidate,
            url,
            max_bytes=self.limits.max_probe_bytes,
            accept=(
                "application/vnd.ogc.sld+xml, application/xml;q=0.9, "
                "text/xml;q=0.8"
            ),
            allowed_media_types=_XML_MEDIA_TYPES,
            validator=lambda payload: _parse_style_bundle(
                payload,
                layer_name=request.layer_name,
                style_names=tuple(item.remote_name for item in request.styles),
            ),
        )
        parsed = cast(_ParsedStyleBundle, downloaded.parsed)
        bundle = self._remote_artifact(
            downloaded,
            kind="style",
            role="observation",
            source_version=parsed.sld_version,
            metadata={
                "schema": "reference-style-bundle/v1",
                "catalog_style_source_keys": [
                    item.catalog_style_source_key for item in request.styles
                ],
                "remote_names": [item.remote_name for item in request.styles],
                "style_layer_name": request.layer_name,
            },
        )
        artifacts: list[AcquiredArtifact] = [bundle]
        standalone_by_name = dict(parsed.standalone_slds)
        maximum = min(
            self.limits.max_probe_bytes,
            self.store.max_blob_bytes,
            self.limits.max_total_bytes,
        )
        for spec in request.styles:
            standalone_blob = self.store.put_stream(
                io.BytesIO(standalone_by_name[spec.remote_name]),
                max_bytes=maximum,
            )
            standalone = AcquiredArtifact(
                artifact_kind="style",
                role="style",
                media_type="application/vnd.ogc.sld+xml",
                blob=standalone_blob,
                source_version=parsed.sld_version,
                metadata={
                    "schema": "reference-style-sld/v1",
                    "catalog_style_source_key": spec.catalog_style_source_key,
                    "remote_name": spec.remote_name,
                    "style_layer_name": request.layer_name,
                    "parent_sha256": bundle.blob.sha256,
                },
            )
            artifacts.append(standalone)
            _enforce_total_bytes(artifacts, self.limits.max_total_bytes)
        return artifacts

    def _finish(
        self,
        candidate: SourceCandidate,
        *,
        probe: SourceProbe | None,
        artifacts: list[AcquiredArtifact],
        materialization: dict[str, Any],
        feature_count: int | None,
        stats: dict[str, Any],
        observed: HTTPSDownloadResult | None = None,
    ) -> AcquisitionResult:
        total_before_manifest = sum(item.blob.size_bytes for item in artifacts)
        if total_before_manifest > self.limits.max_total_bytes:
            raise AcquisitionLimitError(
                "source snapshot exceeds the aggregate byte limit",
                code="snapshot_too_large",
            )
        manifest_payload = {
            "schema": "reference-acquisition-manifest/v1",
            "source": {
                "source_key": candidate.source_key,
                "definition_sha256": candidate.definition_sha256,
                "protocol": candidate.protocol,
                "target_kind": candidate.target_kind,
                "endpoint_url": candidate.endpoint_url,
                "remote_name": candidate.remote_name,
            },
            "probe": _probe_manifest(probe),
            "materialization": materialization,
            "artifacts": [_artifact_manifest(index, item) for index, item in enumerate(artifacts)],
            "stats": stats,
        }
        manifest = self._local_json_artifact(
            manifest_payload,
            kind="manifest",
            role="metadata",
            metadata={
                "schema": manifest_payload["schema"],
                "protocol": candidate.protocol,
                "target_kind": candidate.target_kind,
            },
        )
        artifacts.append(manifest)
        total_bytes = sum(item.blob.size_bytes for item in artifacts)
        if total_bytes > self.limits.max_total_bytes:
            raise AcquisitionLimitError(
                "source snapshot exceeds the aggregate byte limit",
                code="snapshot_too_large",
            )
        return AcquisitionResult(
            source_key=candidate.source_key,
            source_definition_sha256=candidate.definition_sha256,
            protocol=candidate.protocol,
            target_kind=candidate.target_kind,
            not_modified=False,
            artifacts=tuple(artifacts),
            manifest_sha256=manifest.blob.sha256,
            probe=probe,
            observed_etag=observed.etag if observed else None,
            observed_last_modified=_http_datetime(observed.last_modified) if observed else None,
            observed_version=probe.service_version if probe else None,
            feature_count=feature_count,
            total_bytes=total_bytes,
            stats=stats,
        )

    def _unchanged(
        self,
        candidate: SourceCandidate,
        result: HTTPSDownloadResult,
        *,
        probe: SourceProbe | None = None,
        artifacts: tuple[AcquiredArtifact, ...] = (),
    ) -> AcquisitionResult:
        return AcquisitionResult(
            source_key=candidate.source_key,
            source_definition_sha256=candidate.definition_sha256,
            protocol=candidate.protocol,
            target_kind=candidate.target_kind,
            not_modified=True,
            artifacts=artifacts,
            manifest_sha256=None,
            probe=probe,
            observed_etag=result.etag,
            observed_last_modified=_http_datetime(result.last_modified),
            observed_version=probe.service_version if probe else None,
            feature_count=None,
            total_bytes=sum(item.blob.size_bytes for item in artifacts),
            stats={"not_modified": True},
        )

    def _acquire_wfs(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        del conditional  # A page-level 304 cannot prove a complete snapshot.
        probe, capabilities, _document = self._probe(candidate)
        canonical_name = probe.canonical_name or ""
        page_size = _configured_page_size(candidate.config, self.limits.page_size)
        output_format = _config_text(
            candidate.config,
            "output_format",
            default="application/json",
            max_chars=200,
        )
        version = probe.service_version or "2.0.0"
        if version.startswith("1."):
            type_parameter = "typeName"
            count_parameter = "maxFeatures"
        elif version.startswith("2."):
            type_parameter = "typeNames"
            count_parameter = "count"
        else:
            raise AcquisitionValidationError(
                "WFS advertised an unsupported version",
                code="unsupported_wfs_version",
            )

        artifacts = [capabilities]
        page_artifacts: list[AcquiredArtifact] = []
        seen_page_digests: set[str] = set()
        seen_feature_ids: set[str] = set()
        total_features = 0
        expected_matched: int | None = None
        offset = 0
        terminal = False
        for page_index in range(self.limits.max_pages):
            params = {
                "service": "WFS",
                "request": "GetFeature",
                "version": version,
                type_parameter: canonical_name,
                "outputFormat": output_format,
                count_parameter: str(page_size),
            }
            if version.startswith("2.") or offset:
                params["startIndex"] = str(offset)
            sort_by = _config_optional_text(candidate.config, "sort_by", max_chars=500)
            if sort_by is not None:
                params["sortBy"] = sort_by
            url = _merge_query(candidate.endpoint_url, params)
            downloaded = self._download(
                candidate,
                url,
                max_bytes=self.limits.max_page_bytes,
                accept="application/geo+json, application/json;q=0.9",
                allowed_media_types=_JSON_MEDIA_TYPES,
                validator=lambda payload: _parse_feature_collection(
                    payload,
                    page_size=page_size,
                    allow_next=False,
                ),
            )
            page = cast(_FeaturePage, downloaded.parsed)
            if downloaded.blob is None:
                raise AcquisitionValidationError(
                    "WFS returned an unexpected not-modified page",
                    code="partial_not_modified",
                )
            if downloaded.blob.sha256 in seen_page_digests and page.count:
                raise AcquisitionValidationError(
                    "WFS repeated a non-empty page",
                    code="pagination_loop",
                )
            seen_page_digests.add(downloaded.blob.sha256)
            if page.number_matched is not None:
                if expected_matched is None:
                    expected_matched = page.number_matched
                    if expected_matched > self.limits.max_features:
                        raise AcquisitionLimitError(
                            "WFS reports too many features",
                            code="feature_limit",
                        )
                elif page.number_matched != expected_matched:
                    raise AcquisitionValidationError(
                        "WFS feature count changed during pagination",
                        code="unstable_snapshot",
                        retryable=True,
                    )
            duplicates = seen_feature_ids.intersection(page.feature_ids)
            if duplicates:
                raise AcquisitionValidationError(
                    "WFS repeated feature identifiers across pages",
                    code="unstable_pagination",
                    retryable=True,
                )
            seen_feature_ids.update(page.feature_ids)
            total_features += page.count
            if total_features > self.limits.max_features:
                raise AcquisitionLimitError(
                    "WFS snapshot exceeds the feature limit",
                    code="feature_limit",
                )
            page_artifacts.append(
                self._remote_artifact(
                    downloaded,
                    kind="dataset",
                    role="input" if page.count else "observation",
                    source_version=version,
                    metadata={
                        "protocol": "wfs",
                        "data_format": "geojson",
                        "collection": canonical_name,
                        "page_index": page_index,
                        "offset": offset,
                        "feature_count": page.count,
                        "terminal_empty_page": page.count == 0,
                    },
                )
            )
            _enforce_total_bytes(
                [capabilities, *page_artifacts],
                self.limits.max_total_bytes,
            )
            offset += page.count
            terminal = (
                page.count == 0
                or page.count < page_size
                or (expected_matched is not None and offset >= expected_matched)
            )
            if terminal:
                break
        if not terminal:
            raise AcquisitionLimitError(
                "WFS pagination exceeds the page limit",
                code="page_limit",
            )
        if expected_matched is not None and total_features != expected_matched:
            raise AcquisitionValidationError(
                "WFS snapshot does not match its advertised feature count",
                code="unstable_snapshot",
                retryable=True,
            )
        if len(page_artifacts) > 1 and len(seen_feature_ids) != total_features:
            raise AcquisitionValidationError(
                "paginated WFS snapshot lacks stable feature identifiers",
                code="missing_pagination_identity",
            )
        artifacts.extend(page_artifacts)
        style_artifacts = self._acquire_styles(candidate)
        artifacts.extend(style_artifacts)
        _enforce_total_bytes(artifacts, self.limits.max_total_bytes)
        stats = {
            "page_count": len(page_artifacts),
            "feature_count": total_features,
            "page_size": page_size,
            "number_matched": expected_matched,
            "feature_ids_observed": len(seen_feature_ids),
        }
        materialization = {
            "kind": "feature-pages",
            "format": "geojson",
            "collection": canonical_name,
            "page_artifact_sha256": [
                item.blob.sha256
                for item in page_artifacts
                if item.role == "input"
            ],
        }
        style_digests = _style_digests(style_artifacts)
        if style_digests:
            stats["style_count"] = len(style_digests)
            materialization["style_artifact_sha256"] = style_digests
        return self._finish(
            candidate,
            probe=probe,
            artifacts=artifacts,
            materialization=materialization,
            feature_count=total_features,
            stats=stats,
            observed=downloaded.result,
        )

    def _acquire_ogc_api(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        del conditional
        probe, capabilities, _document = self._probe(candidate)
        collection = probe.canonical_name or ""
        page_size = _configured_page_size(candidate.config, self.limits.page_size)
        current_url = _ogc_items_url(candidate, collection, page_size)
        seen_urls: set[str] = set()
        seen_digests: set[str] = set()
        seen_feature_ids: set[str] = set()
        page_artifacts: list[AcquiredArtifact] = []
        total_features = 0
        expected_matched: int | None = None
        offset = 0
        terminal = False
        last_result: HTTPSDownloadResult | None = None
        for page_index in range(self.limits.max_pages):
            current_url = _require_same_origin(candidate.endpoint_url, current_url)
            if current_url in seen_urls:
                raise AcquisitionValidationError(
                    "OGC API pagination contains a URL loop",
                    code="pagination_loop",
                )
            seen_urls.add(current_url)
            downloaded = self._download(
                candidate,
                current_url,
                max_bytes=self.limits.max_page_bytes,
                accept="application/geo+json, application/json;q=0.9",
                allowed_media_types=_JSON_MEDIA_TYPES,
                validator=lambda payload: _parse_feature_collection(
                    payload,
                    page_size=page_size,
                    allow_next=True,
                ),
            )
            last_result = downloaded.result
            page = cast(_FeaturePage, downloaded.parsed)
            if downloaded.blob is None:
                raise AcquisitionValidationError(
                    "OGC API returned an unexpected not-modified page",
                    code="partial_not_modified",
                )
            if downloaded.blob.sha256 in seen_digests and page.count:
                raise AcquisitionValidationError(
                    "OGC API repeated a non-empty page",
                    code="pagination_loop",
                )
            seen_digests.add(downloaded.blob.sha256)
            if page.number_matched is not None:
                if expected_matched is None:
                    expected_matched = page.number_matched
                    if expected_matched > self.limits.max_features:
                        raise AcquisitionLimitError(
                            "OGC API reports too many features",
                            code="feature_limit",
                        )
                elif page.number_matched != expected_matched:
                    raise AcquisitionValidationError(
                        "OGC API feature count changed during pagination",
                        code="unstable_snapshot",
                        retryable=True,
                    )
            duplicates = seen_feature_ids.intersection(page.feature_ids)
            if duplicates:
                raise AcquisitionValidationError(
                    "OGC API repeated feature identifiers across pages",
                    code="unstable_pagination",
                    retryable=True,
                )
            seen_feature_ids.update(page.feature_ids)
            total_features += page.count
            if total_features > self.limits.max_features:
                raise AcquisitionLimitError(
                    "OGC API snapshot exceeds the feature limit",
                    code="feature_limit",
                )
            page_artifacts.append(
                self._remote_artifact(
                    downloaded,
                    kind="dataset",
                    role="input" if page.count else "observation",
                    metadata={
                        "protocol": "ogc_api_features",
                        "data_format": "geojson",
                        "collection": collection,
                        "page_index": page_index,
                        "offset": offset,
                        "feature_count": page.count,
                        "terminal_empty_page": page.count == 0,
                    },
                )
            )
            _enforce_total_bytes(
                [capabilities, *page_artifacts],
                self.limits.max_total_bytes,
            )
            offset += page.count
            if page.next_url is not None:
                current_url = urljoin(current_url, page.next_url)
                terminal = False
            elif page.count == 0 or page.count < page_size:
                terminal = True
            elif expected_matched is not None and offset >= expected_matched:
                terminal = True
            else:
                current_url = _replace_query_value(current_url, "offset", str(offset))
                terminal = False
            if terminal:
                break
        if not terminal:
            raise AcquisitionLimitError(
                "OGC API pagination exceeds the page limit",
                code="page_limit",
            )
        if expected_matched is not None and total_features != expected_matched:
            raise AcquisitionValidationError(
                "OGC API snapshot does not match its advertised feature count",
                code="unstable_snapshot",
                retryable=True,
            )
        if len(page_artifacts) > 1 and len(seen_feature_ids) != total_features:
            raise AcquisitionValidationError(
                "paginated OGC API snapshot lacks stable feature identifiers",
                code="missing_pagination_identity",
            )
        artifacts = [capabilities, *page_artifacts]
        stats = {
            "page_count": len(page_artifacts),
            "feature_count": total_features,
            "page_size": page_size,
            "number_matched": expected_matched,
            "feature_ids_observed": len(seen_feature_ids),
        }
        return self._finish(
            candidate,
            probe=probe,
            artifacts=artifacts,
            materialization={
                "kind": "feature-pages",
                "format": "geojson",
                "collection": collection,
                "page_artifact_sha256": [
                    item.blob.sha256
                    for item in page_artifacts
                    if item.role == "input"
                ],
            },
            feature_count=total_features,
            stats=stats,
            observed=last_result,
        )

    def _acquire_arcgis(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        del conditional
        probe, capabilities, _document = self._probe(candidate)
        layer_id = probe.canonical_name or ""
        if not layer_id.isdecimal():
            raise AcquisitionValidationError(
                "ArcGIS probe did not resolve a numeric layer id",
                code="invalid_arcgis_layer",
            )
        layer_url = _append_path(candidate.endpoint_url, layer_id)
        details_url = _merge_query(layer_url, {"f": "pjson"})
        details = self._download(
            candidate,
            details_url,
            max_bytes=self.limits.max_probe_bytes,
            accept="application/json",
            allowed_media_types=_JSON_MEDIA_TYPES,
            validator=lambda payload: _parse_arcgis_details(payload, int(layer_id)),
        )
        details_json = cast(dict[str, Any], details.parsed)
        details_artifact = self._remote_artifact(
            details,
            kind="metadata",
            role="observation",
            source_version=probe.service_version,
            metadata={
                "protocol": "arcgis_rest",
                "layer_id": int(layer_id),
                "object_id_field": details_json.get("objectIdField"),
                "geometry_type": details_json.get("geometryType"),
                "spatial_reference": details_json.get("extent", {}).get("spatialReference")
                if isinstance(details_json.get("extent"), dict)
                else None,
            },
        )
        ids_url = _merge_query(
            _append_path(layer_url, "query"),
            {"where": "1=1", "returnIdsOnly": "true", "f": "json"},
        )
        ids_download = self._download(
            candidate,
            ids_url,
            max_bytes=self.limits.max_page_bytes,
            accept="application/json",
            allowed_media_types=_JSON_MEDIA_TYPES,
            validator=lambda payload: _parse_arcgis_ids(payload, self.limits.max_features),
        )
        ids = cast(_ArcGISIds, ids_download.parsed)
        ids_artifact = self._remote_artifact(
            ids_download,
            kind="metadata",
            role="observation",
            source_version=probe.service_version,
            metadata={
                "protocol": "arcgis_rest",
                "kind": "object-id-snapshot",
                "layer_id": int(layer_id),
                "object_id_field": ids.object_id_field,
                "feature_count": len(ids.object_ids),
            },
        )
        configured_size = _configured_page_size(candidate.config, self.limits.page_size)
        server_size = details_json.get("maxRecordCount")
        if isinstance(server_size, int) and not isinstance(server_size, bool) and server_size > 0:
            page_size = min(configured_size, server_size)
        else:
            page_size = configured_size
        data_format = _config_text(
            candidate.config,
            "arcgis_format",
            default="geojson",
            max_chars=20,
        ).casefold()
        if data_format not in {"geojson", "json"}:
            raise AcquisitionConfigurationError(
                "arcgis_format must be geojson or json",
                code="invalid_arcgis_format",
            )

        chunks = _arcgis_id_chunks(
            ids.object_ids,
            page_size=page_size,
            base_url=_append_path(layer_url, "query"),
        )
        if len(chunks) > self.limits.max_pages:
            raise AcquisitionLimitError(
                "ArcGIS pagination exceeds the page limit",
                code="page_limit",
            )
        page_artifacts: list[AcquiredArtifact] = []
        total_features = 0
        last_result = ids_download.result
        for page_index, object_ids in enumerate(chunks):
            query_url = _merge_query(
                _append_path(layer_url, "query"),
                {
                    "objectIds": ",".join(str(value) for value in object_ids),
                    "outFields": "*",
                    "returnGeometry": "true",
                    "f": data_format,
                },
            )
            if data_format == "geojson":
                validator = lambda payload, expected=object_ids: (
                    _parse_arcgis_geojson_page(
                        payload,
                        expected,
                        ids.object_id_field,
                    )
                )
            else:
                validator = lambda payload, expected=object_ids: (
                    _parse_arcgis_json_page(
                        payload,
                        expected,
                        ids.object_id_field,
                    )
                )
            downloaded = self._download(
                candidate,
                query_url,
                max_bytes=self.limits.max_page_bytes,
                accept="application/geo+json, application/json;q=0.9",
                allowed_media_types=_JSON_MEDIA_TYPES,
                validator=validator,
            )
            last_result = downloaded.result
            count = cast(int, downloaded.parsed)
            total_features += count
            page_artifacts.append(
                self._remote_artifact(
                    downloaded,
                    kind="dataset",
                    role="input",
                    source_version=probe.service_version,
                    metadata={
                        "protocol": "arcgis_rest",
                        "data_format": "geojson" if data_format == "geojson" else "esri-json",
                        "layer_id": int(layer_id),
                        "page_index": page_index,
                        "feature_count": count,
                        "first_object_id": object_ids[0],
                        "last_object_id": object_ids[-1],
                        "object_id_count": len(object_ids),
                    },
                )
            )
            _enforce_total_bytes(
                [capabilities, details_artifact, ids_artifact, *page_artifacts],
                self.limits.max_total_bytes,
            )

        verify_ids = self._download(
            candidate,
            ids_url,
            max_bytes=self.limits.max_page_bytes,
            accept="application/json",
            allowed_media_types=_JSON_MEDIA_TYPES,
            validator=lambda payload: _parse_arcgis_ids(payload, self.limits.max_features),
        )
        final_ids = cast(_ArcGISIds, verify_ids.parsed)
        if final_ids != ids:
            raise AcquisitionValidationError(
                "ArcGIS object ids changed during pagination",
                code="unstable_snapshot",
                retryable=True,
            )
        if total_features != len(ids.object_ids):
            raise AcquisitionValidationError(
                "ArcGIS page totals do not match the object-id snapshot",
                code="unstable_snapshot",
                retryable=True,
            )
        artifacts = [capabilities, details_artifact, ids_artifact, *page_artifacts]
        stats = {
            "page_count": len(page_artifacts),
            "feature_count": total_features,
            "page_size": page_size,
            "object_id_field": ids.object_id_field,
            "id_set_verified": True,
        }
        return self._finish(
            candidate,
            probe=probe,
            artifacts=artifacts,
            materialization={
                "kind": "feature-pages",
                "format": "geojson" if data_format == "geojson" else "esri-json",
                "layer_id": int(layer_id),
                "object_id_field": ids.object_id_field,
                "page_artifact_sha256": [item.blob.sha256 for item in page_artifacts],
            },
            feature_count=total_features,
            stats=stats,
            observed=last_result,
        )

    def _acquire_wcs(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        probe, capabilities, _document = self._probe(candidate)
        coverage = probe.canonical_name or ""
        version = probe.service_version or "2.0.1"
        image_format = _config_text(
            candidate.config,
            "format",
            default="image/tiff",
            max_chars=200,
        )
        if version.startswith("2."):
            params = {
                "service": "WCS",
                "request": "GetCoverage",
                "version": version,
                "coverageId": coverage,
                "format": image_format,
            }
        elif version.startswith("1.1"):
            params = {
                "service": "WCS",
                "request": "GetCoverage",
                "version": version,
                "identifier": coverage,
                "format": image_format,
            }
        elif version.startswith("1.0"):
            params = {
                "service": "WCS",
                "request": "GetCoverage",
                "version": version,
                "coverage": coverage,
                "format": image_format,
            }
        else:
            raise AcquisitionValidationError(
                "WCS advertised an unsupported version",
                code="unsupported_wcs_version",
            )
        extra = candidate.config.get("request_params", {})
        if not isinstance(extra, dict):
            raise AcquisitionConfigurationError("request_params must be an object")
        protected = {name.casefold() for name in params}
        for key, value in extra.items():
            values = value if isinstance(value, list) else [value]
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 100
                or key.casefold() in protected
                or not values
                or len(values) > 32
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item) > 2000
                    or any(ord(character) < 32 for character in key + item)
                    for item in values
                )
            ):
                raise AcquisitionConfigurationError(
                    "WCS request_params contains an invalid or protected value"
                )
            params[key] = values if len(values) > 1 else values[0]
        url = _merge_query(candidate.endpoint_url, params)
        downloaded = self._download(
            candidate,
            url,
            max_bytes=self.limits.max_dataset_bytes,
            accept="image/tiff, application/geotiff, application/octet-stream;q=0.5",
            allowed_media_types=_RASTER_MEDIA_TYPES,
            file_validator=_validate_raster_file,
            conditional=conditional,
        )
        if downloaded.result.not_modified:
            return self._unchanged(
                candidate,
                downloaded.result,
                probe=probe,
                artifacts=(capabilities,),
            )
        dataset = self._remote_artifact(
            downloaded,
            kind="dataset",
            role="input",
            source_version=version,
            metadata={
                "protocol": "wcs",
                "data_format": "geotiff",
                "coverage": coverage,
            },
        )
        style_artifacts = self._acquire_styles(candidate)
        artifacts = [capabilities, dataset, *style_artifacts]
        _enforce_total_bytes(artifacts, self.limits.max_total_bytes)
        materialization = {
            "kind": "raster",
            "format": "geotiff",
            "coverage": coverage,
            "dataset_sha256": dataset.blob.sha256,
        }
        stats = {"dataset_bytes": dataset.blob.size_bytes}
        style_digests = _style_digests(style_artifacts)
        if style_digests:
            stats["style_count"] = len(style_digests)
            materialization["style_artifact_sha256"] = style_digests
        return self._finish(
            candidate,
            probe=probe,
            artifacts=artifacts,
            materialization=materialization,
            feature_count=None,
            stats=stats,
            observed=downloaded.result,
        )

    def _acquire_download(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        download_url = _config_optional_text(
            candidate.config,
            "download_url",
            max_chars=8192,
        ) or candidate.endpoint_url
        media_type = _config_optional_text(
            candidate.config,
            "media_type",
            max_chars=200,
        )
        if media_type is None:
            raise AcquisitionConfigurationError(
                "direct download requires an explicit media_type"
            )
        data_format = _config_optional_text(
            candidate.config,
            "data_format",
            max_chars=100,
        )
        if data_format is None:
            raise AcquisitionConfigurationError(
                "direct download requires an explicit data_format"
            )
        allowed = frozenset({media_type.casefold()})
        downloaded = self._download(
            candidate,
            download_url,
            max_bytes=self.limits.max_dataset_bytes,
            accept=media_type,
            allowed_media_types=allowed,
            file_validator=_dataset_file_validator(data_format, self.limits),
            conditional=conditional,
        )
        if downloaded.result.not_modified:
            return self._unchanged(candidate, downloaded.result)
        dataset = self._remote_artifact(
            downloaded,
            kind="dataset",
            role="input",
            metadata={
                "protocol": "download",
                "data_format": data_format,
                "remote_name": candidate.remote_name,
            },
        )
        return self._finish(
            candidate,
            probe=None,
            artifacts=[dataset],
            materialization={
                "kind": "direct-dataset",
                "format": data_format,
                "dataset_sha256": dataset.blob.sha256,
            },
            feature_count=None,
            stats={"dataset_bytes": dataset.blob.size_bytes},
            observed=downloaded.result,
        )

    def _acquire_atom(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        feed = self._download(
            candidate,
            candidate.endpoint_url,
            max_bytes=self.limits.max_probe_bytes,
            accept="application/atom+xml, application/xml;q=0.9",
            allowed_media_types=_XML_MEDIA_TYPES,
            validator=lambda payload: _parse_atom_feed(
                payload,
                _config_optional_text(candidate.config, "entry_id", max_chars=1000)
                or candidate.remote_name,
            ),
        )
        enclosure_url = urljoin(feed.result.final_url, cast(str, feed.parsed))
        feed_artifact = self._remote_artifact(
            feed,
            kind="metadata",
            role="observation",
            metadata={
                "protocol": "atom",
                "remote_name": candidate.remote_name,
                "selected_url": _require_same_origin(candidate.endpoint_url, enclosure_url),
            },
        )
        media_type = _config_optional_text(candidate.config, "media_type", max_chars=200)
        if media_type is None:
            raise AcquisitionConfigurationError(
                "Atom dataset requires an explicit media_type"
            )
        data_format = _config_optional_text(
            candidate.config,
            "data_format",
            max_chars=100,
        )
        if data_format is None:
            raise AcquisitionConfigurationError(
                "Atom dataset requires an explicit data_format"
            )
        allowed = frozenset({media_type.casefold()})
        downloaded = self._download(
            candidate,
            enclosure_url,
            max_bytes=self.limits.max_dataset_bytes,
            accept=media_type,
            allowed_media_types=allowed,
            file_validator=_dataset_file_validator(data_format, self.limits),
            conditional=conditional,
        )
        if downloaded.result.not_modified:
            return self._unchanged(
                candidate,
                downloaded.result,
                artifacts=(feed_artifact,),
            )
        dataset = self._remote_artifact(
            downloaded,
            kind="dataset",
            role="input",
            metadata={
                "protocol": "atom",
                "data_format": data_format,
                "remote_name": candidate.remote_name,
            },
        )
        return self._finish(
            candidate,
            probe=None,
            artifacts=[feed_artifact, dataset],
            materialization={
                "kind": "direct-dataset",
                "format": data_format,
                "dataset_sha256": dataset.blob.sha256,
            },
            feature_count=None,
            stats={"dataset_bytes": dataset.blob.size_bytes},
            observed=downloaded.result,
        )

    def _acquire_tile_source(
        self,
        candidate: SourceCandidate,
        *,
        conditional: ConditionalRequest | None,
    ) -> AcquisitionResult:
        del conditional
        artifacts: list[AcquiredArtifact] = []
        probe: SourceProbe | None = None
        if candidate.protocol == "xyz":
            descriptor = _xyz_tile_descriptor(candidate)
        else:
            probe, capabilities, _document = self._probe(candidate)
            artifacts.append(capabilities)
            if candidate.protocol == "wmts":
                descriptor = _wmts_tile_descriptor(candidate, probe)
            else:
                descriptor = _wms_tile_descriptor(candidate, probe)
        tile_source = self._local_json_artifact(
            {
                "schema": "reference-tile-source/v1",
                "protocol": candidate.protocol,
                "definition_sha256": candidate.definition_sha256,
                "descriptor": descriptor,
            },
            kind="metadata",
            role="input",
            metadata={
                "schema": "reference-tile-source/v1",
                "protocol": candidate.protocol,
                "data_format": "tile-source",
            },
        )
        artifacts.append(tile_source)
        stats = {
            "seeded": False,
            "tile_source_bytes": tile_source.blob.size_bytes,
            "coverage_required": descriptor["coverage_required"],
        }
        return self._finish(
            candidate,
            probe=probe,
            artifacts=artifacts,
            materialization={
                "kind": "tile-source",
                "seeded": False,
                "descriptor_sha256": tile_source.blob.sha256,
                "bounds": descriptor["bounds"],
                "min_zoom": descriptor["min_zoom"],
                "max_zoom": descriptor["max_zoom"],
            },
            feature_count=None,
            stats=stats,
        )


def candidate_from_source_model(source: ReferenceLayerSource) -> SourceCandidate:
    """Freeze a persisted source into the discovery/probe value object."""

    if not isinstance(source, ReferenceLayerSource):
        raise TypeError("source must be a ReferenceLayerSource")
    if not source.enabled:
        raise AcquisitionConfigurationError(
            "disabled source cannot be acquired",
            code="source_disabled",
        )
    if source.protocol == "local":
        raise AcquisitionConfigurationError(
            "local source requires manual acquisition",
            code="manual_source",
        )
    if source.endpoint_url is None:
        raise AcquisitionConfigurationError(
            "non-manual source has no endpoint URL",
            code="missing_endpoint",
        )
    if not isinstance(source.config_json, dict):
        raise AcquisitionConfigurationError("source config must be an object")
    remote_name = source.remote_name
    if remote_name is None and source.protocol == "download":
        remote_name = source.source_key
    elif remote_name is None and source.protocol == "atom":
        remote_name = _config_optional_text(
            source.config_json,
            "entry_id",
            max_chars=1000,
        )
    if not isinstance(remote_name, str):
        raise AcquisitionConfigurationError("source has no remote collection name")
    candidate = SourceCandidate(
        protocol=cast(Any, source.protocol),
        target_kind=cast(Any, source.target_kind),
        endpoint_url=source.endpoint_url,
        remote_name=remote_name,
        sync_strategy=cast(Any, source.sync_strategy),
        priority=source.priority,
        config=dict(source.config_json),
        source_key=source.source_key,
        definition_sha256=source.definition_sha256,
    )
    _validate_candidate(candidate)
    return candidate


def persist_acquisition_result(
    db: Session,
    *,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
    result: AcquisitionResult,
    lease_token: str,
    now: datetime | None = None,
) -> tuple[ReferenceSourceArtifact, ...]:
    """Idempotently append artifacts and run links; never finalize the run.

    Storage keys are intentionally shareable by multiple source records.  The
    durable identity of a source artifact is ``(source, kind, sha256)``; the
    blob store itself provides content-addressed de-duplication.
    """

    if not isinstance(lease_token, str) or not lease_token or len(lease_token) > 64:
        raise AcquisitionPersistenceError(
            "lease token is invalid",
            code="lease_fenced",
        )
    if source.id is None or run.id is None:
        raise AcquisitionPersistenceError(
            "source and run must be persistent",
            code="missing_persistent_identity",
        )
    current_time = _aware_utc(now or datetime.now(timezone.utc))
    locked_source = db.scalar(
        select(ReferenceLayerSource)
        .where(ReferenceLayerSource.id == source.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_run = db.scalar(
        select(ReferenceSyncRun)
        .where(
            ReferenceSyncRun.id == run.id,
            ReferenceSyncRun.source_id == source.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_source is None or locked_run is None:
        raise AcquisitionPersistenceError(
            "source or sync run no longer exists",
            code="lease_fenced",
        )
    expires_at = locked_run.lease_expires_at
    if (
        not locked_source.enabled
        or locked_run.status != "running"
        or locked_run.lease_token != lease_token
        or expires_at is None
        or _aware_utc(expires_at) <= current_time
        or locked_run.source_definition_sha256 != locked_source.definition_sha256
    ):
        raise AcquisitionPersistenceError(
            "sync run lease or source definition is no longer current",
            code="lease_fenced",
        )
    try:
        candidate_from_source_model(locked_source)
    except AcquisitionConfigurationError as exc:
        raise AcquisitionPersistenceError(
            "persisted source definition no longer matches its digest",
            code="source_definition_mismatch",
        ) from exc
    if (
        result.source_key != locked_source.source_key
        or result.protocol != locked_source.protocol
        or result.source_definition_sha256 != locked_source.definition_sha256
        or result.source_definition_sha256 != locked_run.source_definition_sha256
    ):
        raise AcquisitionPersistenceError(
            "acquisition result does not belong to the source",
            code="result_source_mismatch",
        )
    persisted: list[ReferenceSourceArtifact] = []
    seen_links: set[tuple[int, str]] = set()
    for item in result.artifacts:
        existing = db.scalar(
            select(ReferenceSourceArtifact).where(
                ReferenceSourceArtifact.source_id == source.id,
                ReferenceSourceArtifact.artifact_kind == item.artifact_kind,
                ReferenceSourceArtifact.sha256 == item.blob.sha256,
            )
        )
        if existing is None:
            existing = ReferenceSourceArtifact(
                source_id=locked_source.id,
                artifact_kind=item.artifact_kind,
                source_url=item.source_url,
                final_url=item.final_url,
                source_version=item.source_version,
                upstream_etag=item.upstream_etag,
                upstream_last_modified=item.upstream_last_modified,
                media_type=item.media_type,
                storage_backend=item.blob.storage_backend,
                storage_key=item.blob.storage_key,
                size_bytes=item.blob.size_bytes,
                sha256=item.blob.sha256,
                metadata_json=item.metadata,
                retrieved_at=_aware_utc(item.retrieved_at),
            )
            db.add(existing)
            db.flush()
        elif (
            existing.storage_backend != item.blob.storage_backend
            or existing.storage_key != item.blob.storage_key
            or existing.size_bytes != item.blob.size_bytes
            or existing.media_type != item.media_type
        ):
            raise AcquisitionPersistenceError(
                "existing source artifact conflicts with immutable blob metadata",
                code="artifact_identity_conflict",
            )
        link_key = (existing.id, item.role)
        if link_key in seen_links:
            continue
        seen_links.add(link_key)
        link = db.get(
            ReferenceSyncRunArtifact,
            (locked_source.id, locked_run.id, *link_key),
        )
        if link is None:
            db.add(
                ReferenceSyncRunArtifact(
                    source_id=locked_source.id,
                    run_id=locked_run.id,
                    artifact_id=existing.id,
                    role=item.role,
                )
            )
        persisted.append(existing)
    db.flush()
    return tuple(persisted)


def _validate_run_snapshot(
    source: ReferenceLayerSource,
    run: ReferenceSyncRun | None,
) -> None:
    if run is None:
        return
    if source.id is None or run.source_id != source.id:
        raise AcquisitionConfigurationError(
            "sync run does not belong to the source",
            code="run_source_mismatch",
        )
    if run.status != "running":
        raise AcquisitionConfigurationError(
            "only a running sync run can acquire artifacts",
            code="run_not_running",
        )
    if run.source_definition_sha256 != source.definition_sha256:
        raise AcquisitionConfigurationError(
            "source definition changed after the run was queued",
            code="stale_source_definition",
        )


def _validate_candidate(candidate: SourceCandidate) -> None:
    if candidate.protocol not in {
        "wfs",
        "ogc_api_features",
        "arcgis_rest",
        "wcs",
        "download",
        "atom",
        "wmts",
        "xyz",
        "wms_tiles",
        "local",
    }:
        raise AcquisitionConfigurationError("source protocol is unsupported")
    expected_targets = {
        "wfs": "vector",
        "ogc_api_features": "vector",
        "arcgis_rest": "vector",
        "wcs": "raster",
        "download": {"vector", "raster"},
        "atom": {"vector", "raster"},
        "wmts": "tiles",
        "xyz": "tiles",
        "wms_tiles": "tiles",
    }
    expected = expected_targets.get(candidate.protocol)
    if expected is not None and (
        candidate.target_kind not in expected
        if isinstance(expected, set)
        else candidate.target_kind != expected
    ):
        raise AcquisitionConfigurationError("protocol and target kind do not match")
    allowed_strategies = {
        "wfs": {"paged_snapshot", "full_snapshot"},
        "ogc_api_features": {"paged_snapshot", "full_snapshot"},
        "arcgis_rest": {"paged_snapshot", "full_snapshot"},
        "wcs": {"full_snapshot", "conditional_get"},
        "download": {"full_snapshot", "conditional_get"},
        "atom": {"full_snapshot", "conditional_get"},
        "wmts": {"tile_seed"},
        "xyz": {"tile_seed"},
        "wms_tiles": {"tile_seed"},
        "local": {"manual"},
    }
    if candidate.sync_strategy not in allowed_strategies[candidate.protocol]:
        raise AcquisitionConfigurationError(
            "sync strategy is incompatible with the source protocol"
        )
    if not isinstance(candidate.config, dict):
        raise AcquisitionConfigurationError("source config must be an object")
    if (
        not isinstance(candidate.remote_name, str)
        or _SAFE_REMOTE_NAME_RE.fullmatch(candidate.remote_name) is None
    ):
        raise AcquisitionConfigurationError("remote collection name is invalid")
    if not isinstance(candidate.source_key, str) or not candidate.source_key.strip():
        raise AcquisitionConfigurationError("source key is invalid")
    if _SHA256_RE.fullmatch(candidate.definition_sha256) is None:
        raise AcquisitionConfigurationError("source definition digest is invalid")
    if source_candidate_definition_sha256(candidate) != candidate.definition_sha256:
        raise AcquisitionConfigurationError(
            "source definition digest does not match its effective configuration",
            code="source_definition_mismatch",
        )
    endpoint_for_validation = candidate.endpoint_url
    if candidate.protocol == "xyz":
        endpoint_for_validation = _ANY_TEMPLATE_TOKEN_RE.sub("0", endpoint_for_validation)
    normalize_https_url(endpoint_for_validation)
    _style_request_config(candidate)


def source_candidate_definition_sha256(candidate: SourceCandidate) -> str:
    """Hash exactly the effective definition used by source discovery."""

    try:
        encoded = json.dumps(
            candidate_definition(candidate),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AcquisitionConfigurationError(
            "source definition is not canonical JSON",
            code="invalid_source_definition",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_download_result(
    candidate: SourceCandidate,
    requested_url: str,
    result: HTTPSDownloadResult,
) -> None:
    if not isinstance(result, HTTPSDownloadResult):
        raise AcquisitionValidationError(
            "downloader returned an invalid result",
            code="invalid_download_result",
        )
    if normalize_https_url(result.source_url) != normalize_https_url(requested_url):
        raise AcquisitionValidationError(
            "downloader result source URL does not match the request",
            code="download_url_mismatch",
        )
    _require_same_origin(candidate.endpoint_url, result.final_url)
    if result.not_modified:
        if result.status_code != 304 or result.size_bytes != 0 or result.sha256 is not None:
            raise AcquisitionValidationError(
                "not-modified result has an invalid shape",
                code="invalid_not_modified",
            )
    elif (
        result.status_code != 200
        or result.size_bytes <= 0
        or result.sha256 is None
        or _SHA256_RE.fullmatch(result.sha256) is None
    ):
        raise AcquisitionValidationError(
            "download result has an invalid success shape",
            code="invalid_download_result",
        )


def _probe_request(
    candidate: SourceCandidate,
) -> tuple[str, str, frozenset[str]]:
    if candidate.protocol in {"wfs", "wcs", "wmts", "wms_tiles"}:
        service = "WMS" if candidate.protocol == "wms_tiles" else candidate.protocol.upper()
        return (
            _merge_query(
                candidate.endpoint_url,
                {"service": service, "request": "GetCapabilities"},
            ),
            "application/xml, text/xml;q=0.9",
            _XML_MEDIA_TYPES,
        )
    if candidate.protocol == "arcgis_rest":
        return (
            _merge_query(candidate.endpoint_url, {"f": "pjson"}),
            "application/json",
            _JSON_MEDIA_TYPES,
        )
    if candidate.protocol == "ogc_api_features":
        path = urlsplit(candidate.endpoint_url).path.rstrip("/")
        url = (
            candidate.endpoint_url
            if path.casefold().endswith("/collections")
            else _append_path(candidate.endpoint_url, "collections")
        )
        return (url, "application/json", _JSON_MEDIA_TYPES)
    raise AcquisitionConfigurationError("protocol has no capabilities probe")


def _origin(value: str) -> str:
    normalized = normalize_https_url(_ANY_TEMPLATE_TOKEN_RE.sub("0", value))
    parts = urlsplit(normalized)
    hostname = parts.hostname or ""
    rendered = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{rendered}"


def _require_same_origin(base: str, value: str) -> str:
    normalized = normalize_https_url(value)
    if _origin(base) != _origin(normalized):
        raise AcquisitionConfigurationError(
            "derived source URL crosses the reviewed origin",
            code="cross_origin_url",
        )
    return normalized


def _merge_query(
    base: str,
    params: Mapping[str, str | list[str]],
) -> str:
    normalized = normalize_https_url(base)
    parts = urlsplit(normalized)
    existing = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    names = [name.casefold() for name, _value in existing]
    if len(names) != len(set(names)):
        raise AcquisitionConfigurationError("source endpoint has duplicate query keys")
    protected = {name.casefold() for name in params}
    if protected.intersection(names):
        raise AcquisitionConfigurationError(
            "source endpoint predefines a protected request parameter"
        )
    additions: list[tuple[str, str]] = []
    for key, raw_value in params.items():
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        if not values or len(values) > 32:
            raise AcquisitionConfigurationError("request parameter is invalid")
        for value in values:
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 100
                or not isinstance(value, str)
                or len(value) > 8192
                or any(ord(character) < 32 for character in key + value)
            ):
                raise AcquisitionConfigurationError("request parameter is invalid")
            additions.append((key, value))
    query = urlencode([*existing, *additions], doseq=False, safe=":,/*")
    result = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    if len(result) > 8192:
        raise AcquisitionLimitError("request URL is too long", code="request_url_too_long")
    return result


def _replace_query_value(url: str, name: str, value: str) -> str:
    normalized = normalize_https_url(url)
    parts = urlsplit(normalized)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    replaced = False
    result_pairs: list[tuple[str, str]] = []
    for key, current in pairs:
        if key.casefold() == name.casefold():
            if replaced:
                raise AcquisitionConfigurationError("pagination URL repeats its offset")
            result_pairs.append((key, value))
            replaced = True
        else:
            result_pairs.append((key, current))
    if not replaced:
        result_pairs.append((name, value))
    query = urlencode(result_pairs, safe=":,/*")
    result = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    if len(result) > 8192:
        raise AcquisitionLimitError("pagination URL is too long", code="request_url_too_long")
    return result


def _append_path(base: str, segment: str) -> str:
    if not isinstance(segment, str) or not segment or "/" in segment or segment in {".", ".."}:
        raise AcquisitionConfigurationError("derived source path segment is invalid")
    normalized = normalize_https_url(base)
    parts = urlsplit(normalized)
    path = parts.path.rstrip("/") + "/" + quote(segment, safe="-._~")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _http_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcquisitionValidationError(
            "upstream Last-Modified header is invalid",
            code="invalid_last_modified",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AcquisitionPersistenceError(
            "persistent timestamp is timezone-naive",
            code="invalid_timestamp",
        )
    return value.astimezone(timezone.utc)


def _config_optional_text(
    config: Mapping[str, Any],
    name: str,
    *,
    max_chars: int,
    allow_empty: bool = False,
) -> str | None:
    value = config.get(name)
    if value is None:
        return None
    normalized = value.strip() if isinstance(value, str) else None
    if (
        not isinstance(value, str)
        or (not normalized and not allow_empty)
        or len(value) > max_chars
        or any(ord(character) < 32 for character in value)
    ):
        raise AcquisitionConfigurationError(f"source config {name} is invalid")
    return normalized


def _config_text(
    config: Mapping[str, Any],
    name: str,
    *,
    default: str,
    max_chars: int,
) -> str:
    return _config_optional_text(config, name, max_chars=max_chars) or default


def _style_request_config(candidate: SourceCandidate) -> _StyleRequest | None:
    config = candidate.config
    has_endpoint = "style_endpoint_url" in config
    has_layer = "style_layer_name" in config
    has_styles = "styles" in config
    if not any((has_endpoint, has_layer, has_styles)):
        return None
    if candidate.protocol not in {"wfs", "wcs"}:
        raise AcquisitionConfigurationError(
            "style acquisition is only supported for GeoServer data sources"
        )
    if not all((has_endpoint, has_layer, has_styles)):
        raise AcquisitionConfigurationError(
            "style acquisition config is incomplete"
        )
    endpoint = _config_optional_text(
        config,
        "style_endpoint_url",
        max_chars=8192,
    )
    layer_name = _config_optional_text(
        config,
        "style_layer_name",
        max_chars=1000,
    )
    if endpoint is None or layer_name is None:
        raise AcquisitionConfigurationError(
            "style acquisition config is incomplete"
        )
    if _SAFE_REMOTE_NAME_RE.fullmatch(layer_name) is None:
        raise AcquisitionConfigurationError("style layer name is invalid")
    if layer_name != candidate.remote_name:
        raise AcquisitionConfigurationError(
            "style layer name does not match the acquired collection"
        )
    endpoint = _require_same_origin(candidate.endpoint_url, endpoint)
    raw_styles = config.get("styles")
    if not isinstance(raw_styles, list):
        raise AcquisitionConfigurationError("source config styles must be a list")
    if len(raw_styles) > _MAX_STYLES_PER_SOURCE:
        raise AcquisitionLimitError(
            "source declares too many catalog styles",
            code="style_count_limit",
        )
    styles: list[_StyleSpec] = []
    source_keys: set[str] = set()
    remote_names: set[str] = set()
    for raw_style in raw_styles:
        if not isinstance(raw_style, dict) or set(raw_style) != {
            "catalog_style_source_key",
            "remote_name",
        }:
            raise AcquisitionConfigurationError(
                "source config contains an invalid style identity"
            )
        source_key = raw_style.get("catalog_style_source_key")
        remote_name = raw_style.get("remote_name")
        if (
            not isinstance(source_key, str)
            or _STYLE_SOURCE_KEY_RE.fullmatch(source_key) is None
            or not isinstance(remote_name, str)
            or _STYLE_NAME_RE.fullmatch(remote_name) is None
        ):
            raise AcquisitionConfigurationError(
                "source config contains an invalid style identity"
            )
        if source_key in source_keys or remote_name in remote_names:
            raise AcquisitionConfigurationError(
                "source config repeats a catalog or remote style identity"
            )
        source_keys.add(source_key)
        remote_names.add(remote_name)
        styles.append(
            _StyleSpec(
                catalog_style_source_key=source_key,
                remote_name=remote_name,
            )
        )
    if styles != sorted(styles, key=lambda item: item.catalog_style_source_key):
        raise AcquisitionConfigurationError(
            "source config styles are not in canonical order"
        )
    validation_params: dict[str, str] = {
        "service": "WMS",
        "request": "GetStyles",
        "version": "1.1.1",
        "layers": layer_name,
    }
    _merge_query(endpoint, validation_params)
    return _StyleRequest(
        endpoint_url=endpoint,
        layer_name=layer_name,
        styles=tuple(styles),
    )


def _configured_page_size(config: Mapping[str, Any], default: int) -> int:
    value = config.get("page_size", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= default:
        raise AcquisitionConfigurationError(
            f"source page_size must be between 1 and {default}"
        )
    return value


def _strict_json(document: bytes) -> Any:
    if not isinstance(document, bytes) or not document:
        raise AcquisitionValidationError("JSON response is empty")

    def unique_object(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AcquisitionValidationError("response is not strict UTF-8 JSON") from exc
    stack = [(value, 0)]
    seen = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if seen > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise AcquisitionLimitError(
                "JSON response is too complex",
                code="json_complexity_limit",
            )
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise AcquisitionValidationError("JSON contains a non-finite number")
    return value


def _parse_feature_collection(
    document: bytes,
    *,
    page_size: int,
    allow_next: bool,
) -> _FeaturePage:
    root = _strict_json(document)
    if not isinstance(root, dict) or root.get("type") != "FeatureCollection":
        raise AcquisitionValidationError("response is not a GeoJSON FeatureCollection")
    features = root.get("features")
    if not isinstance(features, list):
        raise AcquisitionValidationError("FeatureCollection has no feature array")
    if len(features) > page_size:
        raise AcquisitionLimitError(
            "feature page exceeds the requested page size",
            code="page_feature_limit",
        )
    feature_ids: list[str] = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise AcquisitionValidationError("feature page contains a malformed feature")
        if "properties" in feature and not isinstance(feature["properties"], (dict, type(None))):
            raise AcquisitionValidationError("GeoJSON feature properties are malformed")
        if "geometry" in feature and not isinstance(feature["geometry"], (dict, type(None))):
            raise AcquisitionValidationError("GeoJSON feature geometry is malformed")
        identifier = feature.get("id")
        if identifier is not None:
            if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
                raise AcquisitionValidationError("GeoJSON feature id is malformed")
            normalized = str(identifier)
            if not normalized or len(normalized) > 1000:
                raise AcquisitionValidationError("GeoJSON feature id is malformed")
            feature_ids.append(normalized)
    if len(feature_ids) != len(set(feature_ids)):
        raise AcquisitionValidationError("feature page repeats a feature id")
    returned = root.get("numberReturned")
    if returned is not None and (
        isinstance(returned, bool) or not isinstance(returned, int) or returned != len(features)
    ):
        raise AcquisitionValidationError("numberReturned does not match the feature page")
    matched = _optional_count(root.get("numberMatched"), name="numberMatched")
    if matched is None:
        matched = _optional_count(root.get("totalFeatures"), name="totalFeatures")
    if matched is not None and matched < len(features):
        raise AcquisitionValidationError("matched feature count is smaller than the page")
    next_url = None
    links = root.get("links")
    if links is not None:
        if not isinstance(links, list):
            raise AcquisitionValidationError("FeatureCollection links are malformed")
        if allow_next:
            if len(links) > 100:
                raise AcquisitionLimitError(
                    "FeatureCollection has too many links",
                    code="link_limit",
                )
            matches: list[str] = []
            for link in links:
                if not isinstance(link, dict):
                    raise AcquisitionValidationError("FeatureCollection link is malformed")
                rel = link.get("rel")
                href = link.get("href")
                if isinstance(rel, str) and rel.casefold() == "next":
                    if not isinstance(href, str) or not href or len(href) > 8192:
                        raise AcquisitionValidationError("next-page link is malformed")
                    matches.append(href)
            if len(matches) > 1:
                raise AcquisitionValidationError("FeatureCollection has multiple next links")
            next_url = matches[0] if matches else None
    return _FeaturePage(
        count=len(features),
        number_matched=matched,
        next_url=next_url,
        feature_ids=tuple(feature_ids),
    )


def _optional_count(value: Any, *, name: str) -> int | None:
    if value is None or (isinstance(value, str) and value == "unknown"):
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcquisitionValidationError(f"{name} is invalid")
    return value


def _parse_arcgis_details(document: bytes, expected_id: int) -> dict[str, Any]:
    root = _strict_json(document)
    if not isinstance(root, dict) or "error" in root:
        raise AcquisitionValidationError("ArcGIS layer metadata is malformed")
    identifier = root.get("id")
    if identifier is not None and identifier != expected_id:
        raise AcquisitionValidationError("ArcGIS layer metadata has the wrong id")
    object_id_field = root.get("objectIdField") or root.get("objectIdFieldName")
    if object_id_field is not None and (
        not isinstance(object_id_field, str)
        or not object_id_field
        or len(object_id_field) > 500
    ):
        raise AcquisitionValidationError("ArcGIS object-id field is malformed")
    maximum = root.get("maxRecordCount")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0
    ):
        raise AcquisitionValidationError("ArcGIS maxRecordCount is malformed")
    return root


def _parse_arcgis_ids(document: bytes, max_features: int) -> _ArcGISIds:
    root = _strict_json(document)
    if not isinstance(root, dict) or "error" in root:
        raise AcquisitionValidationError("ArcGIS object-id response is malformed")
    field = root.get("objectIdFieldName") or root.get("objectIdField")
    values = root.get("objectIds")
    if not isinstance(field, str) or not field or len(field) > 500 or not isinstance(values, list):
        raise AcquisitionValidationError("ArcGIS object-id response is malformed")
    if len(values) > max_features:
        raise AcquisitionLimitError(
            "ArcGIS source exceeds the feature limit",
            code="feature_limit",
        )
    identifiers: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AcquisitionValidationError("ArcGIS object id is malformed")
        identifiers.append(value)
    if len(identifiers) != len(set(identifiers)):
        raise AcquisitionValidationError("ArcGIS returned duplicate object ids")
    return _ArcGISIds(object_id_field=field, object_ids=tuple(sorted(identifiers)))


def _parse_arcgis_geojson_page(
    document: bytes,
    expected_ids: tuple[int, ...],
    object_id_field: str,
) -> int:
    page = _parse_feature_collection(
        document,
        page_size=len(expected_ids),
        allow_next=False,
    )
    root = cast(dict[str, Any], _strict_json(document))
    observed = [
        _arcgis_feature_id(
            feature,
            object_id_field=object_id_field,
            properties_key="properties",
        )
        for feature in root["features"]
    ]
    if page.count != len(expected_ids) or tuple(sorted(observed)) != expected_ids:
        raise AcquisitionValidationError(
            "ArcGIS GeoJSON page does not match its requested object ids",
            code="unstable_snapshot",
            retryable=True,
        )
    return page.count


def _parse_arcgis_json_page(
    document: bytes,
    expected_ids: tuple[int, ...],
    object_id_field: str,
) -> int:
    root = _strict_json(document)
    if not isinstance(root, dict) or "error" in root or not isinstance(root.get("features"), list):
        raise AcquisitionValidationError("ArcGIS feature page is malformed")
    features = root["features"]
    if any(not isinstance(item, dict) for item in features):
        raise AcquisitionValidationError("ArcGIS feature page contains a malformed feature")
    observed = [
        _arcgis_feature_id(
            feature,
            object_id_field=object_id_field,
            properties_key="attributes",
        )
        for feature in features
    ]
    if len(features) != len(expected_ids) or tuple(sorted(observed)) != expected_ids:
        raise AcquisitionValidationError(
            "ArcGIS feature page does not match its requested object ids",
            code="unstable_snapshot",
            retryable=True,
        )
    return len(features)


def _arcgis_feature_id(
    feature: dict[str, Any],
    *,
    object_id_field: str,
    properties_key: str,
) -> int:
    candidates: list[Any] = []
    if "id" in feature:
        candidates.append(feature["id"])
    properties = feature.get(properties_key)
    if isinstance(properties, dict):
        field_matches = [
            value
            for key, value in properties.items()
            if key.casefold() == object_id_field.casefold()
        ]
        if len(field_matches) > 1:
            raise AcquisitionValidationError("ArcGIS feature repeats its object-id field")
        candidates.extend(field_matches)
    normalized: list[int] = []
    for value in candidates:
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AcquisitionValidationError("ArcGIS feature object id is malformed")
        normalized.append(value)
    if not normalized or len(set(normalized)) != 1:
        raise AcquisitionValidationError("ArcGIS feature has no unambiguous object id")
    return normalized[0]


def _arcgis_id_chunks(
    values: tuple[int, ...],
    *,
    page_size: int,
    base_url: str,
) -> tuple[tuple[int, ...], ...]:
    chunks: list[tuple[int, ...]] = []
    current: list[int] = []
    for value in values:
        candidate = [*current, value]
        too_long = False
        try:
            rendered = _merge_query(
                base_url,
                {
                    "objectIds": ",".join(str(item) for item in candidate),
                    "outFields": "*",
                    "returnGeometry": "true",
                    "f": "geojson",
                },
            )
            too_long = len(rendered) > 8000
        except AcquisitionLimitError as exc:
            if exc.code != "request_url_too_long":
                raise
            too_long = True
        if len(candidate) > page_size or too_long:
            if not current:
                raise AcquisitionLimitError(
                    "one ArcGIS object id cannot fit in a safe request URL",
                    code="request_url_too_long",
                )
            chunks.append(tuple(current))
            current = [value]
        else:
            current = candidate
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _validate_raster_file(path: Path, size_bytes: int) -> None:
    def invalid(message: str) -> AcquisitionValidationError:
        return AcquisitionValidationError(message, code="invalid_raster")

    try:
        with path.open("rb") as source:
            header = source.read(8)
            if len(header) != 8 or header[:2] not in {b"II", b"MM"}:
                raise invalid("raster has an invalid TIFF header")
            byte_order = "<" if header[:2] == b"II" else ">"
            magic, first_ifd = struct.unpack(f"{byte_order}HI", header[2:])
            if magic != 42 or not 8 <= first_ifd <= size_bytes - 6:
                raise invalid("raster has an invalid TIFF directory")
            source.seek(first_ifd)
            count_raw = source.read(2)
            if len(count_raw) != 2:
                raise invalid("raster TIFF directory is truncated")
            entry_count = struct.unpack(f"{byte_order}H", count_raw)[0]
            if entry_count == 0 or entry_count > 4096:
                raise invalid("raster TIFF directory count is invalid")
            if first_ifd + 2 + entry_count * 12 + 4 > size_bytes:
                raise invalid("raster TIFF directory exceeds the file")
            width = None
            height = None
            tags: set[int] = set()
            type_sizes = {
                1: 1,
                2: 1,
                3: 2,
                4: 4,
                5: 8,
                6: 1,
                7: 1,
                8: 2,
                9: 4,
                10: 8,
                11: 4,
                12: 8,
                13: 4,
                16: 8,
                17: 8,
                18: 8,
            }
            for _index in range(entry_count):
                entry = source.read(12)
                if len(entry) != 12:
                    raise invalid("raster TIFF directory is truncated")
                tag, value_type, value_count = struct.unpack(
                    f"{byte_order}HHI",
                    entry[:8],
                )
                tags.add(tag)
                type_size = type_sizes.get(value_type)
                if type_size is None or value_count == 0:
                    raise invalid("raster TIFF entry has an unsupported value type")
                value_bytes = type_size * value_count
                if value_bytes > size_bytes:
                    raise invalid("raster TIFF entry exceeds the file")
                if value_bytes > 4:
                    value_offset = struct.unpack(f"{byte_order}I", entry[8:12])[0]
                    if value_offset > size_bytes - value_bytes:
                        raise invalid("raster TIFF value points outside the file")
                if tag in {256, 257} and value_count == 1 and value_type in {3, 4}:
                    value = (
                        struct.unpack(f"{byte_order}H", entry[8:10])[0]
                        if value_type == 3
                        else struct.unpack(f"{byte_order}I", entry[8:12])[0]
                    )
                    if tag == 256:
                        width = value
                    else:
                        height = value
    except OSError as exc:
        raise AcquisitionValidationError(
            "staged raster could not be inspected",
            code="staging_read_failed",
        ) from exc
    if (
        size_bytes < 14
        or width is None
        or height is None
        or width <= 0
        or height <= 0
        or 34735 not in tags
        or not ({33550, 33922}.issubset(tags) or 34264 in tags)
    ):
        raise AcquisitionValidationError(
            "WCS response is not a structurally georeferenced GeoTIFF",
            code="invalid_raster",
        )


def _dataset_file_validator(
    data_format: str,
    limits: AcquisitionLimits,
) -> FileValidator:
    normalized = data_format.strip().casefold()
    if normalized in {"zip", "shapefile-zip"}:
        return lambda path, size: _validate_zip_dataset(
            path,
            size,
            require_shapefile=normalized == "shapefile-zip",
            maximum_uncompressed=limits.max_total_bytes,
        )
    if normalized in {"geopackage", "gpkg"}:
        return _validate_geopackage
    if normalized in {"geotiff", "tiff"}:
        return _validate_raster_file
    if normalized == "flatgeobuf":
        return _validate_flatgeobuf
    if normalized == "geojson":
        return lambda path, size: _validate_geojson_dataset(path, size, limits)
    raise AcquisitionConfigurationError(
        "data_format has no fail-closed acquisition validator",
        code="unsupported_data_format",
    )


def _validate_zip_dataset(
    path: Path,
    size_bytes: int,
    *,
    require_shapefile: bool,
    maximum_uncompressed: int,
) -> None:
    if size_bytes < 22:
        raise AcquisitionValidationError("ZIP dataset is too small")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 100_000:
                raise AcquisitionValidationError("ZIP dataset entry count is invalid")
            total_uncompressed = 0
            shapefile_parts: dict[str, set[str]] = {}
            for entry in entries:
                name = entry.filename
                pure = PurePosixPath(name.replace("\\", "/"))
                if (
                    not name
                    or len(name) > 4096
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                    or entry.flag_bits & 0x1
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise AcquisitionValidationError("ZIP dataset has an unsafe entry")
                if entry.is_dir():
                    continue
                total_uncompressed += entry.file_size
                if total_uncompressed > maximum_uncompressed:
                    raise AcquisitionLimitError(
                        "ZIP dataset exceeds the uncompressed byte limit",
                        code="archive_expansion_limit",
                    )
                suffix = pure.suffix.casefold()
                if suffix in {".shp", ".shx", ".dbf"}:
                    key = str(pure.with_suffix("")).casefold()
                    shapefile_parts.setdefault(key, set()).add(suffix)
            if require_shapefile and not any(
                {".shp", ".shx", ".dbf"}.issubset(parts)
                for parts in shapefile_parts.values()
            ):
                raise AcquisitionValidationError(
                    "shapefile ZIP lacks a matching SHP/SHX/DBF set"
                )
            corrupt = archive.testzip()
            if corrupt is not None:
                raise AcquisitionValidationError("ZIP dataset contains a corrupt entry")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ReferenceAcquisitionError):
            raise
        raise AcquisitionValidationError("dataset is not a valid ZIP archive") from exc


def _validate_geopackage(path: Path, size_bytes: int) -> None:
    if size_bytes < 100:
        raise AcquisitionValidationError("GeoPackage is too small")
    try:
        with path.open("rb") as source:
            if source.read(16) != b"SQLite format 3\x00":
                raise AcquisitionValidationError("GeoPackage has no SQLite header")
        uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if check != ("ok",):
                raise AcquisitionValidationError("GeoPackage integrity check failed")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "name IN ('gpkg_spatial_ref_sys', 'gpkg_contents')"
                )
            }
            if tables != {"gpkg_spatial_ref_sys", "gpkg_contents"}:
                raise AcquisitionValidationError("SQLite dataset is not a GeoPackage")
    except sqlite3.Error as exc:
        raise AcquisitionValidationError("GeoPackage cannot be read safely") from exc


def _validate_flatgeobuf(path: Path, size_bytes: int) -> None:
    try:
        with path.open("rb") as source:
            prefix = source.read(8)
    except OSError as exc:
        raise AcquisitionValidationError("FlatGeobuf could not be inspected") from exc
    if size_bytes < 16 or prefix != b"fgb\x03fgb\x01":
        raise AcquisitionValidationError("dataset is not FlatGeobuf")


def _validate_geojson_dataset(
    path: Path,
    size_bytes: int,
    limits: AcquisitionLimits,
) -> None:
    if size_bytes > limits.max_page_bytes:
        raise AcquisitionLimitError(
            "direct GeoJSON exceeds the bounded parser limit",
            code="geojson_parser_limit",
        )
    try:
        document = path.read_bytes()
    except OSError as exc:
        raise AcquisitionValidationError("GeoJSON could not be inspected") from exc
    _parse_feature_collection(
        document,
        page_size=limits.max_features,
        allow_next=False,
    )


def _parse_style_bundle(
    document: bytes,
    *,
    layer_name: str,
    style_names: tuple[str, ...],
) -> _ParsedStyleBundle:
    if (
        not isinstance(document, bytes)
        or not document
        or len(document) > MAX_PROBE_BYTES
        or b"\x00" in document
    ):
        raise AcquisitionValidationError(
            "GetStyles returned an empty or oversized SLD",
            code="invalid_sld",
        )
    lowered = document.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise AcquisitionValidationError(
            "SLD declarations cannot define DTDs or entities",
            code="unsafe_sld_xml",
        )
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise AcquisitionValidationError(
            "GetStyles returned malformed XML",
            code="invalid_sld",
        ) from exc
    if root.tag != f"{{{_SLD_NAMESPACE}}}StyledLayerDescriptor":
        raise AcquisitionValidationError(
            "GetStyles did not return an SLD document",
            code="invalid_sld",
        )
    version = root.get("version")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 32
        or any(ord(character) < 32 for character in version)
    ):
        raise AcquisitionValidationError(
            "SLD version is missing or invalid",
            code="invalid_sld",
        )

    stack = [(root, 1)]
    element_count = 0
    text_bytes = 0
    blocked_elements = {
        "externalgraphic",
        "externalmark",
        "onlineresource",
        "remoteows",
        "include",
        "fallback",
    }
    while stack:
        element, depth = stack.pop()
        element_count += 1
        if element_count > _MAX_SLD_ELEMENTS or depth > _MAX_SLD_DEPTH:
            raise AcquisitionLimitError(
                "SLD exceeds its XML complexity limit",
                code="sld_complexity_limit",
            )
        local_element = _xml_local(element.tag).casefold()
        if local_element in blocked_elements:
            raise AcquisitionValidationError(
                "SLD references an external or auxiliary resource",
                code="unsafe_sld_reference",
            )
        for attribute, raw_value in element.attrib.items():
            if not isinstance(raw_value, str) or len(raw_value) > MAX_PROBE_BYTES:
                raise AcquisitionLimitError(
                    "SLD attribute exceeds its XML complexity limit",
                    code="sld_complexity_limit",
                )
            local_attribute = _xml_local(attribute).casefold()
            if local_attribute in {"href", "src", "url", "uri"}:
                raise AcquisitionValidationError(
                    "SLD references an external or auxiliary resource",
                    code="unsafe_sld_reference",
                )
            # XML schema locations are validation hints rather than runtime
            # style dependencies.  They are removed from standalone output.
            if local_attribute not in {
                "schemalocation",
                "nonamespaceschemalocation",
            } and _EXTERNAL_SLD_TEXT_RE.search(raw_value):
                raise AcquisitionValidationError(
                    "SLD contains an external resource locator",
                    code="unsafe_sld_reference",
                )
            text_bytes += len(raw_value.encode("utf-8"))
        for value in (element.text, element.tail):
            if value:
                text_bytes += len(value.encode("utf-8"))
                if _EXTERNAL_SLD_TEXT_RE.search(value):
                    raise AcquisitionValidationError(
                        "SLD contains an external resource locator",
                        code="unsafe_sld_reference",
                    )
        if text_bytes > _MAX_SLD_TEXT_BYTES:
            raise AcquisitionLimitError(
                "SLD exceeds its XML text limit",
                code="sld_complexity_limit",
            )
        stack.extend((child, depth + 1) for child in element)

    named_layers = [
        child
        for child in root
        if _xml_local(child.tag) == "NamedLayer"
        and _direct_sld_name(child) == layer_name
    ]
    if len(named_layers) != 1:
        raise AcquisitionValidationError(
            "GetStyles did not return the exact requested layer",
            code="style_layer_unavailable",
        )
    named_layer = named_layers[0]
    standalone_slds: list[tuple[str, bytes]] = []
    standalone_total_bytes = 0
    for style_name in style_names:
        matching_styles = [
            child
            for child in named_layer
            if _xml_local(child.tag) == "UserStyle"
            and _direct_sld_name(child) == style_name
        ]
        if len(matching_styles) != 1:
            raise AcquisitionValidationError(
                "GetStyles did not return the exact requested style",
                code="style_unavailable",
            )

        standalone_root = copy.deepcopy(root)
        for child in list(standalone_root):
            standalone_root.remove(child)
        standalone_layer = copy.deepcopy(named_layer)
        for child in list(standalone_layer):
            if _xml_local(child.tag) == "UserStyle" and (
                _direct_sld_name(child) != style_name
            ):
                standalone_layer.remove(child)
            elif _xml_local(child.tag) == "NamedStyle":
                standalone_layer.remove(child)
        standalone_root.append(standalone_layer)
        for element in standalone_root.iter():
            for attribute in list(element.attrib):
                if _xml_local(attribute).casefold() in {
                    "schemalocation",
                    "nonamespaceschemalocation",
                }:
                    del element.attrib[attribute]
        standalone = ElementTree.tostring(
            standalone_root,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )
        if not standalone or len(standalone) > MAX_PROBE_BYTES:
            raise AcquisitionLimitError(
                "standalone SLD exceeds its byte limit",
                code="sld_size_limit",
            )
        standalone_slds.append((style_name, standalone))
        standalone_total_bytes += len(standalone)
        if standalone_total_bytes > _MAX_SLD_EXTRACTED_BYTES:
            raise AcquisitionLimitError(
                "standalone SLD bundle exceeds its aggregate byte limit",
                code="sld_size_limit",
            )
    return _ParsedStyleBundle(
        sld_version=version,
        standalone_slds=tuple(standalone_slds),
    )


def _direct_sld_name(element: ElementTree.Element) -> str | None:
    names = [
        (child.text or "").strip()
        for child in element
        if _xml_local(child.tag) == "Name" and (child.text or "").strip()
    ]
    if len(names) != 1 or len(names[0]) > 1000:
        return None
    return names[0]


def _parse_atom_feed(document: bytes, remote_name: str) -> str:
    lowered = document.lower()
    if (
        not document
        or len(document) > MAX_PROBE_BYTES
        or b"<!doctype" in lowered
        or b"<!entity" in lowered
        or b"\x00" in document
    ):
        raise AcquisitionValidationError("Atom feed is unsafe or oversized")
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise AcquisitionValidationError("Atom feed XML is malformed") from exc
    if _xml_local(root.tag) != "feed":
        raise AcquisitionValidationError("response is not an Atom feed")
    elements = list(root.iter())
    if len(elements) > 100_000:
        raise AcquisitionLimitError("Atom feed has too many elements", code="xml_complexity_limit")
    enclosure_matches: list[str] = []
    alternate_matches: list[str] = []
    for entry in (item for item in elements if _xml_local(item.tag) == "entry"):
        identities = {
            (child.text or "").strip()
            for child in entry
            if _xml_local(child.tag) in {"id", "title"} and (child.text or "").strip()
        }
        if remote_name not in identities:
            continue
        for link in entry:
            if _xml_local(link.tag) != "link":
                continue
            rel = (link.get("rel") or "alternate").casefold()
            href = link.get("href")
            if isinstance(href, str) and href:
                if rel == "enclosure":
                    enclosure_matches.append(href)
                elif rel == "alternate":
                    alternate_matches.append(href)
    matches = enclosure_matches or alternate_matches
    if len(matches) != 1:
        raise AcquisitionValidationError(
            "Atom feed does not resolve one unambiguous dataset link",
            code="atom_link_unavailable",
        )
    return matches[0]


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ogc_items_url(
    candidate: SourceCandidate,
    collection: str,
    page_size: int,
) -> str:
    configured = _config_optional_text(candidate.config, "items_url", max_chars=8192)
    if configured is not None:
        base = configured.replace("{collection}", quote(collection, safe="-._~"))
        if _ANY_TEMPLATE_TOKEN_RE.search(base):
            raise AcquisitionConfigurationError("items_url contains an unknown placeholder")
    else:
        normalized = normalize_https_url(candidate.endpoint_url)
        path = urlsplit(normalized).path.rstrip("/")
        if path.casefold().endswith("/collections"):
            base = _append_path(_append_path(normalized, collection), "items")
        else:
            base = _append_path(
                _append_path(_append_path(normalized, "collections"), collection),
                "items",
            )
    return _merge_query(base, {"limit": str(page_size), "offset": "0", "f": "json"})


def _tile_common(candidate: SourceCandidate) -> dict[str, Any]:
    bounds = candidate.config.get("bounds")
    if not isinstance(bounds, dict) or set(bounds) != {"west", "south", "east", "north"}:
        raise AcquisitionConfigurationError("tile source bounds are missing or malformed")
    normalized_bounds: dict[str, float] = {}
    for name in ("west", "south", "east", "north"):
        value = bounds[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise AcquisitionConfigurationError("tile source bounds are not finite")
        normalized_bounds[name] = float(value)
    if (
        not -180 <= normalized_bounds["west"] < normalized_bounds["east"] <= 180
        or not -90 <= normalized_bounds["south"] < normalized_bounds["north"] <= 90
    ):
        raise AcquisitionConfigurationError("tile source bounds are outside WGS84")
    min_zoom = candidate.config.get("min_zoom")
    max_zoom = candidate.config.get("max_zoom")
    if (
        isinstance(min_zoom, bool)
        or not isinstance(min_zoom, int)
        or isinstance(max_zoom, bool)
        or not isinstance(max_zoom, int)
        or not 0 <= min_zoom <= max_zoom <= 30
    ):
        raise AcquisitionConfigurationError("tile source zoom range is invalid")
    coverage_required = candidate.config.get("coverage_required", True)
    if not isinstance(coverage_required, bool):
        raise AcquisitionConfigurationError("coverage_required must be boolean")
    max_tile_count = candidate.config.get("max_tile_count", _DEFAULT_MAX_TILE_COUNT)
    if (
        isinstance(max_tile_count, bool)
        or not isinstance(max_tile_count, int)
        or not 1 <= max_tile_count <= _ABSOLUTE_MAX_TILE_COUNT
    ):
        raise AcquisitionConfigurationError("max_tile_count is outside its safe range")
    estimated_tile_count = _estimate_web_mercator_tile_count(
        normalized_bounds,
        min_zoom,
        max_zoom,
    )
    if estimated_tile_count > max_tile_count:
        raise AcquisitionLimitError(
            "tile coverage exceeds the reviewed seed limit",
            code="tile_count_limit",
        )
    return {
        "bounds": normalized_bounds,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "format": _config_text(
            candidate.config,
            "format",
            default="image/png",
            max_chars=200,
        ),
        "coverage_required": coverage_required,
        "estimated_tile_count": estimated_tile_count,
        "max_tile_count": max_tile_count,
    }


def _estimate_web_mercator_tile_count(
    bounds: dict[str, float],
    min_zoom: int,
    max_zoom: int,
) -> int:
    total = 0
    for zoom in range(min_zoom, max_zoom + 1):
        west, east, north, south = _web_mercator_tile_window(bounds, zoom)
        total += (east - west + 1) * (south - north + 1)
        if total > _ABSOLUTE_MAX_TILE_COUNT:
            return total
    return total


def _web_mercator_tile_window(
    bounds: Mapping[str, float],
    zoom: int,
) -> tuple[int, int, int, int]:
    def tile_y(latitude: float, scale: int) -> float:
        clamped = max(-85.0511287798066, min(85.0511287798066, latitude))
        radians = math.radians(clamped)
        return (1 - math.asinh(math.tan(radians)) / math.pi) / 2 * scale

    scale = 2**zoom
    west = max(
        0,
        min(scale - 1, math.floor((bounds["west"] + 180) / 360 * scale)),
    )
    east = max(
        0,
        min(
            scale - 1,
            math.floor((bounds["east"] + 180) / 360 * scale - 1e-12),
        ),
    )
    north = max(
        0,
        min(scale - 1, math.floor(tile_y(bounds["north"], scale))),
    )
    south = max(
        0,
        min(scale - 1, math.floor(tile_y(bounds["south"], scale) - 1e-12)),
    )
    return west, east, north, south


def _xyz_tile_descriptor(candidate: SourceCandidate) -> dict[str, Any]:
    template = candidate.endpoint_url
    tokens = _ANY_TEMPLATE_TOKEN_RE.findall(template)
    if (
        not tokens
        or any(_XYZ_TOKEN_RE.fullmatch(token) is None for token in tokens)
        or not any(token.casefold() == "{z}" for token in tokens)
        or not any(token.casefold() == "{x}" for token in tokens)
        or not any(token.casefold() in {"{y}", "{-y}"} for token in tokens)
    ):
        raise AcquisitionConfigurationError("XYZ template placeholders are invalid")
    if len({token.casefold() for token in tokens}) != len(tokens):
        raise AcquisitionConfigurationError("XYZ template repeats a placeholder")
    _require_same_origin(candidate.endpoint_url, _ANY_TEMPLATE_TOKEN_RE.sub("0", template))
    return {
        **_tile_common(candidate),
        "url_template": template,
        "scheme": "tms" if any(token.casefold() == "{-y}" for token in tokens) else "xyz",
        "layer": candidate.remote_name,
    }


def _wmts_tile_descriptor(
    candidate: SourceCandidate,
    probe: SourceProbe,
) -> dict[str, Any]:
    metadata = probe.metadata
    matrix_sets = metadata.get("tile_matrix_sets")
    if (
        not isinstance(matrix_sets, list)
        or not matrix_sets
        or any(not isinstance(value, str) or not value for value in matrix_sets)
    ):
        raise AcquisitionValidationError(
            "WMTS layer advertises no usable tile matrix set",
            code="wmts_matrix_set_unavailable",
        )
    configured_matrix = _config_optional_text(candidate.config, "tile_matrix_set", max_chars=500)
    if configured_matrix is not None and configured_matrix not in matrix_sets:
        raise AcquisitionValidationError(
            "configured WMTS matrix set is not advertised",
            code="wmts_matrix_set_unavailable",
        )
    common = _tile_common(candidate)
    definitions = metadata.get("tile_matrix_set_definitions")
    if not isinstance(definitions, list):
        raise AcquisitionValidationError(
            "WMTS matrix-set definitions are malformed",
            code="wmts_not_materializable",
        )
    selected_definition = _select_web_mercator_matrix_set(
        definitions,
        configured_identifier=configured_matrix,
        min_zoom=common["min_zoom"],
        max_zoom=common["max_zoom"],
    )
    selected_identifier = cast(str, selected_definition["identifier"])
    selected_limits = _validated_wmts_limits(
        metadata.get("tile_matrix_set_limits", {}).get(selected_identifier, [])
        if isinstance(metadata.get("tile_matrix_set_limits", {}), dict)
        else [],
        selected_definition,
    )
    exact_tile_count = _estimate_wmts_tile_count(
        common["bounds"],
        min_zoom=common["min_zoom"],
        max_zoom=common["max_zoom"],
        matrix_set=selected_definition,
        limits=selected_limits,
    )
    if exact_tile_count <= 0:
        raise AcquisitionValidationError(
            "WMTS limits do not intersect the requested coverage",
            code="wmts_not_materializable",
        )
    if exact_tile_count > common["max_tile_count"]:
        raise AcquisitionLimitError(
            "tile coverage exceeds the reviewed seed limit",
            code="tile_count_limit",
        )
    common["estimated_tile_count"] = exact_tile_count
    styles = metadata.get("styles", [])
    style_names = [item.get("name") for item in styles if isinstance(item, dict)]
    configured_style = _config_optional_text(
        candidate.config,
        "style_name",
        max_chars=500,
        allow_empty=True,
    )
    if configured_style is not None and configured_style not in {"", *style_names}:
        raise AcquisitionValidationError(
            "configured WMTS style is not advertised",
            code="wmts_style_unavailable",
        )
    style = configured_style or next(
        (
            cast(str, item["name"])
            for item in styles
            if isinstance(item, dict) and item.get("default") is True
        ),
        style_names[0] if len(style_names) == 1 else "",
    )
    formats = metadata.get("formats", [])
    if formats and common["format"] not in formats:
        raise AcquisitionValidationError(
            "configured WMTS image format is not advertised",
            code="wmts_format_unavailable",
        )
    resources: list[dict[str, Any]] = []
    for item in metadata.get("resource_urls", []):
        if not isinstance(item, dict) or item.get("resource_type", "").casefold() != "tile":
            continue
        template = item.get("template")
        if not isinstance(template, str):
            raise AcquisitionValidationError("WMTS resource template is malformed")
        template = urljoin(candidate.endpoint_url, template)
        tokens = _ANY_TEMPLATE_TOKEN_RE.findall(template)
        allowed_tokens = {
            "{tilematrixset}",
            "{tilematrix}",
            "{tilerow}",
            "{tilecol}",
            "{style}",
            "{layer}",
        }
        if (
            any(token.casefold() not in allowed_tokens for token in tokens)
            or not {"{tilematrix}", "{tilerow}", "{tilecol}"}.issubset(
                {token.casefold() for token in tokens}
            )
            or "{" in _ANY_TEMPLATE_TOKEN_RE.sub("", template)
            or "}" in _ANY_TEMPLATE_TOKEN_RE.sub("", template)
        ):
            raise AcquisitionValidationError(
                "WMTS resource template placeholders are malformed"
            )
        rendered = _ANY_TEMPLATE_TOKEN_RE.sub("0", template)
        _require_same_origin(candidate.endpoint_url, rendered)
        resource_format = item.get("format")
        if resource_format is None or resource_format == common["format"]:
            resources.append(
                {
                    **item,
                    "resource_type": "tile",
                    "template": template,
                }
            )
    return {
        **common,
        "layer": probe.canonical_name,
        "style": style,
        "tile_matrix_sets": matrix_sets,
        "selected_tile_matrix_set": selected_identifier,
        "tile_matrix_set": selected_definition,
        "tile_matrix_limits": selected_limits,
        "resource_urls": resources,
        "kvp": {
            "endpoint_url": candidate.endpoint_url,
            "service": "WMTS",
            "request": "GetTile",
            "version": probe.service_version or "1.0.0",
            "layer": probe.canonical_name,
            "style": style,
            "format": common["format"],
            "tile_matrix_set": selected_identifier,
            "tile_matrix_placeholder": "{TileMatrix}",
            "tile_row_placeholder": "{TileRow}",
            "tile_col_placeholder": "{TileCol}",
        },
    }


def _wms_tile_descriptor(
    candidate: SourceCandidate,
    probe: SourceProbe,
) -> dict[str, Any]:
    common = _tile_common(candidate)
    version = probe.service_version or "1.3.0"
    advertised_crs = probe.metadata.get("crs")
    if (
        not isinstance(advertised_crs, list)
        or not any(
            isinstance(value, str) and _is_web_mercator_crs(value)
            for value in advertised_crs
        )
    ):
        raise AcquisitionValidationError(
            "WMS layer does not advertise WebMercator rendering",
            code="wms_not_materializable",
        )
    advertised_formats = probe.metadata.get("formats")
    if (
        not isinstance(advertised_formats, list)
        or common["format"] not in advertised_formats
    ):
        raise AcquisitionValidationError(
            "WMS layer does not advertise the configured image format",
            code="wms_not_materializable",
        )
    style = (
        _config_optional_text(
            candidate.config,
            "style_name",
            max_chars=500,
            allow_empty=True,
        )
        or ""
    )
    advertised_styles = probe.metadata.get("styles", [])
    if style and (
        not isinstance(advertised_styles, list)
        or style not in advertised_styles
    ):
        raise AcquisitionValidationError(
            "WMS layer does not advertise the configured style",
            code="wms_not_materializable",
        )
    crs_parameter = "CRS" if version.startswith("1.3") else "SRS"
    return {
        **common,
        "layer": probe.canonical_name,
        "style": style,
        "crs": "EPSG:3857",
        "kvp": {
            "endpoint_url": candidate.endpoint_url,
            "service": "WMS",
            "request": "GetMap",
            "version": version,
            "layers": probe.canonical_name,
            "styles": style,
            "format": common["format"],
            "transparent": (
                "FALSE"
                if common["format"] in {"image/jpeg", "image/jpg"}
                else "TRUE"
            ),
            crs_parameter: "EPSG:3857",
            "bbox_placeholder": "{bbox}",
            "width_placeholder": "{width}",
            "height_placeholder": "{height}",
        },
    }


def _select_web_mercator_matrix_set(
    definitions: list[Any],
    *,
    configured_identifier: str | None,
    min_zoom: int,
    max_zoom: int,
) -> dict[str, Any]:
    compatible: list[dict[str, Any]] = []
    for raw in definitions:
        if not isinstance(raw, dict):
            raise AcquisitionValidationError(
                "WMTS matrix-set definition is malformed",
                code="wmts_not_materializable",
            )
        normalized = _web_mercator_matrix_set(raw)
        if normalized is not None:
            compatible.append(normalized)
    if configured_identifier is not None:
        compatible = [
            item for item in compatible if item["identifier"] == configured_identifier
        ]
    required_zooms = set(range(min_zoom, max_zoom + 1))
    compatible = [
        item
        for item in compatible
        if required_zooms.issubset(
            {matrix["zoom"] for matrix in item["tile_matrices"]}
        )
    ]
    if not compatible:
        raise AcquisitionValidationError(
            "WMTS has no WebMercator tile-matrix set covering the requested zooms",
            code="wmts_not_materializable",
        )

    def rank(item: dict[str, Any]) -> tuple[int, int, int, str]:
        identifier = cast(str, item["identifier"])
        well_known = str(item.get("well_known_scale_set", "")).casefold()
        known_name = identifier.casefold() in {
            "googlemapscompatible",
            "webmercatorquad",
            "epsg:3857",
        }
        return (
            0 if well_known.rstrip("/").endswith("googlemapscompatible") else 1,
            0 if known_name else 1,
            -len(item["tile_matrices"]),
            identifier,
        )

    return min(compatible, key=rank)


def _web_mercator_matrix_set(raw: dict[str, Any]) -> dict[str, Any] | None:
    identifier = raw.get("identifier")
    crs = raw.get("supported_crs")
    matrices = raw.get("tile_matrices")
    if (
        not isinstance(identifier, str)
        or not identifier
        or not isinstance(crs, str)
        or not _is_web_mercator_crs(crs)
        or not isinstance(matrices, list)
        or not matrices
    ):
        return None
    normalized_matrices: list[dict[str, Any]] = []
    seen_zooms: set[int] = set()
    for matrix in matrices:
        if not isinstance(matrix, dict):
            return None
        width = matrix.get("matrix_width")
        height = matrix.get("matrix_height")
        tile_width = matrix.get("tile_width")
        tile_height = matrix.get("tile_height")
        scale = matrix.get("scale_denominator")
        top_left = matrix.get("top_left_corner")
        matrix_identifier = matrix.get("identifier")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or width & (width - 1)
            or height != width
            or tile_width != 256
            or tile_height != 256
            or isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or not isinstance(top_left, list)
            or len(top_left) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in top_left
            )
            or not isinstance(matrix_identifier, str)
            or not matrix_identifier
        ):
            return None
        zoom = width.bit_length() - 1
        expected_scale = 559_082_264.0287178 / (2**zoom)
        if (
            abs(float(top_left[0]) + 20_037_508.342789244) > 1.0
            or abs(float(top_left[1]) - 20_037_508.342789244) > 1.0
            or abs(float(scale) - expected_scale) / expected_scale > 0.0001
            or zoom in seen_zooms
        ):
            return None
        seen_zooms.add(zoom)
        normalized_matrices.append({**matrix, "zoom": zoom})
    normalized_matrices.sort(key=lambda item: item["zoom"])
    return {**raw, "tile_matrices": normalized_matrices}


def _is_web_mercator_crs(value: str) -> bool:
    normalized = value.strip().casefold().rstrip("/")
    return bool(
        re.search(r"(?:[:/]|::)(?:3857|900913)$", normalized)
        or normalized in {"epsg:3857", "epsg:900913"}
    )


def _validated_wmts_limits(
    raw_limits: Any,
    definition: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw_limits, list):
        raise AcquisitionValidationError(
            "WMTS tile-matrix limits are malformed",
            code="wmts_not_materializable",
        )
    matrices = {
        item["identifier"]: item
        for item in definition["tile_matrices"]
        if isinstance(item, dict) and isinstance(item.get("identifier"), str)
    }
    result: list[dict[str, Any]] = []
    for item in raw_limits:
        if not isinstance(item, dict) or item.get("tile_matrix") not in matrices:
            raise AcquisitionValidationError(
                "WMTS limits reference an unknown tile matrix",
                code="wmts_not_materializable",
            )
        matrix = matrices[item["tile_matrix"]]
        values = (
            item.get("min_tile_row"),
            item.get("max_tile_row"),
            item.get("min_tile_col"),
            item.get("max_tile_col"),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise AcquisitionValidationError(
                "WMTS tile-matrix limits are malformed",
                code="wmts_not_materializable",
            )
        if (
            values[0] < 0
            or values[0] > values[1]
            or values[1] >= matrix["matrix_height"]
            or values[2] < 0
            or values[2] > values[3]
            or values[3] >= matrix["matrix_width"]
        ):
            raise AcquisitionValidationError(
                "WMTS tile-matrix limits exceed their matrix",
                code="wmts_not_materializable",
            )
        result.append(dict(item))
    return result


def _estimate_wmts_tile_count(
    bounds: Mapping[str, float],
    *,
    min_zoom: int,
    max_zoom: int,
    matrix_set: Mapping[str, Any],
    limits: list[dict[str, Any]],
) -> int:
    matrices = {
        item["zoom"]: item
        for item in matrix_set["tile_matrices"]
        if isinstance(item, dict)
        and isinstance(item.get("zoom"), int)
        and isinstance(item.get("identifier"), str)
    }
    limits_by_identifier = {item["tile_matrix"]: item for item in limits}
    total = 0
    for zoom in range(min_zoom, max_zoom + 1):
        matrix = matrices.get(zoom)
        if matrix is None:
            raise AcquisitionValidationError(
                "WMTS matrix coverage has a zoom gap",
                code="wmts_not_materializable",
            )
        min_x, max_x, min_y, max_y = _web_mercator_tile_window(bounds, zoom)
        limit = limits_by_identifier.get(matrix["identifier"])
        if limit is not None:
            min_x = max(min_x, limit["min_tile_col"])
            max_x = min(max_x, limit["max_tile_col"])
            min_y = max(min_y, limit["min_tile_row"])
            max_y = min(max_y, limit["max_tile_row"])
        if min_x <= max_x and min_y <= max_y:
            total += (max_x - min_x + 1) * (max_y - min_y + 1)
        if total > _ABSOLUTE_MAX_TILE_COUNT:
            return total
    return total


def _probe_manifest(probe: SourceProbe | None) -> dict[str, Any] | None:
    if probe is None:
        return None
    return {
        "protocol": probe.protocol,
        "requested_name": probe.requested_name,
        "canonical_name": probe.canonical_name,
        "service_version": probe.service_version,
        "fingerprint_sha256": probe.fingerprint_sha256,
        "fingerprint_quality": probe.fingerprint_quality,
        "metadata": probe.metadata,
    }


def _artifact_manifest(index: int, artifact: AcquiredArtifact) -> dict[str, Any]:
    return {
        "index": index,
        "artifact_kind": artifact.artifact_kind,
        "role": artifact.role,
        "media_type": artifact.media_type,
        "sha256": artifact.blob.sha256,
        "size_bytes": artifact.blob.size_bytes,
        "storage_backend": artifact.blob.storage_backend,
        "storage_key": artifact.blob.storage_key,
        "source_url": artifact.source_url,
        "final_url": artifact.final_url,
        "source_version": artifact.source_version,
        "metadata": artifact.metadata,
    }


def _style_digests(artifacts: list[AcquiredArtifact]) -> dict[str, str]:
    result: dict[str, str] = {}
    for artifact in artifacts:
        if artifact.artifact_kind != "style" or artifact.role != "style":
            continue
        source_key = artifact.metadata.get("catalog_style_source_key")
        if not isinstance(source_key, str) or source_key in result:
            raise AcquisitionValidationError(
                "acquired style identities are invalid",
                code="invalid_style_artifacts",
            )
        result[source_key] = artifact.blob.sha256
    return dict(sorted(result.items()))


def _enforce_total_bytes(
    artifacts: list[AcquiredArtifact],
    maximum: int,
) -> None:
    if sum(item.blob.size_bytes for item in artifacts) > maximum:
        raise AcquisitionLimitError(
            "source snapshot exceeds the aggregate byte limit",
            code="snapshot_too_large",
        )


def _bounded_optional_text(
    value: str | None,
    label: str,
    maximum: int,
) -> None:
    if value is not None and (
        not isinstance(value, str) or len(value) > maximum
    ):
        raise AcquisitionValidationError(
            f"{label} exceeds its persistence limit",
            code="metadata_too_large",
        )


def _bounded_json_object(
    value: dict[str, Any],
    label: str,
    maximum: int,
) -> None:
    if not isinstance(value, dict):
        raise AcquisitionValidationError(
            f"{label} must be an object",
            code="invalid_manifest",
        )
    try:
        # PostgreSQL JSONB's text representation includes separators with a
        # following space, so use the same conservative form as the database
        # octet-length constraint rather than the compact manifest encoding.
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AcquisitionValidationError(
            f"{label} contains a non-JSON value",
            code="invalid_manifest",
        ) from exc
    if len(encoded) > maximum:
        raise AcquisitionValidationError(
            f"{label} exceeds its persistence limit",
            code="metadata_too_large",
        )


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AcquisitionValidationError(
            "acquisition manifest contains a non-JSON value",
            code="invalid_manifest",
        ) from exc
