"""Explicit, append-only authorization for SIUR local mirror operations.

The older WMS license review governs only proxy/cache behavior.  This module
intentionally has no dependency on that evidence: a local mirror must carry a
human review bound to the exact acquisition source and its current definition.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.model_registry import register_all_models
from app.db.session import SessionLocal
from app.reference_layers.models import (
    ReferenceDeliveryVersion,
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceService,
    ReferenceSyncRun,
)
from app.reference_layers.reviewed_ortho_evidence import (
    ReviewedOrthoEvidenceError,
    require_reviewed_ign_ortho_acquisition_allowed,
)


SCHEMA_VERSION = "siur-mirror-authorization-v1"
MAX_DOCUMENT_BYTES = 256 * 1024
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,63}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_TEMPLATE_TOKEN_RE = re.compile(r"\{[^{}]+\}")
_URL_KEYS = frozenset(
    {
        "download_url",
        "endpoint",
        "endpoint_url",
        "href",
        "items_url",
        "metadata_url",
        "source_url",
        "style_endpoint_url",
        "template_url",
        "tile_url",
        "url",
        "url_template",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "provider_key",
        "service_id",
        "layer_id",
        "source_id",
        "source_definition_sha256",
        "protocol",
        "target_kind",
        "canonical_origin",
        "allowed_origins",
        "decision",
        "reviewer",
        "reviewed_at",
        "license",
        "permissions",
        "supersedes_review_sha256",
    }
)
_LICENSE_KEYS = frozenset({"name", "url", "terms", "attribution"})
_PERMISSION_KEYS = frozenset(
    {
        "metadata_probe",
        "dataset_download",
        "local_storage",
        "local_service",
        "bulk_tile_seed",
    }
)
_SOURCE_PROTOCOLS = frozenset(
    {
        "wfs",
        "ogc_api_features",
        "wcs",
        "arcgis_rest",
        "atom",
        "download",
        "wmts",
        "xyz",
        "wms_tiles",
        "local",
    }
)
_TARGET_KINDS = frozenset({"vector", "raster", "tiles"})


class MirrorAuthorizationError(RuntimeError):
    """A stable fail-closed authorization rejection."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class MirrorAuthorizationDocumentError(ValueError):
    """A local review document is malformed or internally inconsistent."""


@dataclass(frozen=True)
class MirrorAuthorizationEvidence:
    raw_document: bytes
    document_sha256: str
    review_sha256: str
    provider_key: str
    service_id: int
    layer_id: int
    source_id: int
    source_definition_sha256: str
    protocol: str
    target_kind: str
    canonical_origin: str
    allowed_origins: tuple[str, ...]
    decision: str
    reviewer: str
    reviewed_at: datetime
    license_name: str
    license_url: str
    license_terms: str
    attribution: str | None
    allow_metadata_probe: bool
    allow_dataset_download: bool
    allow_local_storage: bool
    allow_local_service: bool
    allow_bulk_tile_seed: bool
    supersedes_review_sha256: str | None

    def semantic_document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "provider_key": self.provider_key,
            "service_id": self.service_id,
            "layer_id": self.layer_id,
            "source_id": self.source_id,
            "source_definition_sha256": self.source_definition_sha256,
            "protocol": self.protocol,
            "target_kind": self.target_kind,
            "canonical_origin": self.canonical_origin,
            "allowed_origins": list(self.allowed_origins),
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reviewed_at": _utc_isoformat(self.reviewed_at),
            "license": {
                "name": self.license_name,
                "url": self.license_url,
                "terms": self.license_terms,
                "attribution": self.attribution,
            },
            "permissions": {
                "metadata_probe": self.allow_metadata_probe,
                "dataset_download": self.allow_dataset_download,
                "local_storage": self.allow_local_storage,
                "local_service": self.allow_local_service,
                "bulk_tile_seed": self.allow_bulk_tile_seed,
            },
            "supersedes_review_sha256": (
                self.supersedes_review_sha256
            ),
        }


@dataclass(frozen=True)
class MirrorAuthorizationPlan:
    evidence: MirrorAuthorizationEvidence
    predecessor_id: int | None
    predecessor_sha256: str | None
    already_applied_id: int | None

    def public_summary(self, *, applied: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "apply" if applied else "dry-run",
            "applied": applied,
            "schema_version": SCHEMA_VERSION,
            "provider_key": self.evidence.provider_key,
            "service_id": self.evidence.service_id,
            "layer_id": self.evidence.layer_id,
            "source_id": self.evidence.source_id,
            "source_definition_sha256": (
                self.evidence.source_definition_sha256
            ),
            "protocol": self.evidence.protocol,
            "target_kind": self.evidence.target_kind,
            "decision": self.evidence.decision,
            "review_sha256": self.evidence.review_sha256,
            "document_sha256": self.evidence.document_sha256,
            "supersedes_review_sha256": (
                self.evidence.supersedes_review_sha256
            ),
            "already_applied": self.already_applied_id is not None,
            "review_id": self.already_applied_id,
        }


