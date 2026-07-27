"""Review-gated, range-only evidence capture for very large ZIP datasets.

The probe reads an exact HEAD response, the end-of-central-directory records
and the central directory.  It never reads file payloads, writes to the mirror
CAS, creates a sync run or promotes a delivery.  Its output is evidence for a
later committed source classification, not an acquisition candidate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tempfile
import unicodedata
from typing import Any, Protocol, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.idecyl_exact_evidence import (
    ReviewedIDECyLExactSource,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationError,
    require_current_source_metadata_probe_authorization,
)
from app.reference_layers.models import (
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
)
from app.reference_layers.safe_download import (
    HTTPSDownloadPolicy,
    HTTPSHeadResult,
    HTTPSRangeResult,
    SafeDownloadError,
    SafeHTTPSDownloader,
    normalize_https_url,
)


PROBE_PLAN_SCHEMA = "siur-zip-metadata-probe-plan/v1"
PROBE_MANIFEST_SCHEMA = "siur-zip-metadata-probe-manifest/v1"
PROBE_ENVELOPE_SCHEMA = "siur-hash-bound-evidence/v1"
PROBE_SOURCE_CONFIG_SCHEMA = "siur-metadata-probe-only-source/v1"
_ZIP_MEDIA_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/x-zip-compressed",
        "application/zip",
    }
)
_STRONG_ETAG_RE = re.compile(r'^"(?:[\x21\x23-\x7e\x80-\xff]*)"$')
_RESOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$", re.ASCII)
_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_ZIP64_EXTRA_ID = 0x0001
_EOCD_BYTES = 22
_ZIP64_LOCATOR_BYTES = 20
_ZIP64_EOCD_FIXED_BYTES = 56
_CENTRAL_FIXED_BYTES = 46
_MAX_ZIP_COMMENT_BYTES = 65_535
_ZIP32_U16_MAX = 0xFFFF
_ZIP32_U32_MAX = 0xFFFFFFFF
_EXPECTED_SIGPAC_ARCHIVES = (
    "AVILA.zip",
    "BURGOS.zip",
    "LEON.zip",
    "PALENCIA.zip",
    "SALAMANCA.zip",
    "SEGOVIA.zip",
    "SORIA.zip",
    "VALLADOLID.zip",
    "ZAMORA.zip",
)


class ZipMetadataProbeError(RuntimeError):
    """Fail-closed metadata capture failure with one stable reason code."""

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


@dataclass(frozen=True)
class ZipMetadataProbeLimits:
    max_object_bytes: int = 4 * 1024 * 1024 * 1024
    max_tail_bytes: int = 128 * 1024
    max_central_directory_bytes: int = 8 * 1024 * 1024
    max_zip64_eocd_bytes: int = 1024
    max_members_per_archive: int = 100_000
    max_member_uncompressed_bytes: int = 64 * 1024 * 1024 * 1024
    max_archive_uncompressed_bytes: int = 96 * 1024 * 1024 * 1024
    max_total_uncompressed_bytes: int = 192 * 1024 * 1024 * 1024
    max_compression_ratio: float = 2_000.0

    def __post_init__(self) -> None:
        for name in (
            "max_object_bytes",
            "max_tail_bytes",
            "max_central_directory_bytes",
            "max_zip64_eocd_bytes",
            "max_members_per_archive",
            "max_member_uncompressed_bytes",
            "max_archive_uncompressed_bytes",
            "max_total_uncompressed_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_tail_bytes < _EOCD_BYTES + _MAX_ZIP_COMMENT_BYTES:
            raise ValueError("max_tail_bytes cannot fit one complete ZIP tail")
        if self.max_zip64_eocd_bytes < _ZIP64_EOCD_FIXED_BYTES:
            raise ValueError("max_zip64_eocd_bytes cannot fit ZIP64 EOCD")
        if (
            isinstance(self.max_compression_ratio, bool)
            or not isinstance(self.max_compression_ratio, (int, float))
            or not math.isfinite(self.max_compression_ratio)
            or self.max_compression_ratio < 1
        ):
            raise ValueError("max_compression_ratio must be finite and positive")


@dataclass(frozen=True)
class ZipProbeResource:
    resource_key: str
    url: str

    def __post_init__(self) -> None:
        if _RESOURCE_KEY_RE.fullmatch(self.resource_key) is None:
            raise ValueError("ZIP metadata resource key is invalid")
        object.__setattr__(self, "url", normalize_https_url(self.url))


@dataclass(frozen=True)
class ZipMetadataProbePlan:
    profile: str
    audit_layer_id: int
    source_evidence_sha256: str
    root_index_sha256: str
    province_index_sha256: str
    province_directory_url: str
    resources: tuple[ZipProbeResource, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile, str)
            or not self.profile
            or isinstance(self.audit_layer_id, bool)
            or not isinstance(self.audit_layer_id, int)
            or self.audit_layer_id <= 0
        ):
            raise ValueError("ZIP metadata probe identity is invalid")
        for name in (
            "source_evidence_sha256",
            "root_index_sha256",
            "province_index_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII) is None
            ):
                raise ValueError(f"{name} is invalid")
        directory = normalize_https_url(self.province_directory_url)
        if not urlsplit(directory).path.endswith("/"):
            raise ValueError("province directory URL must end with slash")
        object.__setattr__(self, "province_directory_url", directory)
        if (
            len(self.resources) != 9
            or len({item.resource_key for item in self.resources}) != 9
            or len({item.url for item in self.resources}) != 9
            or tuple(item.resource_key for item in self.resources)
            != tuple(
                item.resource_key
                for item in sorted(
                    self.resources,
                    key=lambda item: item.resource_key,
                )
            )
        ):
            raise ValueError("ZIP metadata resources must be nine canonical items")
        origin = _origin(directory)
        if any(
            _origin(item.url) != origin or not item.url.startswith(directory)
            for item in self.resources
        ):
            raise ValueError("ZIP metadata resources must stay in one directory")

    def semantic_document(self) -> dict[str, Any]:
        return {
            "schema": PROBE_PLAN_SCHEMA,
            "profile": self.profile,
            "audit_layer_id": self.audit_layer_id,
            "source_evidence_sha256": self.source_evidence_sha256,
            "root_index_sha256": self.root_index_sha256,
            "province_index_sha256": self.province_index_sha256,
            "province_directory_url": self.province_directory_url,
            "resources": [
                {
                    "resource_key": item.resource_key,
                    "url": item.url,
                }
                for item in self.resources
            ],
        }

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.semantic_document())


@dataclass(frozen=True)
class HashBoundProbeManifest:
    manifest: dict[str, Any]
    manifest_sha256: str

    def __post_init__(self) -> None:
        if canonical_json_sha256(self.manifest) != self.manifest_sha256:
            raise ValueError("probe manifest hash does not match its content")

    def envelope(self) -> dict[str, Any]:
        return {
            "schema": PROBE_ENVELOPE_SCHEMA,
            "manifest_sha256": self.manifest_sha256,
            "manifest": self.manifest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.envelope())


class ZipMetadataHTTPClient(Protocol):
    def head(self, url: str, *, accept: str | None = None) -> HTTPSHeadResult: ...

    def download_range(
        self,
        url: str,
        sink,
        *,
        start: int,
        end: int,
        if_match: str,
        accept: str | None = None,
    ) -> HTTPSRangeResult: ...


class _IndexLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        values = [value for name, value in attrs if name.casefold() == "href"]
        if len(values) == 1 and values[0] is not None:
            self.links.append(values[0])


def build_sigpac_zip_metadata_probe_plan(
    reviewed: ReviewedIDECyLExactSource,
    *,
    root_index: bytes,
    province_index: bytes,
) -> ZipMetadataProbePlan:
    """Bind exact local directory indexes to one still-restricted SIGPAC plan."""

    if reviewed.local_service_status != "restricted":
        raise ZipMetadataProbeError(
            "SIGPAC evidence is no longer restricted",
            code="classification_changed",
        )
    distribution = reviewed.evidence.get("official_https_distribution")
    binding = reviewed.evidence.get("evidence_binding")
    if not isinstance(distribution, dict) or not isinstance(binding, dict):
        raise ZipMetadataProbeError(
            "SIGPAC distribution evidence is missing",
            code="evidence_invalid",
        )
    root_sha256 = hashlib.sha256(root_index).hexdigest()
    province_sha256 = hashlib.sha256(province_index).hexdigest()
    if root_sha256 != distribution.get(
        "root_index_sha256"
    ) or province_sha256 != distribution.get("province_index_sha256"):
        raise ZipMetadataProbeError(
            "SIGPAC directory index changed after review",
            code="directory_index_changed",
        )
    archive_names = distribution.get("archive_names")
    if archive_names != list(_EXPECTED_SIGPAC_ARCHIVES):
        raise ZipMetadataProbeError(
            "SIGPAC province archive inventory changed",
            code="resource_inventory_changed",
        )
    root_links = _html_links(root_index)
    province_links = _html_links(province_index)
    if (
        root_links.count("Parcelario_SIGPAC_CyL_Provincias/") != 1
        or tuple(
            sorted(
                link
                for link in province_links
                if re.fullmatch(r"[A-Z]+\.zip", link, re.ASCII)
            )
        )
        != _EXPECTED_SIGPAC_ARCHIVES
        or any(
            link.endswith(".zip") and link not in _EXPECTED_SIGPAC_ARCHIVES
            for link in province_links
        )
    ):
        raise ZipMetadataProbeError(
            "SIGPAC HTML inventory does not contain the exact nine resources",
            code="resource_inventory_changed",
        )
    directory = normalize_https_url(distribution["province_directory_url"])
    source_evidence_sha256 = canonical_json_sha256(reviewed.evidence)
    classification_sha256 = binding.get("classification_manifest_sha256")
    if not isinstance(classification_sha256, str):
        raise ZipMetadataProbeError(
            "SIGPAC classification binding is missing",
            code="evidence_invalid",
        )
    return ZipMetadataProbePlan(
        profile=f"{distribution['profile']}-metadata-probe-v1",
        audit_layer_id=reviewed.audit_layer_id,
        source_evidence_sha256=source_evidence_sha256,
        root_index_sha256=root_sha256,
        province_index_sha256=province_sha256,
        province_directory_url=directory,
        resources=tuple(
            ZipProbeResource(
                resource_key=name[:-4].casefold(),
                url=_append_path_segment(directory, name),
            )
            for name in _EXPECTED_SIGPAC_ARCHIVES
        ),
    )


def metadata_probe_source_definition(
    plan: ZipMetadataProbePlan,
) -> dict[str, Any]:
    """Return the exact disabled/manual source definition used by the gate."""

    return {
        "protocol": "download",
        "target_kind": "vector",
        "endpoint_url": plan.province_directory_url,
        "remote_name": f"sigpac-{plan.audit_layer_id}-metadata-probe",
        "sync_strategy": "manual",
        "priority": 127,
        "config": {
            "schema": PROBE_SOURCE_CONFIG_SCHEMA,
            "metadata_probe_only": True,
            "probe_plan": plan.semantic_document(),
            "probe_plan_sha256": plan.sha256,
        },
    }


def probe_reviewed_zip_metadata(
    db: Session,
    *,
    source: ReferenceLayerSource,
    plan: ZipMetadataProbePlan,
    captured_at: datetime | None = None,
    limits: ZipMetadataProbeLimits | None = None,
    client: ZipMetadataHTTPClient | None = None,
) -> HashBoundProbeManifest:
    """Capture a canonical ZIP inventory after an exact metadata-only review."""

    active_limits = limits or ZipMetadataProbeLimits()
    review = _require_exact_metadata_only_review(db, source=source, plan=plan)
    at = _aware_utc(captured_at or datetime.now(timezone.utc))
    actual_client = client or _safe_probe_client(plan, active_limits)
    resources: list[dict[str, Any]] = []
    total_uncompressed = 0
    for resource in plan.resources:
        observed = _probe_one_resource(
            actual_client,
            resource,
            limits=active_limits,
        )
        total_uncompressed += observed["total_uncompressed_bytes"]
        if total_uncompressed > active_limits.max_total_uncompressed_bytes:
            raise ZipMetadataProbeError(
                "ZIP set exceeds the aggregate uncompressed limit",
                code="zip_bomb",
            )
        resources.append(observed)

    current = _require_exact_metadata_only_review(db, source=source, plan=plan)
    if current.id != review.id or current.review_sha256 != review.review_sha256:
        raise ZipMetadataProbeError(
            "metadata authorization changed during the probe",
            code="authorization_changed",
        )
    manifest = {
        "schema": PROBE_MANIFEST_SCHEMA,
        "status": "evidence_only_restricted_until_versioned",
        "captured_at": _utc_isoformat(at),
        "probe_plan_sha256": plan.sha256,
        "source_definition_sha256": source.definition_sha256,
        "authorization": {
            "review_id": review.id,
            "review_sha256": review.review_sha256,
            "permissions": {
                "metadata_probe": True,
                "dataset_download": False,
                "local_storage": False,
                "local_service": False,
                "bulk_tile_seed": False,
            },
        },
        "resources": resources,
        "aggregate": {
            "resource_count": len(resources),
            "member_count": sum(item["member_count"] for item in resources),
            "compressed_bytes": sum(
                item["total_compressed_bytes"] for item in resources
            ),
            "uncompressed_bytes": total_uncompressed,
        },
    }
    return HashBoundProbeManifest(
        manifest=manifest,
        manifest_sha256=canonical_json_sha256(manifest),
    )


def write_hash_bound_probe_manifest(
    path: str | Path,
    evidence: HashBoundProbeManifest,
) -> None:
    """Create one evidence file atomically without replacing existing work."""

    target = Path(path).absolute()
    parent = target.parent
    if not parent.is_dir():
        raise ZipMetadataProbeError(
            "probe evidence directory does not exist",
            code="output_directory_missing",
        )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(evidence.canonical_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_name, target)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as error:
        raise ZipMetadataProbeError(
            "probe evidence target already exists",
            code="output_exists",
        ) from error
    except OSError as error:
        raise ZipMetadataProbeError(
            "probe evidence could not be persisted",
            code="output_failed",
        ) from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Plan locally, or execute one exact reviewed range-only capture."""

    parser = argparse.ArgumentParser(
        description=("Plan or capture review-gated ZIP central-directory evidence")
    )
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--root-index", required=True)
    parser.add_argument("--province-index", required=True)
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    arguments = parser.parse_args(argv)
    try:
        if arguments.source_id <= 0:
            raise ZipMetadataProbeError(
                "source_id must be positive",
                code="operator_input_invalid",
            )
        if arguments.apply and (
            not arguments.output or not arguments.expected_plan_sha256
        ):
            raise ZipMetadataProbeError(
                "--apply requires --output and --expected-plan-sha256",
                code="operator_confirmation_missing",
            )
        if not arguments.apply and arguments.expected_plan_sha256 is not None:
            raise ZipMetadataProbeError(
                "--expected-plan-sha256 is only valid with --apply",
                code="operator_input_invalid",
            )
        root_index = _read_local_index(arguments.root_index)
        province_index = _read_local_index(arguments.province_index)
        register_all_models()
        with SessionLocal() as db:
            source = db.get(ReferenceLayerSource, arguments.source_id)
            if source is None:
                raise ZipMetadataProbeError(
                    "metadata probe source does not exist",
                    code="probe_source_missing",
                )
            plan = _plan_for_stored_source(
                source,
                root_index=root_index,
                province_index=province_index,
            )
            review = _require_exact_metadata_only_review(
                db,
                source=source,
                plan=plan,
            )
            output = Path(arguments.output).absolute() if arguments.output else None
            summary: dict[str, Any] = {
                "ok": True,
                "mode": "apply" if arguments.apply else "dry-run",
                "network_performed": False,
                "delivery_mutation_performed": False,
                "source_id": source.id,
                "source_definition_sha256": source.definition_sha256,
                "authorization_review_id": review.id,
                "authorization_review_sha256": review.review_sha256,
                "probe_plan_sha256": plan.sha256,
                "resource_count": len(plan.resources),
                "resource_urls": [item.url for item in plan.resources],
                "output_path": str(output) if output is not None else None,
                "output_exists": output.exists() if output is not None else None,
            }
            if arguments.apply:
                if arguments.expected_plan_sha256 != plan.sha256:
                    raise ZipMetadataProbeError(
                        "probe plan changed after operator review",
                        code="probe_plan_changed",
                    )
                assert output is not None
                evidence = probe_reviewed_zip_metadata(
                    db,
                    source=source,
                    plan=plan,
                )
                write_hash_bound_probe_manifest(output, evidence)
                body = evidence.canonical_bytes()
                summary.update(
                    {
                        "network_performed": True,
                        "manifest_sha256": evidence.manifest_sha256,
                        "output_sha256": hashlib.sha256(body).hexdigest(),
                        "output_size_bytes": len(body),
                    }
                )
        print(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except ZipMetadataProbeError as error:
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
                    "error_code": "zip_metadata_probe_unavailable",
                    "error_summary": type(error).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


def _require_exact_metadata_only_review(
    db: Session,
    *,
    source: ReferenceLayerSource,
    plan: ZipMetadataProbePlan,
) -> ReferenceMirrorAuthorizationReview:
    expected = metadata_probe_source_definition(plan)
    stored = {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": source.priority,
        "config": source.config_json,
    }
    if (
        source.enabled
        or source.is_primary
        or stored != expected
        or source.definition_sha256 != canonical_json_sha256(expected)
    ):
        raise ZipMetadataProbeError(
            "metadata probe source is not the exact disabled/manual plan",
            code="probe_source_changed",
        )
    try:
        review = require_current_source_metadata_probe_authorization(
            db,
            source=source,
        )
    except MirrorAuthorizationError as error:
        raise ZipMetadataProbeError(
            "metadata probe lacks a current persisted review",
            code=error.code,
        ) from error
    if (
        review.decision != "approved"
        or not review.allow_metadata_probe
        or review.allow_dataset_download
        or review.allow_local_storage
        or review.allow_local_service
        or review.allow_bulk_tile_seed
        or tuple(review.allowed_origins_json) != (_origin(plan.province_directory_url),)
    ):
        raise ZipMetadataProbeError(
            "review must grant metadata_probe and no broader permission",
            code="metadata_only_authorization_required",
        )
    return review


def _plan_for_stored_source(
    source: ReferenceLayerSource,
    *,
    root_index: bytes,
    province_index: bytes,
) -> ZipMetadataProbePlan:
    config = source.config_json
    stored_plan = config.get("probe_plan") if isinstance(config, dict) else None
    audit_layer_id = (
        stored_plan.get("audit_layer_id") if isinstance(stored_plan, dict) else None
    )
    if isinstance(audit_layer_id, bool) or not isinstance(audit_layer_id, int):
        raise ZipMetadataProbeError(
            "stored metadata probe plan is invalid",
            code="probe_source_changed",
        )
    from app.reference_layers.idecyl_exact_evidence import (
        idecyl_exact_source_inventory,
    )

    reviewed = next(
        (
            item
            for item in idecyl_exact_source_inventory()
            if item.audit_layer_id == audit_layer_id
        ),
        None,
    )
    if reviewed is None:
        raise ZipMetadataProbeError(
            "stored SIGPAC identity is no longer reviewed",
            code="classification_changed",
        )
    plan = build_sigpac_zip_metadata_probe_plan(
        reviewed,
        root_index=root_index,
        province_index=province_index,
    )
    if (
        stored_plan != plan.semantic_document()
        or config.get("probe_plan_sha256") != plan.sha256
    ):
        raise ZipMetadataProbeError(
            "stored metadata probe plan changed",
            code="probe_source_changed",
        )
    return plan


def _read_local_index(path_value: str) -> bytes:
    path = Path(path_value).absolute()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not 1 <= opened.st_size <= 64 * 1024:
            raise ZipMetadataProbeError(
                "directory index file is invalid",
                code="operator_input_invalid",
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            body = stream.read(64 * 1024 + 1)
            final = os.fstat(stream.fileno())
        if len(body) != opened.st_size or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise ZipMetadataProbeError(
                "directory index changed while it was read",
                code="operator_input_changed",
            )
        return body
    except ZipMetadataProbeError:
        raise
    except OSError as error:
        raise ZipMetadataProbeError(
            "directory index file is unavailable",
            code="operator_input_invalid",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_probe_client(
    plan: ZipMetadataProbePlan,
    limits: ZipMetadataProbeLimits,
) -> SafeHTTPSDownloader:
    maximum_range = max(
        limits.max_tail_bytes,
        limits.max_central_directory_bytes,
        limits.max_zip64_eocd_bytes,
    )
    return SafeHTTPSDownloader(
        HTTPSDownloadPolicy(
            allowed_origins=(_origin(plan.province_directory_url),),
            max_response_bytes=maximum_range,
            timeout_seconds=300,
            idle_timeout_seconds=30,
            max_redirects=0,
            allowed_content_types=_ZIP_MEDIA_TYPES,
            user_agent="AsistenteAyuntamientos/zip-metadata-probe",
        )
    )


def _probe_one_resource(
    client: ZipMetadataHTTPClient,
    resource: ZipProbeResource,
    *,
    limits: ZipMetadataProbeLimits,
) -> dict[str, Any]:
    try:
        head = client.head(resource.url, accept="application/zip")
    except (SafeDownloadError, ValueError) as error:
        raise _transport_error(error) from error
    if (
        head.final_url != resource.url
        or head.redirects != 0
        or head.content_type not in _ZIP_MEDIA_TYPES
        or head.accept_ranges is None
        or head.accept_ranges.casefold() != "bytes"
        or head.content_length is None
        or not _EOCD_BYTES <= head.content_length <= limits.max_object_bytes
        or head.etag is None
        or head.etag.startswith("W/")
        or _STRONG_ETAG_RE.fullmatch(head.etag) is None
    ):
        raise ZipMetadataProbeError(
            "ZIP HEAD response lacks exact range and validator evidence",
            code="head_evidence_invalid",
        )
    tail_start = max(0, head.content_length - limits.max_tail_bytes)
    tail = _read_exact_range(
        client,
        resource.url,
        start=tail_start,
        end=head.content_length - 1,
        object_size=head.content_length,
        etag=head.etag,
    )
    directory = _locate_directory(
        client,
        resource.url,
        tail=tail,
        tail_start=tail_start,
        object_size=head.content_length,
        etag=head.etag,
        limits=limits,
    )
    central = _read_exact_range(
        client,
        resource.url,
        start=directory["offset"],
        end=directory["offset"] + directory["size"] - 1,
        object_size=head.content_length,
        etag=head.etag,
    )
    members = _parse_central_directory(
        central,
        expected_members=directory["entries"],
        limits=limits,
    )
    total_compressed = sum(item["compressed_bytes"] for item in members)
    total_uncompressed = sum(item["uncompressed_bytes"] for item in members)
    if total_uncompressed > limits.max_archive_uncompressed_bytes:
        raise ZipMetadataProbeError(
            "ZIP archive exceeds its uncompressed limit",
            code="zip_bomb",
        )
    return {
        "resource_key": resource.resource_key,
        "url": resource.url,
        "validators": {
            "etag": head.etag,
            "last_modified": head.last_modified,
            "content_length": head.content_length,
            "content_type": head.content_type,
        },
        "zip64": directory["zip64"],
        "central_directory": {
            "offset": directory["offset"],
            "size_bytes": directory["size"],
            "sha256": hashlib.sha256(central).hexdigest(),
        },
        "member_count": len(members),
        "total_compressed_bytes": total_compressed,
        "total_uncompressed_bytes": total_uncompressed,
        "members": members,
    }


def _locate_directory(
    client: ZipMetadataHTTPClient,
    url: str,
    *,
    tail: bytes,
    tail_start: int,
    object_size: int,
    etag: str,
    limits: ZipMetadataProbeLimits,
) -> dict[str, int | bool]:
    eocd_index, eocd_fields = _find_eocd(tail)
    (
        signature,
        disk_number,
        directory_disk,
        disk_entries,
        total_entries,
        directory_size,
        directory_offset,
        comment_size,
    ) = eocd_fields
    if (
        signature != _EOCD_SIGNATURE
        or eocd_index + _EOCD_BYTES + comment_size != len(tail)
        or disk_number not in {0, _ZIP32_U16_MAX}
        or directory_disk not in {0, _ZIP32_U16_MAX}
    ):
        raise ZipMetadataProbeError(
            "ZIP end-of-central-directory is inconsistent",
            code="zip_truncated",
        )
    eocd_offset = tail_start + eocd_index
    zip64 = (
        disk_entries == _ZIP32_U16_MAX
        or total_entries == _ZIP32_U16_MAX
        or directory_size == _ZIP32_U32_MAX
        or directory_offset == _ZIP32_U32_MAX
    )
    if not zip64:
        if (
            disk_number != 0
            or directory_disk != 0
            or disk_entries != total_entries
            or directory_offset + directory_size != eocd_offset
        ):
            raise ZipMetadataProbeError(
                "multi-disk or gapped ZIP archives are unsupported",
                code="zip_layout_invalid",
            )
        return _validated_directory(
            offset=directory_offset,
            size=directory_size,
            entries=total_entries,
            zip64=False,
            object_size=object_size,
            limits=limits,
        )

    locator_offset = eocd_offset - _ZIP64_LOCATOR_BYTES
    locator = _slice_or_read(
        client,
        url,
        tail=tail,
        tail_start=tail_start,
        start=locator_offset,
        size=_ZIP64_LOCATOR_BYTES,
        object_size=object_size,
        etag=etag,
    )
    signature, eocd_disk, zip64_offset, disk_count = struct.unpack(
        "<4sLQL",
        locator,
    )
    if (
        signature != _ZIP64_LOCATOR_SIGNATURE
        or eocd_disk != 0
        or disk_count != 1
        or zip64_offset >= locator_offset
    ):
        raise ZipMetadataProbeError(
            "ZIP64 locator is invalid",
            code="zip64_invalid",
        )
    fixed = _slice_or_read(
        client,
        url,
        tail=tail,
        tail_start=tail_start,
        start=zip64_offset,
        size=_ZIP64_EOCD_FIXED_BYTES,
        object_size=object_size,
        etag=etag,
    )
    (
        signature,
        record_size,
        _version_made,
        _version_needed,
        disk_number,
        directory_disk,
        disk_entries,
        total_entries,
        directory_size,
        directory_offset,
    ) = struct.unpack("<4sQ2H2L4Q", fixed)
    full_record_size = record_size + 12
    if (
        signature != _ZIP64_EOCD_SIGNATURE
        or record_size < 44
        or full_record_size > limits.max_zip64_eocd_bytes
        or disk_number != 0
        or directory_disk != 0
        or disk_entries != total_entries
        or zip64_offset + full_record_size != locator_offset
        or directory_offset + directory_size != zip64_offset
    ):
        raise ZipMetadataProbeError(
            "ZIP64 end-of-central-directory is invalid",
            code="zip64_invalid",
        )
    if full_record_size > len(fixed):
        _slice_or_read(
            client,
            url,
            tail=tail,
            tail_start=tail_start,
            start=zip64_offset,
            size=full_record_size,
            object_size=object_size,
            etag=etag,
        )
    return _validated_directory(
        offset=directory_offset,
        size=directory_size,
        entries=total_entries,
        zip64=True,
        object_size=object_size,
        limits=limits,
    )


def _find_eocd(tail: bytes) -> tuple[int, tuple[Any, ...]]:
    search_end = len(tail)
    while search_end > 0:
        index = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if index < 0:
            break
        if len(tail) - index >= _EOCD_BYTES:
            fields = struct.unpack_from("<4s4H2LH", tail, index)
            if index + _EOCD_BYTES + fields[-1] == len(tail):
                return index, fields
        search_end = index
    raise ZipMetadataProbeError(
        "ZIP end-of-central-directory is missing or truncated",
        code="zip_truncated",
    )


def _validated_directory(
    *,
    offset: int,
    size: int,
    entries: int,
    zip64: bool,
    object_size: int,
    limits: ZipMetadataProbeLimits,
) -> dict[str, int | bool]:
    if (
        entries <= 0
        or entries > limits.max_members_per_archive
        or size <= 0
        or size > limits.max_central_directory_bytes
        or offset < 0
        or offset + size > object_size
    ):
        raise ZipMetadataProbeError(
            "ZIP central directory exceeds its limits",
            code="zip_directory_limit",
        )
    return {
        "offset": offset,
        "size": size,
        "entries": entries,
        "zip64": zip64,
    }


def _parse_central_directory(
    value: bytes,
    *,
    expected_members: int,
    limits: ZipMetadataProbeLimits,
) -> list[dict[str, Any]]:
    offset = 0
    result: list[dict[str, Any]] = []
    canonical_names: set[str] = set()
    total_uncompressed = 0
    while offset < len(value):
        if (
            len(result) >= limits.max_members_per_archive
            or len(value) - offset < _CENTRAL_FIXED_BYTES
        ):
            raise ZipMetadataProbeError(
                "ZIP central directory is truncated",
                code="zip_truncated",
            )
        fields = struct.unpack_from("<4s6H3L5H2L", value, offset)
        if fields[0] != _CENTRAL_SIGNATURE:
            raise ZipMetadataProbeError(
                "ZIP central directory contains an unexpected record",
                code="zip_layout_invalid",
            )
        (
            _signature,
            _version_made,
            _version_needed,
            flags,
            compression_method,
            _modified_time,
            _modified_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_size,
            extra_size,
            comment_size,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_offset,
        ) = fields
        record_end = (
            offset + _CENTRAL_FIXED_BYTES + name_size + extra_size + comment_size
        )
        if name_size == 0 or record_end > len(value):
            raise ZipMetadataProbeError(
                "ZIP central directory member is truncated",
                code="zip_truncated",
            )
        name_bytes = value[
            offset + _CENTRAL_FIXED_BYTES : offset + _CENTRAL_FIXED_BYTES + name_size
        ]
        extra = value[
            offset
            + _CENTRAL_FIXED_BYTES
            + name_size : offset
            + _CENTRAL_FIXED_BYTES
            + name_size
            + extra_size
        ]
        name = _decode_member_name(name_bytes, flags)
        canonical_name = _canonical_member_name(name)
        if canonical_name in canonical_names:
            raise ZipMetadataProbeError(
                "ZIP central directory contains duplicate members",
                code="zip_duplicate_member",
            )
        canonical_names.add(canonical_name)
        (
            uncompressed_size,
            compressed_size,
            local_offset,
            disk_start,
        ) = _zip64_member_values(
            extra,
            uncompressed_size=uncompressed_size,
            compressed_size=compressed_size,
            local_offset=local_offset,
            disk_start=disk_start,
        )
        if disk_start != 0:
            raise ZipMetadataProbeError(
                "multi-disk ZIP members are unsupported",
                code="zip_layout_invalid",
            )
        if flags & 0x0001:
            raise ZipMetadataProbeError(
                "encrypted ZIP members are unsupported",
                code="zip_encrypted",
            )
        if (
            uncompressed_size > limits.max_member_uncompressed_bytes
            or (compressed_size == 0 and uncompressed_size > 0)
            or (
                compressed_size > 0
                and uncompressed_size / compressed_size > limits.max_compression_ratio
            )
        ):
            raise ZipMetadataProbeError(
                "ZIP member exceeds decompression limits",
                code="zip_bomb",
            )
        total_uncompressed += uncompressed_size
        if total_uncompressed > limits.max_archive_uncompressed_bytes:
            raise ZipMetadataProbeError(
                "ZIP archive exceeds decompression limits",
                code="zip_bomb",
            )
        result.append(
            {
                "name": name,
                "crc32": f"{crc32:08x}",
                "compressed_bytes": compressed_size,
                "uncompressed_bytes": uncompressed_size,
                "compression_method": compression_method,
                "general_purpose_flags": flags,
                "local_header_offset": local_offset,
            }
        )
        offset = record_end
    if len(result) != expected_members:
        raise ZipMetadataProbeError(
            "ZIP central directory member count changed",
            code="zip_member_count_mismatch",
        )
    result.sort(key=lambda item: item["name"].encode("utf-8"))
    return result


def _zip64_member_values(
    extra: bytes,
    *,
    uncompressed_size: int,
    compressed_size: int,
    local_offset: int,
    disk_start: int,
) -> tuple[int, int, int, int]:
    required = (
        uncompressed_size == _ZIP32_U32_MAX,
        compressed_size == _ZIP32_U32_MAX,
        local_offset == _ZIP32_U32_MAX,
        disk_start == _ZIP32_U16_MAX,
    )
    if not any(required):
        return uncompressed_size, compressed_size, local_offset, disk_start
    cursor = 0
    zip64: bytes | None = None
    while cursor < len(extra):
        if len(extra) - cursor < 4:
            raise ZipMetadataProbeError(
                "ZIP extra field is truncated",
                code="zip64_invalid",
            )
        field_id, field_size = struct.unpack_from("<HH", extra, cursor)
        cursor += 4
        field_end = cursor + field_size
        if field_end > len(extra):
            raise ZipMetadataProbeError(
                "ZIP extra field is truncated",
                code="zip64_invalid",
            )
        if field_id == _ZIP64_EXTRA_ID:
            if zip64 is not None:
                raise ZipMetadataProbeError(
                    "ZIP member has duplicate ZIP64 fields",
                    code="zip64_invalid",
                )
            zip64 = extra[cursor:field_end]
        cursor = field_end
    if zip64 is None:
        raise ZipMetadataProbeError(
            "ZIP64 member values are missing",
            code="zip64_invalid",
        )
    cursor = 0
    values = [uncompressed_size, compressed_size, local_offset, disk_start]
    widths = [8, 8, 8, 4]
    for index, needed in enumerate(required):
        if not needed:
            continue
        width = widths[index]
        if len(zip64) - cursor < width:
            raise ZipMetadataProbeError(
                "ZIP64 member values are truncated",
                code="zip64_invalid",
            )
        values[index] = int.from_bytes(zip64[cursor : cursor + width], "little")
        cursor += width
    if cursor != len(zip64):
        raise ZipMetadataProbeError(
            "ZIP64 member contains ambiguous values",
            code="zip64_invalid",
        )
    return values[0], values[1], values[2], values[3]


def _read_exact_range(
    client: ZipMetadataHTTPClient,
    url: str,
    *,
    start: int,
    end: int,
    object_size: int,
    etag: str,
) -> bytes:
    if start < 0 or end < start or end >= object_size:
        raise ZipMetadataProbeError(
            "ZIP metadata range is invalid",
            code="range_invalid",
        )
    sink = BytesIO()
    try:
        result = client.download_range(
            url,
            sink,
            start=start,
            end=end,
            if_match=etag,
            accept="application/zip",
        )
    except (SafeDownloadError, ValueError) as error:
        raise _transport_error(error) from error
    body = sink.getvalue()
    if (
        result.final_url != url
        or result.redirects != 0
        or result.object_size != object_size
        or result.range_start != start
        or result.range_end != end
        or result.etag != etag
        or result.size_bytes != len(body)
        or len(body) != end - start + 1
        or result.sha256 != hashlib.sha256(body).hexdigest()
    ):
        raise ZipMetadataProbeError(
            "ZIP ranged response changed or was truncated",
            code="range_evidence_invalid",
            retryable=True,
        )
    return body


def _slice_or_read(
    client: ZipMetadataHTTPClient,
    url: str,
    *,
    tail: bytes,
    tail_start: int,
    start: int,
    size: int,
    object_size: int,
    etag: str,
) -> bytes:
    if start >= tail_start and start + size <= tail_start + len(tail):
        relative = start - tail_start
        return tail[relative : relative + size]
    return _read_exact_range(
        client,
        url,
        start=start,
        end=start + size - 1,
        object_size=object_size,
        etag=etag,
    )


def _decode_member_name(value: bytes, flags: int) -> str:
    try:
        return value.decode("utf-8" if flags & 0x0800 else "cp437")
    except UnicodeDecodeError as error:
        raise ZipMetadataProbeError(
            "ZIP member name is not decodable",
            code="zip_member_name_invalid",
        ) from error


def _canonical_member_name(value: str) -> str:
    if (
        not value
        or len(value.encode("utf-8")) > 4_096
        or value != unicodedata.normalize("NFC", value)
        or "\\" in value
        or "//" in value
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ZipMetadataProbeError(
            "ZIP member path is unsafe",
            code="zip_path_traversal",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ZipMetadataProbeError(
            "ZIP member path is unsafe",
            code="zip_path_traversal",
        )
    return value.rstrip("/").casefold()


def _html_links(value: bytes) -> list[str]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ZipMetadataProbeError(
            "SIGPAC directory index is not UTF-8",
            code="directory_index_invalid",
        ) from error
    parser = _IndexLinks()
    try:
        parser.feed(text)
        parser.close()
    except Exception as error:
        raise ZipMetadataProbeError(
            "SIGPAC directory index is malformed",
            code="directory_index_invalid",
        ) from error
    return parser.links


def _append_path_segment(base: str, segment: str) -> str:
    if not segment or "/" in segment or "\\" in segment or segment in {".", ".."}:
        raise ZipMetadataProbeError(
            "ZIP resource name is unsafe",
            code="resource_inventory_changed",
        )
    parts = urlsplit(normalize_https_url(base))
    path = parts.path.rstrip("/") + "/" + quote(segment, safe="-._~")
    return normalize_https_url(urlunsplit((parts.scheme, parts.netloc, path, "", "")))


def _origin(value: str) -> str:
    parts = urlsplit(normalize_https_url(value))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _transport_error(error: BaseException) -> ZipMetadataProbeError:
    code = getattr(error, "code", "metadata_probe_transport_failed")
    retryable = bool(getattr(error, "retryable", False))
    return ZipMetadataProbeError(
        "bounded ZIP metadata request failed",
        code=code,
        retryable=retryable,
    )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("captured_at is invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("captured_at must include a timezone")
    return value.astimezone(timezone.utc)


def _utc_isoformat(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "HashBoundProbeManifest",
    "PROBE_ENVELOPE_SCHEMA",
    "PROBE_MANIFEST_SCHEMA",
    "PROBE_PLAN_SCHEMA",
    "ZipMetadataProbeError",
    "ZipMetadataProbeLimits",
    "ZipMetadataProbePlan",
    "ZipProbeResource",
    "build_sigpac_zip_metadata_probe_plan",
    "main",
    "metadata_probe_source_definition",
    "probe_reviewed_zip_metadata",
    "write_hash_bound_probe_manifest",
]


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    raise SystemExit(main())
