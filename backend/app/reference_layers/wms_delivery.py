"""Fail-closed resolution of catalog-bound WMS delivery attestations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reference_layers.catalog import canonical_normalized_definition_sha256
from app.reference_layers.delivery_evidence import (
    attestation_chain_link_is_valid,
    stored_capabilities_hash_is_valid,
    stored_license_review_hash_is_valid,
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
from app.reference_layers.wms_proxy import (
    UnsafeWMSEndpointError,
    validate_siur_wms_endpoint,
)


class WMSDeliveryEvidenceUnavailableError(Exception):
    pass


class WMSDeliveryLicenseDeniedError(Exception):
    pass


class WMSDeliveryCapabilityError(Exception):
    pass


class WMSDeliveryWebMercatorError(Exception):
    pass


class WMSDeliveryIdentifyError(Exception):
    pass


@dataclass(frozen=True)
class AttestedWMSDelivery:
    attestation_sha256: str
    endpoint_url: str
    version: str
    remote_name: str
    style_name: str
    allow_cache: bool


@dataclass(frozen=True)
class LayerDeliveryAvailability:
    delivery_available: bool
    legend_available: bool
    identify_available: bool
    delivery_blocker: str | None
    available_style_ids: tuple[int, ...]
    available_legend_style_ids: tuple[int, ...]


@dataclass(frozen=True)
class _CurrentServiceEvidence:
    review: ReferenceLicenseReview
    capabilities: ReferenceWMSCapabilitiesSnapshot
    attestation: ReferenceDeliveryAttestation
    manifest: dict[str, dict[str, object]]


def resolve_attested_wms_delivery(
    db: Session,
    *,
    layer: ReferenceLayer,
    service: ReferenceService,
    style: ReferenceLayerStyle | None,
    operation: Literal["tile", "legend", "identify"],
) -> AttestedWMSDelivery:
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == layer.provider_key,
            ReferenceCatalogSnapshot.is_current.is_(True),
            ReferenceCatalogSnapshot.status == "applied",
        )
    )
    try:
        catalog_hash_is_valid = (
            snapshot is not None
            and canonical_normalized_definition_sha256(
                snapshot.normalized_definition_json
            )
            == snapshot.definition_sha256
        )
    except (TypeError, ValueError):
        catalog_hash_is_valid = False
    if (
        snapshot is None
        or not catalog_hash_is_valid
        or layer.last_seen_snapshot_id != snapshot.id
        or service.last_seen_snapshot_id != snapshot.id
        or layer.service_id != service.id
        or service.provider_key != layer.provider_key
        or (style is not None and style.last_seen_snapshot_id != snapshot.id)
    ):
        raise WMSDeliveryEvidenceUnavailableError

    evidence = _load_current_service_evidence(
        db,
        snapshot=snapshot,
        service=service,
    )
    review = evidence.review
    capabilities = evidence.capabilities
    attestation = evidence.attestation
    capability_layer = evidence.manifest.get(layer.remote_name or "")
    if capability_layer is None:
        raise WMSDeliveryCapabilityError
    crs = _string_list(capability_layer.get("crs"), maximum=256)
    if "EPSG:3857" not in crs:
        raise WMSDeliveryWebMercatorError
    styles = _string_list(
        capability_layer.get("styles"),
        maximum=256,
        allow_empty=True,
    )
    style_name = style.remote_name if style is not None else ""
    if style_name not in styles:
        raise WMSDeliveryCapabilityError

    endpoint = capabilities.get_map_endpoint
    if operation == "legend":
        endpoint = capabilities.get_legend_endpoint or ""
        if not endpoint:
            raise WMSDeliveryCapabilityError
        _require_safe_endpoint(endpoint)
        if "image/png" not in _string_list(
            capabilities.get_legend_formats_json,
            maximum=100,
        ):
            raise WMSDeliveryCapabilityError
    elif operation == "identify":
        if capability_layer.get("queryable") is not True:
            raise WMSDeliveryIdentifyError
        endpoint = capabilities.get_feature_info_endpoint or ""
        if not endpoint:
            raise WMSDeliveryIdentifyError
        try:
            _require_safe_endpoint(endpoint)
        except WMSDeliveryCapabilityError as exc:
            raise WMSDeliveryIdentifyError from exc
        feature_info_formats = _string_list(
            capabilities.get_feature_info_formats_json,
            maximum=100,
        )
        if "application/json" not in feature_info_formats:
            raise WMSDeliveryIdentifyError
    return AttestedWMSDelivery(
        attestation_sha256=attestation.attestation_sha256,
        endpoint_url=endpoint,
        version=capabilities.wms_version,
        remote_name=layer.remote_name or "",
        style_name=style_name,
        allow_cache=review.allow_cache,
    )


def catalog_delivery_availability(
    db: Session,
    *,
    snapshot: ReferenceCatalogSnapshot,
    services: list[ReferenceService],
    layers: list[ReferenceLayer],
    styles: list[ReferenceLayerStyle],
) -> dict[int, LayerDeliveryAvailability]:
    services_by_id = {service.id: service for service in services}
    styles_by_layer: dict[int, list[ReferenceLayerStyle]] = {}
    for style in styles:
        styles_by_layer.setdefault(style.layer_id, []).append(style)
    evidence_by_service: dict[int, _CurrentServiceEvidence | str] = {}
    result: dict[int, LayerDeliveryAvailability] = {}

    for layer in layers:
        if layer.node_type != "layer":
            result[layer.id] = _unavailable(None)
            continue
        if (
            layer.renderer != "raster_tile"
            or layer.delivery_mode not in {"proxy", "mirror"}
            or layer.status not in {"active", "degraded"}
            or layer.last_seen_snapshot_id != snapshot.id
            or layer.service_id is None
        ):
            result[layer.id] = _unavailable("not_deliverable")
            continue
        service = services_by_id.get(layer.service_id)
        if service is None or service.last_seen_snapshot_id != snapshot.id:
            result[layer.id] = _unavailable("catalog_stale")
            continue
        if service.upstream_protocol != "wms" or service.status not in {
            "active",
            "degraded",
        }:
            result[layer.id] = _unavailable("not_deliverable")
            continue
        cached = evidence_by_service.get(service.id)
        if cached is None:
            try:
                cached = _load_current_service_evidence(
                    db,
                    snapshot=snapshot,
                    service=service,
                )
            except WMSDeliveryLicenseDeniedError:
                cached = "license_not_approved"
            except WMSDeliveryCapabilityError:
                cached = "service_mismatch"
            except WMSDeliveryEvidenceUnavailableError:
                cached = "attestation_missing"
            evidence_by_service[service.id] = cached
        if isinstance(cached, str):
            result[layer.id] = _unavailable(cached)
            continue

        capability_layer = cached.manifest.get(layer.remote_name or "")
        if capability_layer is None:
            result[layer.id] = _unavailable("layer_missing")
            continue
        crs = _string_list(capability_layer.get("crs"), maximum=256)
        if "EPSG:3857" not in crs:
            result[layer.id] = _unavailable("web_mercator_unsupported")
            continue
        capability_styles = set(
            _string_list(
                capability_layer.get("styles"),
                maximum=256,
                allow_empty=True,
            )
        )
        layer_styles = [
            style
            for style in styles_by_layer.get(layer.id, [])
            if style.status in {"active", "degraded"}
            and style.last_seen_snapshot_id == snapshot.id
        ]
        available_style_ids = tuple(
            style.id
            for style in layer_styles
            if style.remote_name in capability_styles
        )
        default_styles = [style for style in layer_styles if style.is_default]
        default_available = (
            default_styles[0].id in available_style_ids
            if default_styles
            else not layer_styles and "" in capability_styles
        )
        legend_operation_available = (
            cached.capabilities.get_legend_endpoint is not None
            and "image/png"
            in _string_list(
                cached.capabilities.get_legend_formats_json,
                maximum=100,
            )
        )
        available_legend_style_ids = (
            available_style_ids if legend_operation_available else ()
        )
        if not default_available:
            result[layer.id] = LayerDeliveryAvailability(
                delivery_available=False,
                legend_available=False,
                identify_available=False,
                delivery_blocker="style_unsupported",
                available_style_ids=available_style_ids,
                available_legend_style_ids=available_legend_style_ids,
            )
            continue
        identify_available = (
            layer.queryable
            and capability_layer.get("queryable") is True
            and cached.capabilities.get_feature_info_endpoint is not None
            and "application/json"
            in _string_list(
                cached.capabilities.get_feature_info_formats_json,
                maximum=100,
            )
        )
        result[layer.id] = LayerDeliveryAvailability(
            delivery_available=True,
            legend_available=legend_operation_available,
            identify_available=identify_available,
            delivery_blocker=None,
            available_style_ids=available_style_ids,
            available_legend_style_ids=available_legend_style_ids,
        )
    return result


def _load_current_service_evidence(
    db: Session,
    *,
    snapshot: ReferenceCatalogSnapshot,
    service: ReferenceService,
) -> _CurrentServiceEvidence:
    if service.upstream_protocol != "wms" or service.status not in {
        "active",
        "degraded",
    }:
        raise WMSDeliveryCapabilityError
    try:
        catalog_hash_is_valid = (
            canonical_normalized_definition_sha256(
                snapshot.normalized_definition_json
            )
            == snapshot.definition_sha256
        )
    except (TypeError, ValueError):
        catalog_hash_is_valid = False
    if not catalog_hash_is_valid:
        raise WMSDeliveryEvidenceUnavailableError

    attestation = db.scalar(
        select(ReferenceDeliveryAttestation)
        .where(
            ReferenceDeliveryAttestation.provider_key == service.provider_key,
            ReferenceDeliveryAttestation.service_id == service.id,
        )
        .order_by(ReferenceDeliveryAttestation.sequence_number.desc())
        .limit(1)
    )
    if (
        attestation is None
        or attestation.catalog_snapshot_id != snapshot.id
        or attestation.catalog_definition_sha256
        != snapshot.definition_sha256
    ):
        raise WMSDeliveryEvidenceUnavailableError
    capabilities = db.get(
        ReferenceWMSCapabilitiesSnapshot,
        attestation.capabilities_snapshot_id,
    )
    review = db.get(
        ReferenceLicenseReview,
        attestation.license_review_id,
    )
    if (
        capabilities is None
        or review is None
        or not stored_capabilities_hash_is_valid(capabilities)
        or not stored_license_review_hash_is_valid(
            review,
            service_source_key=service.source_key,
        )
        or not attestation_chain_link_is_valid(
            db,
            attestation,
            capabilities,
            review,
        )
    ):
        raise WMSDeliveryEvidenceUnavailableError
    if (
        attestation.attestation_kind == "revocation"
        or review.decision != "approved"
        or not review.allow_proxy
    ):
        raise WMSDeliveryLicenseDeniedError
    if attestation.attestation_kind != "delivery":
        raise WMSDeliveryEvidenceUnavailableError
    if (
        service.base_url != capabilities.get_map_endpoint
        or service.version != capabilities.wms_version
        or capabilities.wms_version not in {"1.1.1", "1.3.0"}
    ):
        raise WMSDeliveryCapabilityError
    _require_safe_endpoint(capabilities.get_map_endpoint)
    if "image/png" not in _string_list(
        capabilities.get_map_formats_json,
        maximum=100,
    ):
        raise WMSDeliveryCapabilityError
    _string_list(
        capabilities.get_legend_formats_json,
        maximum=100,
    )
    _string_list(
        capabilities.get_feature_info_formats_json,
        maximum=100,
    )
    if capabilities.get_legend_endpoint is not None:
        _require_safe_endpoint(capabilities.get_legend_endpoint)
    if capabilities.get_feature_info_endpoint is not None:
        _require_safe_endpoint(capabilities.get_feature_info_endpoint)
    return _CurrentServiceEvidence(
        review=review,
        capabilities=capabilities,
        attestation=attestation,
        manifest=_manifest_index(capabilities.layer_manifest_json),
    )


def _require_safe_endpoint(value: str) -> None:
    try:
        validate_siur_wms_endpoint(value)
    except UnsafeWMSEndpointError as exc:
        raise WMSDeliveryCapabilityError from exc


def _manifest_index(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10_000:
        raise WMSDeliveryEvidenceUnavailableError
    result: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "crs",
            "queryable",
            "styles",
        }:
            raise WMSDeliveryEvidenceUnavailableError
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 500
            or name in result
            or not isinstance(item.get("queryable"), bool)
        ):
            raise WMSDeliveryEvidenceUnavailableError
        _string_list(item.get("crs"), maximum=256)
        _string_list(item.get("styles"), maximum=256, allow_empty=True)
        result[name] = item
    return result


def _unavailable(blocker: str | None) -> LayerDeliveryAvailability:
    return LayerDeliveryAvailability(
        delivery_available=False,
        legend_available=False,
        identify_available=False,
        delivery_blocker=blocker,
        available_style_ids=(),
        available_legend_style_ids=(),
    )


def _string_list(
    value: object,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise WMSDeliveryEvidenceUnavailableError
    if any(
        not isinstance(item, str)
        or len(item) > 500
        or (not item and not allow_empty)
        for item in value
    ):
        raise WMSDeliveryEvidenceUnavailableError
    if len(value) != len(set(value)):
        raise WMSDeliveryEvidenceUnavailableError
    return tuple(value)