def parse_mirror_authorization(
    document: bytes,
) -> MirrorAuthorizationEvidence:
    """Parse one bounded JSON document, rejecting duplicate or extra keys."""

    if not isinstance(document, bytes):
        raise MirrorAuthorizationDocumentError(
            "authorization document must be bytes"
        )
    if not 1 <= len(document) <= MAX_DOCUMENT_BYTES:
        raise MirrorAuthorizationDocumentError(
            "authorization document size is invalid"
        )
    if document.startswith(b"\xef\xbb\xbf"):
        raise MirrorAuthorizationDocumentError(
            "authorization document must not contain a UTF-8 BOM"
        )
    try:
        text_document = document.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MirrorAuthorizationDocumentError(
            "authorization document must be UTF-8"
        ) from error
    try:
        parsed = json.loads(
            text_document,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise MirrorAuthorizationDocumentError(
            "authorization document is not strict JSON"
        ) from error
    _bounded_json_tree(parsed)
    root = _exact_object(parsed, _TOP_LEVEL_KEYS, "document")
    if root["schema_version"] != SCHEMA_VERSION:
        raise MirrorAuthorizationDocumentError(
            "authorization schema_version is unsupported"
        )

    provider_key = _required_text(
        root["provider_key"],
        "provider_key",
        64,
    )
    if _PROVIDER_RE.fullmatch(provider_key) is None:
        raise MirrorAuthorizationDocumentError("provider_key is invalid")
    service_id = _positive_integer(root["service_id"], "service_id")
    layer_id = _positive_integer(root["layer_id"], "layer_id")
    source_id = _positive_integer(root["source_id"], "source_id")
    source_definition_sha256 = _sha256(
        root["source_definition_sha256"],
        "source_definition_sha256",
    )
    protocol = _required_text(root["protocol"], "protocol", 32)
    if protocol not in _SOURCE_PROTOCOLS:
        raise MirrorAuthorizationDocumentError("protocol is invalid")
    target_kind = _required_text(
        root["target_kind"],
        "target_kind",
        16,
    )
    if target_kind not in _TARGET_KINDS:
        raise MirrorAuthorizationDocumentError("target_kind is invalid")

    raw_origin = _required_text(
        root["canonical_origin"],
        "canonical_origin",
        512,
    )
    canonical_origin = canonical_https_origin(raw_origin)
    if canonical_origin != raw_origin:
        raise MirrorAuthorizationDocumentError(
            "canonical_origin is not canonical"
        )
    raw_origins = root["allowed_origins"]
    if (
        not isinstance(raw_origins, list)
        or not 1 <= len(raw_origins) <= 32
    ):
        raise MirrorAuthorizationDocumentError(
            "allowed_origins must contain between 1 and 32 origins"
        )
    origins: list[str] = []
    for index, value in enumerate(raw_origins):
        origin = canonical_https_origin(
            _required_text(
                value,
                f"allowed_origins[{index}]",
                512,
            )
        )
        if origin != value:
            raise MirrorAuthorizationDocumentError(
                f"allowed_origins[{index}] is not canonical"
            )
        origins.append(origin)
    if len(set(origins)) != len(origins):
        raise MirrorAuthorizationDocumentError(
            "allowed_origins contains duplicates"
        )
    allowed_origins = tuple(sorted(origins))
    if canonical_origin not in allowed_origins:
        raise MirrorAuthorizationDocumentError(
            "canonical_origin is not an allowed origin"
        )

    decision = _required_text(root["decision"], "decision", 20)
    if decision not in {"approved", "restricted", "rejected"}:
        raise MirrorAuthorizationDocumentError("decision is invalid")
    reviewer = _required_text(root["reviewer"], "reviewer", 255)
    reviewed_at = _aware_datetime(root["reviewed_at"], "reviewed_at")
    license_value = _exact_object(
        root["license"],
        _LICENSE_KEYS,
        "license",
    )
    license_name = _required_text(
        license_value["name"],
        "license.name",
        500,
    )
    license_url = _https_url(
        license_value["url"],
        "license.url",
        8192,
    )
    license_terms = _required_text(
        license_value["terms"],
        "license.terms",
        20_000,
    )
    attribution = _optional_text(
        license_value["attribution"],
        "license.attribution",
        8_192,
    )

    permissions = _exact_object(
        root["permissions"],
        _PERMISSION_KEYS,
        "permissions",
    )
    allow_metadata_probe = _boolean(
        permissions["metadata_probe"],
        "permissions.metadata_probe",
    )
    allow_dataset_download = _boolean(
        permissions["dataset_download"],
        "permissions.dataset_download",
    )
    allow_local_storage = _boolean(
        permissions["local_storage"],
        "permissions.local_storage",
    )
    allow_local_service = _boolean(
        permissions["local_service"],
        "permissions.local_service",
    )
    allow_bulk_tile_seed = _boolean(
        permissions["bulk_tile_seed"],
        "permissions.bulk_tile_seed",
    )
    permission_values = (
        allow_metadata_probe,
        allow_dataset_download,
        allow_local_storage,
        allow_local_service,
        allow_bulk_tile_seed,
    )
    if decision != "approved" and any(permission_values):
        raise MirrorAuthorizationDocumentError(
            "only approved reviews may grant permissions"
        )
    if allow_local_storage and not allow_dataset_download:
        raise MirrorAuthorizationDocumentError(
            "local_storage requires dataset_download"
        )
    if allow_local_service and not allow_local_storage:
        raise MirrorAuthorizationDocumentError(
            "local_service requires local_storage"
        )
    if allow_local_service and attribution is None:
        raise MirrorAuthorizationDocumentError(
            "local_service requires attribution"
        )
    if (
        target_kind == "tiles"
        and allow_local_service
        and not allow_bulk_tile_seed
    ):
        raise MirrorAuthorizationDocumentError(
            "tile local_service requires bulk_tile_seed"
        )
    supersedes = root["supersedes_review_sha256"]
    supersedes_review_sha256 = (
        None
        if supersedes is None
        else _sha256(supersedes, "supersedes_review_sha256")
    )

    values = {
        "raw_document": document,
        "document_sha256": hashlib.sha256(document).hexdigest(),
        "review_sha256": "",
        "provider_key": provider_key,
        "service_id": service_id,
        "layer_id": layer_id,
        "source_id": source_id,
        "source_definition_sha256": source_definition_sha256,
        "protocol": protocol,
        "target_kind": target_kind,
        "canonical_origin": canonical_origin,
        "allowed_origins": allowed_origins,
        "decision": decision,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "license_name": license_name,
        "license_url": license_url,
        "license_terms": license_terms,
        "attribution": attribution,
        "allow_metadata_probe": allow_metadata_probe,
        "allow_dataset_download": allow_dataset_download,
        "allow_local_storage": allow_local_storage,
        "allow_local_service": allow_local_service,
        "allow_bulk_tile_seed": allow_bulk_tile_seed,
        "supersedes_review_sha256": supersedes_review_sha256,
    }
    draft = MirrorAuthorizationEvidence(**values)
    values["review_sha256"] = _canonical_json_sha256(
        draft.semantic_document()
    )
    return MirrorAuthorizationEvidence(**values)


def canonical_https_origin(value: str) -> str:
    """Return a strict HTTPS origin without a path, query or credentials."""

    if not isinstance(value, str) or not value or len(value) > 8192:
        raise MirrorAuthorizationDocumentError("HTTPS origin is invalid")
    rendered = _TEMPLATE_TOKEN_RE.sub("0", value)
    try:
        parts = urlsplit(rendered)
        port = parts.port
    except ValueError as error:
        raise MirrorAuthorizationDocumentError(
            "HTTPS origin is invalid"
        ) from error
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise MirrorAuthorizationDocumentError("HTTPS origin is invalid")
    host = parts.hostname.rstrip(".").lower()
    if not host or any(ord(character) > 127 for character in host):
        raise MirrorAuthorizationDocumentError("HTTPS origin host is invalid")
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def url_origin(value: str) -> str:
    """Extract a canonical HTTPS origin from a URL or URL template."""

    if not isinstance(value, str) or not value or len(value) > 8192:
        raise MirrorAuthorizationDocumentError("source URL is invalid")
    rendered = _TEMPLATE_TOKEN_RE.sub("0", value)
    try:
        parts = urlsplit(rendered)
        port = parts.port
    except ValueError as error:
        raise MirrorAuthorizationDocumentError(
            "source URL is invalid"
        ) from error
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise MirrorAuthorizationDocumentError("source URL is invalid")
    host = parts.hostname.rstrip(".").lower()
    if not host or any(ord(character) > 127 for character in host):
        raise MirrorAuthorizationDocumentError("source URL host is invalid")
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def stored_mirror_authorization_review_is_valid(
    review: ReferenceMirrorAuthorizationReview,
) -> bool:
    """Verify raw bytes, canonical semantics and every projected column."""

    try:
        evidence = parse_mirror_authorization(
            bytes(review.reviewed_document)
        )
        reviewed_at = _aware_utc(review.reviewed_at)
        raw_allowed_origins = review.allowed_origins_json
        if (
            not isinstance(raw_allowed_origins, list)
            or any(
                not isinstance(origin, str)
                for origin in raw_allowed_origins
            )
        ):
            return False
        allowed_origins = tuple(sorted(raw_allowed_origins))
    except (
        MirrorAuthorizationDocumentError,
        TypeError,
        ValueError,
        UnicodeError,
    ):
        return False
    return bool(
        review.document_size_bytes == len(evidence.raw_document)
        and review.document_sha256 == evidence.document_sha256
        and review.review_sha256 == evidence.review_sha256
        and review.provider_key == evidence.provider_key
        and review.service_id == evidence.service_id
        and review.layer_id == evidence.layer_id
        and review.source_id == evidence.source_id
        and review.source_definition_sha256
        == evidence.source_definition_sha256
        and review.protocol == evidence.protocol
        and review.target_kind == evidence.target_kind
        and review.canonical_origin == evidence.canonical_origin
        and allowed_origins == evidence.allowed_origins
        and review.decision == evidence.decision
        and review.reviewer == evidence.reviewer
        and reviewed_at == evidence.reviewed_at
        and review.license_name == evidence.license_name
        and review.license_url == evidence.license_url
        and review.license_terms == evidence.license_terms
        and review.attribution == evidence.attribution
        and review.allow_metadata_probe == evidence.allow_metadata_probe
        and review.allow_dataset_download
        == evidence.allow_dataset_download
        and review.allow_local_storage == evidence.allow_local_storage
        and review.allow_local_service == evidence.allow_local_service
        and review.allow_bulk_tile_seed == evidence.allow_bulk_tile_seed
        and review.supersedes_review_sha256
        == evidence.supersedes_review_sha256
        and (
            (review.supersedes_review_id is None)
            == (evidence.supersedes_review_sha256 is None)
        )
    )


def authorization_chain_is_valid(
    reviews: Sequence[ReferenceMirrorAuthorizationReview],
) -> bool:
    """Validate one complete, linear source-scoped chain."""

    if not reviews:
        return False
    previous: ReferenceMirrorAuthorizationReview | None = None
    seen_ids: set[int] = set()
    seen_hashes: set[str] = set()
    identity = (
        reviews[0].provider_key,
        reviews[0].layer_id,
        reviews[0].source_id,
    )
    for review in reviews:
        if (
            review.id in seen_ids
            or review.review_sha256 in seen_hashes
            or (
                review.provider_key,
                review.layer_id,
                review.source_id,
            )
            != identity
            or not stored_mirror_authorization_review_is_valid(review)
        ):
            return False
        if previous is None:
            if (
                review.supersedes_review_id is not None
                or review.supersedes_review_sha256 is not None
            ):
                return False
        elif (
            review.supersedes_review_id != previous.id
            or review.supersedes_review_sha256
            != previous.review_sha256
            or _aware_utc(review.reviewed_at)
            <= _aware_utc(previous.reviewed_at)
        ):
            return False
        seen_ids.add(review.id)
        seen_hashes.add(review.review_sha256)
        previous = review
    return True


def plan_mirror_authorization_review(
    db: Session,
    document: bytes,
    *,
    lock: bool = False,
) -> MirrorAuthorizationPlan:
    """Build a review plan against the exact current source and chain head."""

    evidence = parse_mirror_authorization(document)
    if lock:
        _lock_authorization_source(db, evidence.source_id)
    source_query = select(ReferenceLayerSource).where(
        ReferenceLayerSource.id == evidence.source_id,
        ReferenceLayerSource.provider_key == evidence.provider_key,
        ReferenceLayerSource.layer_id == evidence.layer_id,
    )
    if lock:
        source_query = source_query.with_for_update()
    source = db.scalar(source_query)
    layer = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.id == evidence.layer_id,
            ReferenceLayer.provider_key == evidence.provider_key,
            ReferenceLayer.service_id == evidence.service_id,
            ReferenceLayer.node_type == "layer",
        )
    )
    if source is None or layer is None:
        raise MirrorAuthorizationDocumentError(
            "authorization source identity is not current"
        )
    _validate_evidence_for_source(evidence, source)
    reviews = _authorization_chain(db, source.id, lock=lock)
    if reviews and not authorization_chain_is_valid(reviews):
        raise MirrorAuthorizationDocumentError(
            "stored authorization chain is invalid"
        )
    existing = next(
        (
            review
            for review in reviews
            if review.review_sha256 == evidence.review_sha256
            and review.document_sha256 == evidence.document_sha256
        ),
        None,
    )
    latest = reviews[-1] if reviews else None
    if existing is not None:
        if latest is None or existing.id != latest.id:
            raise MirrorAuthorizationDocumentError(
                "authorization document already exists but is not the head"
            )
        return MirrorAuthorizationPlan(
            evidence,
            latest.supersedes_review_id,
            latest.supersedes_review_sha256,
            latest.id,
        )
    if latest is None:
        if evidence.supersedes_review_sha256 is not None:
            raise MirrorAuthorizationDocumentError(
                "genesis authorization must not supersede another review"
            )
    else:
        if evidence.supersedes_review_sha256 != latest.review_sha256:
            raise MirrorAuthorizationDocumentError(
                "authorization does not supersede the current chain head"
            )
        if evidence.reviewed_at <= _aware_utc(latest.reviewed_at):
            raise MirrorAuthorizationDocumentError(
                "authorization reviewed_at must follow its predecessor"
            )
    return MirrorAuthorizationPlan(
        evidence,
        latest.id if latest is not None else None,
        latest.review_sha256 if latest is not None else None,
        None,
    )


