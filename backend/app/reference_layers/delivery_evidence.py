"""Immutable technical, legal, and catalog-bound WMS delivery evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers.catalog import (
    SOURCE_KEY_RE,
    canonical_normalized_definition_sha256,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceDeliveryAttestation,
    ReferenceLayer,
    ReferenceLayerStyle,
    ReferenceLicenseReview,
    ReferenceService,
    ReferenceWMSCapabilitiesSnapshot,
)
from app.reference_layers.wms_capabilities import (
    NORMALIZATION_VERSION,
    WMSCapabilitiesError,
    WMSCapabilitiesEvidence,
    canonical_capabilities_sha256,
    parse_wms_capabilities,
)
from app.reference_layers.wms_proxy import siur_wms_endpoints_are_equivalent

MAX_LICENSE_REVIEW_BYTES = 256 * 1024
LICENSE_REVIEW_SCHEMA = "siur-license-review-v1"
ATTESTATION_SCHEMA = "siur-delivery-attestation-v2"
LICENSE_REVIEW_KEYS = {
    "schema_version",
    "provider_key",
    "service_key",
    "decision",
    "reviewer",
    "reviewed_at",
    "supersedes_review_sha256",
    "license_name",
    "license_url",
    "license_terms",
    "allow_proxy",
    "allow_cache",
}


class LicenseReviewError(ValueError):
    """The human review document is malformed or internally contradictory."""


class DeliveryEvidenceError(ValueError):
    """The reviewed delivery-evidence plan cannot be applied."""


@dataclass(frozen=True)
class LicenseReviewEvidence:
    raw_document: bytes
    evidence_sha256: str
    review_sha256: str
    provider_key: str
    service_key: str
    decision: str
    reviewer: str
    reviewed_at: datetime
    supersedes_review_sha256: str | None
    license_name: str
    license_url: str | None
    license_terms: str
    allow_proxy: bool
    allow_cache: bool

    def normalized(self) -> dict[str, object]:
        return {
            "schema_version": LICENSE_REVIEW_SCHEMA,
            "provider_key": self.provider_key,
            "service_key": self.service_key,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "reviewed_at": _utc_isoformat(self.reviewed_at),
            "supersedes_review_sha256": self.supersedes_review_sha256,
            "license_name": self.license_name,
            "license_url": self.license_url,
            "license_terms": self.license_terms,
            "allow_proxy": self.allow_proxy,
            "allow_cache": self.allow_cache,
        }


@dataclass(frozen=True)
class DeliveryEvidencePlan:
    capabilities: WMSCapabilitiesEvidence
    license_review: LicenseReviewEvidence
    service_id: int | None
    catalog_snapshot_id: int | None
    catalog_definition_sha256: str | None
    attestation_kind: str | None
    sequence_number: int | None
    previous_attestation_id: int | None
    previous_attestation_sha256: str | None
    attestation_sha256: str | None
    fatal_issues: tuple[str, ...]
    attestation_issues: tuple[str, ...]
    delivery_issues: tuple[str, ...]
    existing_capabilities_snapshot_id: int | None = None
    existing_license_review_id: int | None = None
    existing_attestation_id: int | None = None

    @property
    def delivery_ready(self) -> bool:
        return (
            self.attestable
            and self.attestation_kind == "delivery"
            and not self.delivery_issues
        )

    @property
    def attestable(self) -> bool:
        if self.fatal_issues:
            return False
        if self.attestation_kind == "revocation":
            return True
        return (
            self.attestation_kind == "delivery"
            and not self.attestation_issues
        )

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.plan_identity()).encode("utf-8")
        ).hexdigest()

    def plan_identity(self) -> dict[str, object]:
        return {
            "schema_version": "siur-delivery-import-plan-v2",
            "provider_key": self.license_review.provider_key,
            "service_key": self.license_review.service_key,
            "service_id": self.service_id,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "catalog_definition_sha256": self.catalog_definition_sha256,
            "attestation_kind": self.attestation_kind,
            "sequence_number": self.sequence_number,
            "previous_attestation_id": self.previous_attestation_id,
            "previous_attestation_sha256": (
                self.previous_attestation_sha256
            ),
            "capabilities_raw_sha256": self.capabilities.raw_sha256,
            "capabilities_normalized_sha256": (
                self.capabilities.normalized_sha256
            ),
            "license_evidence_sha256": self.license_review.evidence_sha256,
            "license_review_sha256": self.license_review.review_sha256,
            "attestation_sha256": self.attestation_sha256,
            "fatal_issues": list(self.fatal_issues),
            "attestation_issues": list(self.attestation_issues),
            "delivery_issues": list(self.delivery_issues),
        }


@dataclass(frozen=True)
class AppliedDeliveryEvidence:
    capabilities_snapshot_id: int
    license_review_id: int
    attestation_id: int | None
    attestation_sha256: str | None


def parse_license_review(document: bytes) -> LicenseReviewEvidence:
    if not isinstance(document, bytes):
        raise LicenseReviewError("License review must be supplied as bytes")
    if not 0 < len(document) <= MAX_LICENSE_REVIEW_BYTES:
        raise LicenseReviewError("License review size is outside the allowed range")
    try:
        value = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(item)
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LicenseReviewError(
            "License review is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or set(value) != LICENSE_REVIEW_KEYS:
        raise LicenseReviewError("License review fields do not match the schema")
    if value["schema_version"] != LICENSE_REVIEW_SCHEMA:
        raise LicenseReviewError("License review schema is unsupported")
    provider_key = _bounded_text(value["provider_key"], "provider_key", 64)
    service_key = _bounded_text(value["service_key"], "service_key", 255)
    if provider_key != "siur" or not SOURCE_KEY_RE.fullmatch(service_key):
        raise LicenseReviewError("License review service identity is invalid")
    decision = value["decision"]
    if decision not in {"approved", "restricted", "rejected"}:
        raise LicenseReviewError("License review decision is invalid")
    reviewer = _bounded_text(value["reviewer"], "reviewer", 255)
    reviewed_at = _reviewed_at(value["reviewed_at"])
    supersedes_review_sha256 = value["supersedes_review_sha256"]
    if supersedes_review_sha256 is not None and (
        not isinstance(supersedes_review_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supersedes_review_sha256)
    ):
        raise LicenseReviewError("Superseded review hash is invalid")
    license_name = _bounded_text(value["license_name"], "license_name", 500)
    license_terms = _bounded_text(value["license_terms"], "license_terms", 50_000)
    license_url = _license_url(value["license_url"])
    allow_proxy = value["allow_proxy"]
    allow_cache = value["allow_cache"]
    if not isinstance(allow_proxy, bool) or not isinstance(allow_cache, bool):
        raise LicenseReviewError("License permissions must be booleans")
    if allow_cache and not allow_proxy:
        raise LicenseReviewError("Cache permission requires proxy permission")
    if decision != "approved" and (allow_proxy or allow_cache):
        raise LicenseReviewError(
            "Only an approved review can grant delivery permissions"
        )
    normalized = {
        "schema_version": LICENSE_REVIEW_SCHEMA,
        "provider_key": provider_key,
        "service_key": service_key,
        "decision": decision,
        "reviewer": reviewer,
        "reviewed_at": _utc_isoformat(reviewed_at),
        "supersedes_review_sha256": supersedes_review_sha256,
        "license_name": license_name,
        "license_url": license_url,
        "license_terms": license_terms,
        "allow_proxy": allow_proxy,
        "allow_cache": allow_cache,
    }
    evidence_sha256 = hashlib.sha256(document).hexdigest()
    return LicenseReviewEvidence(
        raw_document=document,
        evidence_sha256=evidence_sha256,
        review_sha256=hashlib.sha256(
            _canonical_json(
                {
                    "evidence_sha256": evidence_sha256,
                    "review": normalized,
                }
            ).encode("utf-8")
        ).hexdigest(),
        provider_key=provider_key,
        service_key=service_key,
        decision=decision,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        supersedes_review_sha256=supersedes_review_sha256,
        license_name=license_name,
        license_url=license_url,
        license_terms=license_terms,
        allow_proxy=allow_proxy,
        allow_cache=allow_cache,
    )


def build_delivery_evidence_plan(
    db: Session,
    capabilities: WMSCapabilitiesEvidence,
    license_review: LicenseReviewEvidence,
    *,
    lock: bool = False,
) -> DeliveryEvidencePlan:
    fatal: list[str] = []
    attestation_issues: list[str] = []
    delivery: list[str] = []
    service_query = select(ReferenceService).where(
        ReferenceService.provider_key == license_review.provider_key,
        ReferenceService.source_key == license_review.service_key,
    )
    if lock:
        service_query = service_query.with_for_update()
    service = db.scalar(service_query)
    if service is None:
        fatal.append("Reviewed service does not exist in the catalog")

    snapshot_query = select(ReferenceCatalogSnapshot).where(
        ReferenceCatalogSnapshot.provider_key == license_review.provider_key,
        ReferenceCatalogSnapshot.is_current.is_(True),
        ReferenceCatalogSnapshot.status == "applied",
    )
    if lock:
        snapshot_query = snapshot_query.with_for_update()
    snapshot = db.scalar(snapshot_query)
    if snapshot is None:
        fatal.append("Current applied catalog snapshot is unavailable")
    elif (
        canonical_normalized_definition_sha256(
            snapshot.normalized_definition_json
        )
        != snapshot.definition_sha256
    ):
        fatal.append("Current catalog definition hash is invalid")

    existing_capabilities = None
    existing_review = None
    existing_attestation = None
    latest_review = None
    current_attestation = None
    current_capabilities = None
    current_review = None
    if service is not None:
        existing_capabilities = db.scalar(
            select(ReferenceWMSCapabilitiesSnapshot).where(
                ReferenceWMSCapabilitiesSnapshot.provider_key
                == license_review.provider_key,
                ReferenceWMSCapabilitiesSnapshot.service_id == service.id,
                ReferenceWMSCapabilitiesSnapshot.raw_sha256
                == capabilities.raw_sha256,
                ReferenceWMSCapabilitiesSnapshot.normalized_sha256
                == capabilities.normalized_sha256,
            )
        )
        existing_review = db.scalar(
            select(ReferenceLicenseReview).where(
                ReferenceLicenseReview.provider_key
                == license_review.provider_key,
                ReferenceLicenseReview.service_id == service.id,
                ReferenceLicenseReview.evidence_sha256
                == license_review.evidence_sha256,
                ReferenceLicenseReview.review_sha256
                == license_review.review_sha256,
            )
        )
        if (
            existing_capabilities is not None
            and not stored_capabilities_hash_is_valid(existing_capabilities)
        ):
            fatal.append("Stored capabilities evidence is corrupt")
        if (
            existing_review is not None
            and not stored_license_review_hash_is_valid(
                existing_review,
                service_source_key=service.source_key,
            )
        ):
            fatal.append("Stored license review evidence is corrupt")
        latest_review = db.scalar(
            select(ReferenceLicenseReview)
            .where(
                ReferenceLicenseReview.provider_key
                == license_review.provider_key,
                ReferenceLicenseReview.service_id == service.id,
            )
            .order_by(ReferenceLicenseReview.id.desc())
            .limit(1)
        )
        if latest_review is not None and not stored_license_review_hash_is_valid(
            latest_review,
            service_source_key=service.source_key,
        ):
            fatal.append("Current license review evidence is corrupt")

        review_matches_latest = (
            latest_review is not None
            and latest_review.evidence_sha256 == license_review.evidence_sha256
            and latest_review.review_sha256 == license_review.review_sha256
        )
        if latest_review is not None and not review_matches_latest:
            if (
                license_review.supersedes_review_sha256
                != latest_review.review_sha256
            ):
                fatal.append(
                    "Replacement license review does not supersede the "
                    "current review"
                )
            if license_review.reviewed_at <= latest_review.reviewed_at:
                fatal.append(
                    "A replacement license review must be later than the "
                    "current review"
                )
        elif latest_review is None and (
            license_review.supersedes_review_sha256 is not None
        ):
            fatal.append("The first license review cannot supersede another review")

        current_attestation = db.scalar(
            select(ReferenceDeliveryAttestation)
            .where(
                ReferenceDeliveryAttestation.provider_key
                == license_review.provider_key,
                ReferenceDeliveryAttestation.service_id == service.id,
            )
            .order_by(ReferenceDeliveryAttestation.sequence_number.desc())
            .limit(1)
        )
        if current_attestation is not None:
            current_capabilities = db.get(
                ReferenceWMSCapabilitiesSnapshot,
                current_attestation.capabilities_snapshot_id,
            )
            current_review = db.get(
                ReferenceLicenseReview,
                current_attestation.license_review_id,
            )
            if (
                current_capabilities is None
                or current_review is None
                or not stored_capabilities_hash_is_valid(
                    current_capabilities
                )
                or not stored_license_review_hash_is_valid(
                    current_review,
                    service_source_key=service.source_key,
                )
                or not attestation_chain_link_is_valid(
                    db,
                    current_attestation,
                    current_capabilities,
                    current_review,
                )
            ):
                fatal.append("Current delivery attestation is corrupt")

    if service is not None and snapshot is not None:
        if service.last_seen_snapshot_id != snapshot.id:
            fatal.append("Service is not bound to the current catalog snapshot")
        if service.upstream_protocol != "wms" or service.status not in {
            "active",
            "degraded",
        }:
            fatal.append("Catalog service is not an active WMS service")
        attestation_issues.extend(
            _attestation_issues(
                service=service,
                capabilities=capabilities,
                license_review=license_review,
            )
        )
        delivery.extend(
            _layer_delivery_issues(
                db,
                service=service,
                snapshot=snapshot,
                capabilities=capabilities,
            )
        )

    fatal_tuple = tuple(sorted(set(fatal)))
    attestation_tuple = tuple(sorted(set(attestation_issues)))
    delivery_tuple = tuple(sorted(set(delivery)))
    attestation_kind = (
        "delivery"
        if license_review.decision == "approved" and license_review.allow_proxy
        else "revocation"
    )
    review_matches_current_attestation = (
        current_review is not None
        and current_review.evidence_sha256 == license_review.evidence_sha256
        and current_review.review_sha256 == license_review.review_sha256
    )
    capabilities_match_current_attestation = (
        current_capabilities is not None
        and current_capabilities.raw_sha256 == capabilities.raw_sha256
        and current_capabilities.normalized_sha256
        == capabilities.normalized_sha256
    )
    if (
        attestation_kind == "delivery"
        and attestation_tuple
        and (
            not review_matches_current_attestation
            or (
                current_attestation is not None
                and current_attestation.attestation_kind == "revocation"
                and capabilities_match_current_attestation
            )
        )
    ):
        # A superseding human review is authoritative even when its paired
        # technical snapshot cannot authorize delivery. Record a fail-closed
        # chain head so an older review (and its cache permission) cannot remain
        # effective. Reusing the same rejected pairing remains idempotent.
        attestation_kind = "revocation"
    sequence_number = (
        current_attestation.sequence_number + 1
        if current_attestation is not None
        else 1
    )
    previous_attestation_id = (
        current_attestation.id if current_attestation is not None else None
    )
    previous_attestation_sha256 = (
        current_attestation.attestation_sha256
        if current_attestation is not None
        else None
    )
    attestation_sha256 = None
    if (
        service is not None
        and snapshot is not None
        and not fatal_tuple
        and (
            attestation_kind == "revocation"
            or not attestation_tuple
        )
    ):
        if (
            current_attestation is not None
            and current_capabilities is not None
            and current_review is not None
            and current_attestation.catalog_snapshot_id == snapshot.id
            and current_attestation.catalog_definition_sha256
            == snapshot.definition_sha256
            and current_attestation.attestation_kind == attestation_kind
            and current_capabilities.raw_sha256 == capabilities.raw_sha256
            and current_capabilities.normalized_sha256
            == capabilities.normalized_sha256
            and current_review.evidence_sha256
            == license_review.evidence_sha256
            and current_review.review_sha256 == license_review.review_sha256
        ):
            existing_attestation = current_attestation
            sequence_number = current_attestation.sequence_number
            previous_attestation_id = (
                current_attestation.previous_attestation_id
            )
            previous_attestation_sha256 = (
                current_attestation.previous_attestation_sha256
            )
            attestation_sha256 = current_attestation.attestation_sha256
        else:
            attestation_sha256 = canonical_attestation_sha256(
                provider_key=license_review.provider_key,
                service_id=service.id,
                catalog_snapshot_id=snapshot.id,
                catalog_definition_sha256=snapshot.definition_sha256,
                capabilities_raw_sha256=capabilities.raw_sha256,
                capabilities_normalized_sha256=(
                    capabilities.normalized_sha256
                ),
                license_evidence_sha256=license_review.evidence_sha256,
                license_review_sha256=license_review.review_sha256,
                attestation_kind=attestation_kind,
                sequence_number=sequence_number,
                previous_attestation_sha256=(
                    previous_attestation_sha256
                ),
            )
    return DeliveryEvidencePlan(
        capabilities=capabilities,
        license_review=license_review,
        service_id=service.id if service is not None else None,
        catalog_snapshot_id=snapshot.id if snapshot is not None else None,
        catalog_definition_sha256=(
            snapshot.definition_sha256 if snapshot is not None else None
        ),
        attestation_kind=attestation_kind,
        sequence_number=sequence_number,
        previous_attestation_id=previous_attestation_id,
        previous_attestation_sha256=previous_attestation_sha256,
        attestation_sha256=attestation_sha256,
        fatal_issues=fatal_tuple,
        attestation_issues=attestation_tuple,
        delivery_issues=delivery_tuple,
        existing_capabilities_snapshot_id=(
            existing_capabilities.id
            if existing_capabilities is not None
            else None
        ),
        existing_license_review_id=(
            existing_review.id if existing_review is not None else None
        ),
        existing_attestation_id=(
            existing_attestation.id if existing_attestation is not None else None
        ),
    )


def apply_delivery_evidence(
    db: Session,
    capabilities: WMSCapabilitiesEvidence,
    license_review: LicenseReviewEvidence,
    *,
    expected_plan_sha256: str,
) -> tuple[AppliedDeliveryEvidence, DeliveryEvidencePlan]:
    try:
        plan = build_delivery_evidence_plan(
            db,
            capabilities,
            license_review,
            lock=True,
        )
        if plan.fatal_issues:
            raise DeliveryEvidenceError("; ".join(plan.fatal_issues))
        if plan.plan_sha256 != expected_plan_sha256:
            raise DeliveryEvidenceError(
                "Delivery evidence state changed after the reviewed dry-run"
            )
        assert plan.service_id is not None
        assert plan.catalog_snapshot_id is not None
        assert plan.catalog_definition_sha256 is not None

        capabilities_row = (
            db.get(
                ReferenceWMSCapabilitiesSnapshot,
                plan.existing_capabilities_snapshot_id,
            )
            if plan.existing_capabilities_snapshot_id is not None
            else None
        )
        if capabilities_row is None:
            capabilities_row = ReferenceWMSCapabilitiesSnapshot(
                provider_key=license_review.provider_key,
                service_id=plan.service_id,
                raw_xml=capabilities.raw_xml,
                raw_size_bytes=len(capabilities.raw_xml),
                raw_sha256=capabilities.raw_sha256,
                normalized_sha256=capabilities.normalized_sha256,
                normalization_version=capabilities.normalization_version,
                wms_version=capabilities.version,
                get_map_endpoint=capabilities.get_map_endpoint,
                get_legend_endpoint=capabilities.get_legend_endpoint,
                get_feature_info_endpoint=(
                    capabilities.get_feature_info_endpoint
                ),
                get_map_formats_json=list(capabilities.get_map_formats),
                get_legend_formats_json=list(
                    capabilities.get_legend_formats
                ),
                get_feature_info_formats_json=list(
                    capabilities.get_feature_info_formats
                ),
                layer_manifest_json=capabilities.layer_manifest,
            )
            db.add(capabilities_row)
            db.flush()

        review_row = (
            db.get(ReferenceLicenseReview, plan.existing_license_review_id)
            if plan.existing_license_review_id is not None
            else None
        )
        if review_row is None:
            review_row = ReferenceLicenseReview(
                provider_key=license_review.provider_key,
                service_id=plan.service_id,
                reviewed_document=license_review.raw_document,
                document_size_bytes=len(license_review.raw_document),
                evidence_sha256=license_review.evidence_sha256,
                review_sha256=license_review.review_sha256,
                supersedes_review_sha256=(
                    license_review.supersedes_review_sha256
                ),
                decision=license_review.decision,
                reviewer=license_review.reviewer,
                reviewed_at=license_review.reviewed_at,
                license_name=license_review.license_name,
                license_url=license_review.license_url,
                license_terms=license_review.license_terms,
                allow_proxy=license_review.allow_proxy,
                allow_cache=license_review.allow_cache,
            )
            db.add(review_row)
            db.flush()

        attestation_row = None
        if plan.attestable:
            attestation_row = (
                db.get(
                    ReferenceDeliveryAttestation,
                    plan.existing_attestation_id,
                )
                if plan.existing_attestation_id is not None
                else None
            )
            if attestation_row is None:
                assert plan.attestation_sha256 is not None
                assert plan.attestation_kind is not None
                assert plan.sequence_number is not None
                attestation_row = ReferenceDeliveryAttestation(
                    provider_key=license_review.provider_key,
                    service_id=plan.service_id,
                    catalog_snapshot_id=plan.catalog_snapshot_id,
                    catalog_definition_sha256=(
                        plan.catalog_definition_sha256
                    ),
                    capabilities_snapshot_id=capabilities_row.id,
                    license_review_id=review_row.id,
                    attestation_kind=plan.attestation_kind,
                    sequence_number=plan.sequence_number,
                    previous_attestation_id=plan.previous_attestation_id,
                    previous_attestation_sha256=(
                        plan.previous_attestation_sha256
                    ),
                    attestation_sha256=plan.attestation_sha256,
                )
                db.add(attestation_row)
                db.flush()
        db.commit()
        return (
            AppliedDeliveryEvidence(
                capabilities_snapshot_id=capabilities_row.id,
                license_review_id=review_row.id,
                attestation_id=(
                    attestation_row.id if attestation_row is not None else None
                ),
                attestation_sha256=(
                    attestation_row.attestation_sha256
                    if attestation_row is not None
                    else None
                ),
            ),
            plan,
        )
    except Exception:
        db.rollback()
        raise


def canonical_attestation_sha256(
    *,
    provider_key: str,
    service_id: int,
    catalog_snapshot_id: int,
    catalog_definition_sha256: str,
    capabilities_raw_sha256: str,
    capabilities_normalized_sha256: str,
    license_evidence_sha256: str,
    license_review_sha256: str,
    attestation_kind: str,
    sequence_number: int,
    previous_attestation_sha256: str | None,
) -> str:
    payload = {
        "schema_version": ATTESTATION_SCHEMA,
        "provider_key": provider_key,
        "service_id": service_id,
        "catalog_snapshot_id": catalog_snapshot_id,
        "catalog_definition_sha256": catalog_definition_sha256,
        "capabilities_raw_sha256": capabilities_raw_sha256,
        "capabilities_normalized_sha256": capabilities_normalized_sha256,
        "license_evidence_sha256": license_evidence_sha256,
        "license_review_sha256": license_review_sha256,
        "attestation_kind": attestation_kind,
        "sequence_number": sequence_number,
        "previous_attestation_sha256": previous_attestation_sha256,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def attestation_chain_link_is_valid(
    db: Session,
    attestation: ReferenceDeliveryAttestation,
    capabilities: ReferenceWMSCapabilitiesSnapshot,
    review: ReferenceLicenseReview,
) -> bool:
    if (
        attestation.attestation_kind not in {"delivery", "revocation"}
        or attestation.sequence_number < 1
        or capabilities.id != attestation.capabilities_snapshot_id
        or review.id != attestation.license_review_id
        or capabilities.provider_key != attestation.provider_key
        or review.provider_key != attestation.provider_key
        or capabilities.service_id != attestation.service_id
        or review.service_id != attestation.service_id
    ):
        return False
    if attestation.sequence_number == 1:
        if (
            attestation.previous_attestation_id is not None
            or attestation.previous_attestation_sha256 is not None
        ):
            return False
    else:
        if (
            attestation.previous_attestation_id is None
            or attestation.previous_attestation_sha256 is None
        ):
            return False
        previous = db.get(
            ReferenceDeliveryAttestation,
            attestation.previous_attestation_id,
        )
        if (
            previous is None
            or previous.provider_key != attestation.provider_key
            or previous.service_id != attestation.service_id
            or previous.sequence_number != attestation.sequence_number - 1
            or previous.attestation_sha256
            != attestation.previous_attestation_sha256
        ):
            return False
    return attestation.attestation_sha256 == canonical_attestation_sha256(
        provider_key=attestation.provider_key,
        service_id=attestation.service_id,
        catalog_snapshot_id=attestation.catalog_snapshot_id,
        catalog_definition_sha256=attestation.catalog_definition_sha256,
        capabilities_raw_sha256=capabilities.raw_sha256,
        capabilities_normalized_sha256=capabilities.normalized_sha256,
        license_evidence_sha256=review.evidence_sha256,
        license_review_sha256=review.review_sha256,
        attestation_kind=attestation.attestation_kind,
        sequence_number=attestation.sequence_number,
        previous_attestation_sha256=(
            attestation.previous_attestation_sha256
        ),
    )


def stored_capabilities_normalized(
    snapshot: ReferenceWMSCapabilitiesSnapshot,
) -> dict[str, object]:
    return {
        "normalization_version": snapshot.normalization_version,
        "wms_version": snapshot.wms_version,
        "get_map_endpoint": snapshot.get_map_endpoint,
        "get_legend_endpoint": snapshot.get_legend_endpoint,
        "get_feature_info_endpoint": snapshot.get_feature_info_endpoint,
        "get_map_formats": snapshot.get_map_formats_json,
        "get_legend_formats": snapshot.get_legend_formats_json,
        "get_feature_info_formats": snapshot.get_feature_info_formats_json,
        "layers": snapshot.layer_manifest_json,
    }


def stored_capabilities_hash_is_valid(
    snapshot: ReferenceWMSCapabilitiesSnapshot,
) -> bool:
    if snapshot.normalization_version != NORMALIZATION_VERSION:
        return False
    try:
        parsed = parse_wms_capabilities(snapshot.raw_xml)
        stored = stored_capabilities_normalized(snapshot)
        return (
            len(snapshot.raw_xml) == snapshot.raw_size_bytes
            and parsed.raw_sha256 == snapshot.raw_sha256
            and parsed.normalized_sha256 == snapshot.normalized_sha256
            and parsed.normalized() == stored
            and canonical_capabilities_sha256(stored)
            == snapshot.normalized_sha256
        )
    except (AttributeError, TypeError, ValueError, WMSCapabilitiesError):
        return False


def stored_license_review_hash_is_valid(
    review: ReferenceLicenseReview,
    *,
    service_source_key: str,
) -> bool:
    try:
        parsed = parse_license_review(review.reviewed_document)
        normalized = {
            "schema_version": LICENSE_REVIEW_SCHEMA,
            "provider_key": review.provider_key,
            "service_key": service_source_key,
            "decision": review.decision,
            "reviewer": review.reviewer,
            "reviewed_at": _utc_isoformat(review.reviewed_at),
            "supersedes_review_sha256": (
                review.supersedes_review_sha256
            ),
            "license_name": review.license_name,
            "license_url": review.license_url,
            "license_terms": review.license_terms,
            "allow_proxy": review.allow_proxy,
            "allow_cache": review.allow_cache,
        }
        return (
            len(review.reviewed_document) == review.document_size_bytes
            and parsed.evidence_sha256 == review.evidence_sha256
            and parsed.review_sha256 == review.review_sha256
            and parsed.normalized() == normalized
            and parsed.service_key == service_source_key
            and hashlib.sha256(
                _canonical_json(
                    {
                        "evidence_sha256": review.evidence_sha256,
                        "review": normalized,
                    }
                ).encode("utf-8")
            ).hexdigest()
            == review.review_sha256
        )
    except (AttributeError, LicenseReviewError, TypeError, ValueError):
        return False


def _attestation_issues(
    *,
    service: ReferenceService,
    capabilities: WMSCapabilitiesEvidence,
    license_review: LicenseReviewEvidence,
) -> list[str]:
    issues: list[str] = []
    if not siur_wms_endpoints_are_equivalent(
        service.base_url,
        capabilities.get_map_endpoint,
    ):
        issues.append("GetMap endpoint does not match the current catalog")
    if service.version is not None and service.version != capabilities.version:
        issues.append("WMS version does not match the current catalog")
    if capabilities.version not in {"1.1.1", "1.3.0"}:
        issues.append("WMS version is not supported for delivery")
    if license_review.decision != "approved":
        issues.append("Human license review is not approved")
    if not license_review.allow_proxy:
        issues.append("Human license review does not permit proxy delivery")
    return issues


def _layer_delivery_issues(
    db: Session,
    *,
    service: ReferenceService,
    snapshot: ReferenceCatalogSnapshot,
    capabilities: WMSCapabilitiesEvidence,
) -> list[str]:
    issues: list[str] = []
    if "image/png" not in capabilities.get_map_formats:
        issues.append("GetMap does not support PNG")
    if capabilities.get_legend_endpoint is None:
        issues.append("GetLegendGraphic endpoint is absent")
    if "image/png" not in capabilities.get_legend_formats:
        issues.append("GetLegendGraphic does not support PNG")

    manifest = {layer.name: layer for layer in capabilities.layers}
    layers = list(
        db.scalars(
            select(ReferenceLayer).where(
                ReferenceLayer.provider_key == service.provider_key,
                ReferenceLayer.service_id == service.id,
                ReferenceLayer.status.in_(("active", "degraded")),
                ReferenceLayer.node_type == "layer",
                ReferenceLayer.renderer == "raster_tile",
                ReferenceLayer.delivery_mode.in_(("proxy", "mirror")),
            )
        )
    )
    if not layers:
        issues.append("Catalog service has no deliverable raster layers")
    for layer in layers:
        label = layer.source_key
        if layer.last_seen_snapshot_id != snapshot.id:
            issues.append(f"Layer is not current: {label}")
            continue
        capability_layer = manifest.get(layer.remote_name or "")
        if capability_layer is None:
            issues.append(f"Remote layer is absent from GetCapabilities: {label}")
            continue
        if "EPSG:3857" not in capability_layer.crs:
            issues.append(f"Layer lacks literal EPSG:3857 support: {label}")
        styles = list(
            db.scalars(
                select(ReferenceLayerStyle).where(
                    ReferenceLayerStyle.provider_key == layer.provider_key,
                    ReferenceLayerStyle.layer_id == layer.id,
                    ReferenceLayerStyle.status.in_(("active", "degraded")),
                )
            )
        )
        for style in styles:
            if style.last_seen_snapshot_id != snapshot.id:
                issues.append(
                    f"Style is not current: {label}|{style.source_key}"
                )
            elif style.remote_name not in capability_layer.styles:
                issues.append(
                    "Style is absent from GetCapabilities: "
                    f"{label}|{style.source_key}"
                )
        if not styles and "" not in capability_layer.styles:
            issues.append(f"Default style is absent from GetCapabilities: {label}")
        if layer.queryable:
            if not capability_layer.queryable:
                issues.append(f"Layer is not queryable in GetCapabilities: {label}")
            if capabilities.get_feature_info_endpoint is None:
                issues.append(f"GetFeatureInfo endpoint is absent: {label}")
            if "application/json" not in capabilities.get_feature_info_formats:
                issues.append(f"JSON FeatureInfo is absent: {label}")
    return issues


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LicenseReviewError(f"License review {label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise LicenseReviewError(f"License review {label} is invalid")
    return normalized


def _reviewed_at(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise LicenseReviewError("License review timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LicenseReviewError("License review timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LicenseReviewError("License review timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _license_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= 2000:
        raise LicenseReviewError("License URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LicenseReviewError("License URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise LicenseReviewError("License URL is invalid")
    return value


def _utc_isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