def apply_mirror_authorization_review(
    db: Session,
    document: bytes,
    *,
    expected_review_sha256: str,
    expected_document_sha256: str,
) -> ReferenceMirrorAuthorizationReview:
    """Append one exact reviewed plan after lock-scoped revalidation."""

    expected_review = _sha256(
        expected_review_sha256,
        "expected_review_sha256",
    )
    expected_document = _sha256(
        expected_document_sha256,
        "expected_document_sha256",
    )
    try:
        plan = plan_mirror_authorization_review(db, document, lock=True)
        if plan.evidence.review_sha256 != expected_review:
            raise MirrorAuthorizationDocumentError(
                "review hash changed after the dry-run"
            )
        if plan.evidence.document_sha256 != expected_document:
            raise MirrorAuthorizationDocumentError(
                "document hash changed after the dry-run"
            )
        if plan.already_applied_id is not None:
            existing = db.get(
                ReferenceMirrorAuthorizationReview,
                plan.already_applied_id,
            )
            if (
                existing is None
                or not stored_mirror_authorization_review_is_valid(existing)
            ):
                raise MirrorAuthorizationDocumentError(
                    "stored authorization is invalid"
                )
            db.commit()
            return existing
        evidence = plan.evidence
        review = ReferenceMirrorAuthorizationReview(
            provider_key=evidence.provider_key,
            service_id=evidence.service_id,
            layer_id=evidence.layer_id,
            source_id=evidence.source_id,
            source_definition_sha256=(
                evidence.source_definition_sha256
            ),
            protocol=evidence.protocol,
            target_kind=evidence.target_kind,
            canonical_origin=evidence.canonical_origin,
            allowed_origins_json=list(evidence.allowed_origins),
            reviewed_document=evidence.raw_document,
            document_size_bytes=len(evidence.raw_document),
            document_sha256=evidence.document_sha256,
            review_sha256=evidence.review_sha256,
            supersedes_review_id=plan.predecessor_id,
            supersedes_review_sha256=plan.predecessor_sha256,
            decision=evidence.decision,
            reviewer=evidence.reviewer,
            reviewed_at=evidence.reviewed_at,
            license_name=evidence.license_name,
            license_url=evidence.license_url,
            license_terms=evidence.license_terms,
            attribution=evidence.attribution,
            allow_metadata_probe=evidence.allow_metadata_probe,
            allow_dataset_download=evidence.allow_dataset_download,
            allow_local_storage=evidence.allow_local_storage,
            allow_local_service=evidence.allow_local_service,
            allow_bulk_tile_seed=evidence.allow_bulk_tile_seed,
        )
        db.add(review)
        db.flush()
        db.commit()
        return review
    except Exception:
        db.rollback()
        raise


def bind_sync_run_authorization(
    db: Session,
    *,
    run: ReferenceSyncRun,
    source: ReferenceLayerSource,
) -> ReferenceMirrorAuthorizationReview:
    """Bind a running job to the approval used before its first network call."""

    current = require_current_source_authorization(
        db,
        source=source,
        require_acquisition=True,
    )
    if (
        run.provider_key != source.provider_key
        or run.layer_id != source.layer_id
        or run.source_id != source.id
        or run.source_definition_sha256 != source.definition_sha256
        or _canonical_json_sha256(run.source_definition_json)
        != run.source_definition_sha256
    ):
        raise MirrorAuthorizationError(
            "sync-run source identity changed",
            code="mirror_authorization_source_changed",
        )
    _validate_urls_within_origins(
        _url_values(run.source_definition_json),
        current.allowed_origins_json,
    )
    if run.mirror_authorization_review_id is None:
        if run.mirror_authorization_review_sha256 is not None:
            raise MirrorAuthorizationError(
                "sync-run authorization link is partial",
                code="mirror_authorization_invalid",
            )
        run.mirror_authorization_review_id = current.id
        run.mirror_authorization_review_sha256 = current.review_sha256
        db.flush()
        return current
    original = _review_from_chain(
        db,
        source=source,
        review_id=run.mirror_authorization_review_id,
        review_sha256=run.mirror_authorization_review_sha256,
    )
    _require_permissions(original, source=source, acquisition=True)
    return original


def require_bound_sync_run_authorization(
    db: Session,
    *,
    run: ReferenceSyncRun,
    source: ReferenceLayerSource,
    acquired: Any | None = None,
) -> ReferenceMirrorAuthorizationReview:
    """Revalidate original evidence and current head between worker stages."""

    if (
        run.mirror_authorization_review_id is None
        or run.mirror_authorization_review_sha256 is None
    ):
        raise MirrorAuthorizationError(
            "sync run has no mirror authorization",
            code="mirror_authorization_missing",
        )
    original = _review_from_chain(
        db,
        source=source,
        review_id=run.mirror_authorization_review_id,
        review_sha256=run.mirror_authorization_review_sha256,
    )
    _require_permissions(original, source=source, acquisition=True)
    current = require_current_source_authorization(
        db,
        source=source,
        require_acquisition=True,
    )
    if acquired is not None:
        _validate_acquisition_result_origins(
            acquired,
            source,
            current.allowed_origins_json,
        )
    return current


def require_version_local_service_authorization(
    db: Session,
    *,
    version: ReferenceDeliveryVersion,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
) -> ReferenceMirrorAuthorizationReview:
    """Require intact original evidence and a currently serviceable head."""

    if (
        version.mirror_authorization_review_id is None
        or version.mirror_authorization_review_sha256 is None
        or run.mirror_authorization_review_id
        != version.mirror_authorization_review_id
        or run.mirror_authorization_review_sha256
        != version.mirror_authorization_review_sha256
    ):
        raise MirrorAuthorizationError(
            "delivery version has no valid mirror authorization link",
            code="mirror_authorization_missing",
        )
    _review_from_chain(
        db,
        source=source,
        review_id=version.mirror_authorization_review_id,
        review_sha256=version.mirror_authorization_review_sha256,
        frozen_run=run,
    )
    return require_current_source_authorization(
        db,
        source=source,
        require_acquisition=False,
    )


def require_current_source_authorization(
    db: Session,
    *,
    source: ReferenceLayerSource,
    require_acquisition: bool,
) -> ReferenceMirrorAuthorizationReview:
    """Return the verified current head or raise one stable blocker."""

    reviews = _authorization_chain(db, source.id)
    if not reviews:
        raise MirrorAuthorizationError(
            "mirror authorization is missing",
            code="mirror_authorization_missing",
        )
    if not authorization_chain_is_valid(reviews):
        raise MirrorAuthorizationError(
            "mirror authorization chain is invalid",
            code="mirror_authorization_invalid",
        )
    current = reviews[-1]
    _validate_review_source(current, source)
    _require_permissions(
        current,
        source=source,
        acquisition=require_acquisition,
    )
    return current


def require_current_source_metadata_probe_authorization(
    db: Session,
    *,
    source: ReferenceLayerSource,
) -> ReferenceMirrorAuthorizationReview:
    """Authorize one metadata-only request without granting dataset access."""

    reviews = _authorization_chain(db, source.id)
    if not reviews:
        raise MirrorAuthorizationError(
            "mirror authorization is missing",
            code="mirror_authorization_missing",
        )
    if not authorization_chain_is_valid(reviews):
        raise MirrorAuthorizationError(
            "mirror authorization chain is invalid",
            code="mirror_authorization_invalid",
        )
    current = reviews[-1]
    _validate_review_source(current, source)
    if current.decision != "approved" or not current.allow_metadata_probe:
        raise MirrorAuthorizationError(
            "current mirror authorization does not grant metadata probing",
            code="mirror_authorization_restricted",
        )
    return current


def source_authorization_blocker(
    db: Session,
    *,
    source: ReferenceLayerSource,
    require_acquisition: bool = True,
) -> str | None:
    try:
        require_current_source_authorization(
            db,
            source=source,
            require_acquisition=require_acquisition,
        )
    except MirrorAuthorizationError as error:
        return error.code
    return None


def version_authorization_blocker(
    db: Session,
    *,
    version: ReferenceDeliveryVersion,
    source: ReferenceLayerSource,
    run: ReferenceSyncRun,
) -> str | None:
    try:
        require_version_local_service_authorization(
            db,
            version=version,
            source=source,
            run=run,
        )
    except MirrorAuthorizationError as error:
        return error.code
    return None


def effective_service_attributions(
    db: Session,
    *,
    services: Sequence[ReferenceService],
) -> dict[int, str | None]:
    """Project approved current mirror attribution without mutating catalog."""

    result = {service.id: service.attribution for service in services}
    if not services:
        return result
    service_ids = [service.id for service in services]
    rows = db.execute(
        select(ReferenceLayerSource, ReferenceLayer.service_id)
        .join(
            ReferenceLayer,
            (ReferenceLayer.id == ReferenceLayerSource.layer_id)
            & (
                ReferenceLayer.provider_key
                == ReferenceLayerSource.provider_key
            ),
        )
        .where(
            ReferenceLayer.service_id.in_(service_ids),
            ReferenceLayer.node_type == "layer",
            ReferenceLayer.status.in_(("active", "degraded")),
            ReferenceLayerSource.enabled.is_(True),
        )
        .order_by(
            ReferenceLayer.service_id,
            ReferenceLayerSource.id,
        )
    ).all()
    values: dict[int, set[str]] = {}
    for source, service_id in rows:
        try:
            review = require_current_source_authorization(
                db,
                source=source,
                require_acquisition=False,
            )
        except MirrorAuthorizationError:
            continue
        if review.attribution:
            values.setdefault(service_id, set()).add(review.attribution)
    for service_id, attributions in values.items():
        rendered = " · ".join(sorted(attributions))
        if len(rendered) <= 8_192:
            result[service_id] = rendered
    return result


def _validate_evidence_for_source(
    evidence: MirrorAuthorizationEvidence,
    source: ReferenceLayerSource,
) -> None:
    if (
        evidence.provider_key != source.provider_key
        or evidence.layer_id != source.layer_id
        or evidence.source_id != source.id
        or evidence.source_definition_sha256 != source.definition_sha256
        or evidence.protocol != source.protocol
        or evidence.target_kind != source.target_kind
    ):
        raise MirrorAuthorizationDocumentError(
            "authorization does not match the exact source definition"
        )
    if source.endpoint_url is None:
        raise MirrorAuthorizationDocumentError(
            "mirror authorization requires an exact HTTPS source"
        )
    if url_origin(source.endpoint_url) != evidence.canonical_origin:
        raise MirrorAuthorizationDocumentError(
            "authorization canonical origin does not match the source"
        )
    try:
        definition_hash = _canonical_json_sha256(
            _stored_source_definition(source)
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise MirrorAuthorizationDocumentError(
            "source definition is not canonical"
        ) from error
    if definition_hash != source.definition_sha256:
        raise MirrorAuthorizationDocumentError(
            "stored source definition hash is invalid"
        )
    try:
        require_reviewed_ign_ortho_acquisition_allowed(
            _stored_source_definition(source)
        )
    except ReviewedOrthoEvidenceError as error:
        raise MirrorAuthorizationDocumentError(
            "reviewed ortho source is not eligible for local acquisition"
        ) from error
    _validate_urls_within_origins(
        (
            source.endpoint_url,
            *_url_values(source.config_json),
        ),
        evidence.allowed_origins,
    )


def _validate_review_source(
    review: ReferenceMirrorAuthorizationReview,
    source: ReferenceLayerSource,
) -> None:
    try:
        if (
            review.provider_key != source.provider_key
            or review.layer_id != source.layer_id
            or review.source_id != source.id
            or review.source_definition_sha256
            != source.definition_sha256
            or review.protocol != source.protocol
            or review.target_kind != source.target_kind
            or source.endpoint_url is None
            or url_origin(source.endpoint_url) != review.canonical_origin
            or _canonical_json_sha256(_stored_source_definition(source))
            != source.definition_sha256
        ):
            raise MirrorAuthorizationError(
                "mirror authorization source changed",
                code="mirror_authorization_source_changed",
            )
        _validate_urls_within_origins(
            (
                source.endpoint_url,
                *_url_values(source.config_json),
            ),
            review.allowed_origins_json,
        )
    except (
        MirrorAuthorizationDocumentError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise MirrorAuthorizationError(
            "mirror authorization source changed",
            code="mirror_authorization_source_changed",
        ) from error


def _require_permissions(
    review: ReferenceMirrorAuthorizationReview,
    *,
    source: ReferenceLayerSource,
    acquisition: bool,
) -> None:
    try:
        require_reviewed_ign_ortho_acquisition_allowed(
            _stored_source_definition(source)
        )
    except ReviewedOrthoEvidenceError as error:
        raise MirrorAuthorizationError(
            "reviewed ortho source is not eligible for local acquisition",
            code="reviewed_ortho_substitution_blocked",
        ) from error
    if (
        review.decision != "approved"
        or not review.allow_local_storage
        or not review.allow_local_service
        or not review.attribution
        or (
            acquisition
            and (
                not review.allow_metadata_probe
                or not review.allow_dataset_download
            )
        )
        or (
            source.target_kind == "tiles"
            and not review.allow_bulk_tile_seed
        )
    ):
        raise MirrorAuthorizationError(
            "current mirror authorization does not grant this operation",
            code="mirror_authorization_restricted",
        )


def _review_from_chain(
    db: Session,
    *,
    source: ReferenceLayerSource,
    review_id: int,
    review_sha256: str | None,
    frozen_run: ReferenceSyncRun | None = None,
) -> ReferenceMirrorAuthorizationReview:
    reviews = _authorization_chain(db, source.id)
    if not reviews:
        raise MirrorAuthorizationError(
            "mirror authorization is missing",
            code="mirror_authorization_missing",
        )
    if not authorization_chain_is_valid(reviews):
        raise MirrorAuthorizationError(
            "mirror authorization chain is invalid",
            code="mirror_authorization_invalid",
        )
    review = next(
        (
            item
            for item in reviews
            if item.id == review_id
            and item.review_sha256 == review_sha256
        ),
        None,
    )
    if review is None:
        raise MirrorAuthorizationError(
            "linked mirror authorization is invalid",
            code="mirror_authorization_invalid",
        )
    if frozen_run is None:
        _validate_review_source(review, source)
    else:
        _validate_review_run(review, frozen_run)
    return review


def _validate_review_run(
    review: ReferenceMirrorAuthorizationReview,
    run: ReferenceSyncRun,
) -> None:
    try:
        definition = run.source_definition_json
        if (
            review.provider_key != run.provider_key
            or review.layer_id != run.layer_id
            or review.source_id != run.source_id
            or review.source_definition_sha256
            != run.source_definition_sha256
            or not isinstance(definition, dict)
            or _canonical_json_sha256(definition)
            != run.source_definition_sha256
            or review.protocol != definition.get("protocol")
            or review.target_kind != definition.get("target_kind")
        ):
            raise MirrorAuthorizationError(
                "mirror authorization does not match the frozen run",
                code="mirror_authorization_source_changed",
            )
        endpoint_url = definition.get("endpoint_url")
        if (
            not isinstance(endpoint_url, str)
            or url_origin(endpoint_url) != review.canonical_origin
        ):
            raise MirrorAuthorizationError(
                "mirror authorization origin does not match the frozen run",
                code="mirror_authorization_source_changed",
            )
        _validate_urls_within_origins(
            _url_values(definition),
            review.allowed_origins_json,
        )
    except (
        MirrorAuthorizationDocumentError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        raise MirrorAuthorizationError(
            "mirror authorization does not match the frozen run",
            code="mirror_authorization_source_changed",
        ) from error


def _authorization_chain(
    db: Session,
    source_id: int,
    *,
    lock: bool = False,
) -> tuple[ReferenceMirrorAuthorizationReview, ...]:
    query = (
        select(ReferenceMirrorAuthorizationReview)
        .where(ReferenceMirrorAuthorizationReview.source_id == source_id)
        .order_by(
            ReferenceMirrorAuthorizationReview.reviewed_at,
            ReferenceMirrorAuthorizationReview.id,
        )
    )
    if lock:
        query = query.with_for_update()
    return tuple(db.scalars(query))


def _validate_acquisition_result_origins(
    acquired: Any,
    source: ReferenceLayerSource,
    allowed_origins: Iterable[str],
) -> None:
    if (
        getattr(acquired, "source_key", None) != source.source_key
        or getattr(acquired, "source_definition_sha256", None)
        != source.definition_sha256
        or getattr(acquired, "protocol", None) != source.protocol
        or getattr(acquired, "target_kind", None) != source.target_kind
    ):
        raise MirrorAuthorizationError(
            "acquisition result does not match the authorized source",
            code="mirror_authorization_source_changed",
        )
    artifacts = getattr(acquired, "artifacts", None)
    if not isinstance(artifacts, tuple):
        raise MirrorAuthorizationError(
            "acquisition result cannot be origin-validated",
            code="mirror_authorization_invalid",
        )
    urls: list[str] = []
    for artifact in artifacts:
        for name in ("source_url", "final_url"):
            value = getattr(artifact, name, None)
            if value is not None:
                if not isinstance(value, str):
                    raise MirrorAuthorizationError(
                        "acquisition URL evidence is invalid",
                        code="mirror_authorization_invalid",
                    )
                urls.append(value)
        metadata = getattr(artifact, "metadata", None)
        if metadata is not None:
            urls.extend(_url_values(metadata))
    try:
        _validate_urls_within_origins(urls, allowed_origins)
    except MirrorAuthorizationDocumentError as error:
        raise MirrorAuthorizationError(
            "acquisition crossed an unauthorized origin",
            code="mirror_authorization_source_changed",
        ) from error


def _validate_urls_within_origins(
    urls: Iterable[str],
    allowed_origins: Iterable[str],
) -> None:
    allowed = frozenset(allowed_origins)
    if not allowed:
        raise MirrorAuthorizationDocumentError(
            "authorization has no allowed origins"
        )
    for value in urls:
        if url_origin(value) not in allowed:
            raise MirrorAuthorizationDocumentError(
                "source URL is outside the approved origins"
            )


def _url_values(value: Any, *, key: str | None = None) -> tuple[str, ...]:
    """Extract only strings held by explicitly URL-shaped configuration keys."""

    urls: list[str] = []

    def visit(item: Any, item_key: str | None, depth: int) -> None:
        if depth > 16:
            raise MirrorAuthorizationDocumentError(
                "source configuration is too deeply nested"
            )
        url_key = bool(
            item_key
            and (
                item_key.casefold() in _URL_KEYS
                or item_key.casefold().endswith("_url")
                or item_key.casefold().endswith("_urls")
            )
        )
        if isinstance(item, str):
            if url_key:
                urls.append(item)
            return
        if isinstance(item, Mapping):
            if len(item) > 512:
                raise MirrorAuthorizationDocumentError(
                    "source configuration is too large"
                )
            for child_key, child in item.items():
                if not isinstance(child_key, str):
                    raise MirrorAuthorizationDocumentError(
                        "source configuration key is invalid"
                    )
                visit(child, child_key, depth + 1)
            return
        if isinstance(item, (list, tuple)):
            if len(item) > 2048:
                raise MirrorAuthorizationDocumentError(
                    "source configuration is too large"
                )
            for child in item:
                visit(child, item_key, depth + 1)

    visit(value, key, 0)
    return tuple(urls)


def _stored_source_definition(
    source: ReferenceLayerSource,
) -> dict[str, Any]:
    return {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": source.priority,
        "config": source.config_json,
    }


def _lock_authorization_source(db: Session, source_id: int) -> None:
    db.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:authorization_key, 0))"
        ),
        {"authorization_key": f"reference-mirror-authorization:{source_id}"},
    )


def _read_local_document(path_value: str) -> bytes:
    if "://" in path_value:
        raise MirrorAuthorizationDocumentError(
            "authorization input must be a local file"
        )
    path = Path(path_value)
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise MirrorAuthorizationDocumentError(
            "authorization file is unavailable"
        ) from error
    if (
        not stat_module.S_ISREG(path_stat.st_mode)
        or not 1 <= path_stat.st_size <= MAX_DOCUMENT_BYTES
    ):
        raise MirrorAuthorizationDocumentError(
            "authorization file size is invalid"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MirrorAuthorizationDocumentError(
            "authorization file is unavailable"
        ) from error
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            opened_stat = os.fstat(stream.fileno())
            if (
                not stat_module.S_ISREG(opened_stat.st_mode)
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
                or not 1 <= opened_stat.st_size <= MAX_DOCUMENT_BYTES
            ):
                raise MirrorAuthorizationDocumentError(
                    "authorization file is unavailable"
                )
            document = stream.read(MAX_DOCUMENT_BYTES + 1)
            final_stat = os.fstat(stream.fileno())
    except MirrorAuthorizationDocumentError:
        raise
    except OSError as error:
        raise MirrorAuthorizationDocumentError(
            "authorization file is unavailable"
        ) from error
    if (
        len(document) != opened_stat.st_size
        or len(document) > MAX_DOCUMENT_BYTES
        or (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
            opened_stat.st_ctime_ns,
        )
        != (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        )
    ):
        raise MirrorAuthorizationDocumentError(
            "authorization file changed while it was read"
        )
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or append one explicit SIUR local-mirror authorization"
        )
    )
    parser.add_argument("--file", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="append after exact hash confirmation; default is dry-run",
    )
    parser.add_argument("--expected-review-sha256")
    parser.add_argument("--expected-document-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.apply and (
            arguments.expected_review_sha256 is None
            or arguments.expected_document_sha256 is None
        ):
            raise MirrorAuthorizationDocumentError(
                "--apply requires --expected-review-sha256 and "
                "--expected-document-sha256"
            )
        if (
            not arguments.apply
            and (
                arguments.expected_review_sha256 is not None
                or arguments.expected_document_sha256 is not None
            )
        ):
            raise MirrorAuthorizationDocumentError(
                "expected hashes are only valid with --apply"
            )
        document = _read_local_document(arguments.file)
        register_all_models()
        with SessionLocal() as db:
            if arguments.apply:
                review = apply_mirror_authorization_review(
                    db,
                    document,
                    expected_review_sha256=(
                        arguments.expected_review_sha256
                    ),
                    expected_document_sha256=(
                        arguments.expected_document_sha256
                    ),
                )
                plan = plan_mirror_authorization_review(
                    db,
                    bytes(review.reviewed_document),
                )
                result = plan.public_summary(applied=True)
                result["review_id"] = review.id
            else:
                plan = plan_mirror_authorization_review(db, document)
                result = plan.public_summary(applied=False)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except MirrorAuthorizationDocumentError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "mirror_authorization_document_rejected",
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
                    "error_code": "mirror_authorization_service_unavailable",
                    "error_summary": type(error).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MirrorAuthorizationDocumentError(
                f"authorization document contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise MirrorAuthorizationDocumentError(
        f"authorization document contains invalid constant {value}"
    )


def _bounded_json_tree(value: Any) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 512 or depth > 12:
            raise MirrorAuthorizationDocumentError(
                "authorization document is too complex"
            )
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise MirrorAuthorizationDocumentError(
                        "authorization object key is invalid"
                    )
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif not isinstance(item, (str, int, bool, type(None))):
            raise MirrorAuthorizationDocumentError(
                "authorization value type is invalid"
            )

    visit(value, 0)


def _exact_object(
    value: Any,
    expected_keys: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise MirrorAuthorizationDocumentError(
            f"{label} keys do not match the strict schema"
        )
    return value


def _required_text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MirrorAuthorizationDocumentError(f"{label} is invalid")
    return value


def _optional_text(value: Any, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, label, maximum)


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MirrorAuthorizationDocumentError(f"{label} is invalid")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise MirrorAuthorizationDocumentError(f"{label} is invalid")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MirrorAuthorizationDocumentError(f"{label} is invalid")
    return value


def _https_url(value: Any, label: str, maximum: int) -> str:
    rendered = _required_text(value, label, maximum)
    try:
        parts = urlsplit(rendered)
        parts.port
    except ValueError as error:
        raise MirrorAuthorizationDocumentError(f"{label} is invalid") from error
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise MirrorAuthorizationDocumentError(f"{label} is invalid")
    return rendered


def _aware_datetime(value: Any, label: str) -> datetime:
    text_value = _required_text(value, label, 64)
    candidate = (
        f"{text_value[:-1]}+00:00"
        if text_value.endswith("Z")
        else text_value
    )
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise MirrorAuthorizationDocumentError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MirrorAuthorizationDocumentError(
            f"{label} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("datetime value is invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_isoformat(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    raise SystemExit(main())
