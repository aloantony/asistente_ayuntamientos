"""Hash-bound observations for ten exact IDECyL WFS snapshot sources.

This module is deliberately declarative.  It validates a committed audit of
the WFS identities and projects a bounded snapshot recipe, but it does not
create acquisition candidates, perform network requests, or grant permission
to download, retain, or serve data licensed under IGCYL-NC.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import hmac
from importlib.resources import files
import json
import re
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit


MANIFEST_SCHEMA = "siur-idecyl-wfs-snapshot-observations/v1"
EVIDENCE_SCHEMA = "siur-idecyl-wfs-snapshot-evidence/v1"
PROJECTION_SCHEMA = "siur-idecyl-wfs-snapshot-projection/v1"
MANIFEST_RESOURCE = (
    "evidence/idecyl_wfs_snapshot/manifest-v1.json"
)
MANIFEST_SHA256 = (
    "bba50fe19c72ae09018765ed29b205f78"
    "9665b6e486d42a5d7eb568dc4a67ac2"
)
CLASSIFICATION_MANIFEST_SCHEMA = (
    "siur-idecyl-local-service-classification/v2"
)
CLASSIFICATION_MANIFEST_RESOURCE = (
    "evidence/idecyl_exact/decision-manifest-v2.json"
)
CLASSIFICATION_MANIFEST_SHA256 = (
    "3b642924170117914ef23b10da606aad"
    "c7476674d60fee39eace25457642c948"
)
LEGACY_MANIFEST_RESOURCE = "evidence/idecyl_exact/manifest-v1.json"
LEGACY_MANIFEST_SHA256 = (
    "5b60099dfbe8000e286b1da30491e846"
    "73a8691baea64c32aa4f284af56cbf07"
)
MAX_MANIFEST_BYTES = 256 * 1024
MAX_CLASSIFICATION_MANIFEST_BYTES = 256 * 1024
MAX_LEGACY_MANIFEST_BYTES = 256 * 1024

_AUTHORIZATION_EFFECT = (
    "none_without_persisted_human_mirror_review"
)
_CAPTURE_STARTED_AT = "2026-07-27T06:05:03Z"
_CAPTURE_COMPLETED_AT = "2026-07-27T06:16:49Z"
_LICENSE_NAME = "LICENCIA-IGCYL-NC"
_LICENSE_URL = (
    "https://ftp.itacyl.es/cartografia/LICENCIA-IGCYL-NC-2012.pdf"
)
_WFS_VERSION = "2.0.0"
_CRS = "EPSG:25830"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SOURCE_KEY_RE = re.compile(
    r"^layer:siur:[0-9a-f]{64}$",
    re.ASCII,
)
_TYPE_NAME_RE = re.compile(
    r"^[a-z0-9][a-z0-9_.-]{0,63}:"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,499}$",
    re.ASCII,
)
_PROPERTY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$",
    re.ASCII,
)
_EXPECTED_CAPABILITIES = {
    "implements_result_paging": True,
    "implements_sorting": True,
    "paging_is_transaction_safe": False,
    "supports_result_type_hits": True,
}
_MISMATCH_METADATA_FIDS = {
    142: "spagobcyltemmonprocomcyl",
    171: "spagobcyltemmontesconrep",
}
_WFS_WORKSPACES = frozenset(
    {
        "energia",
        "entidades",
        "montes",
        "pesca",
        "poblacion",
        "viascomunicacion",
    }
)
_RESTRICTED_WFS_REASON_CODES = [
    "wfs_igcyl_nc_requires_recipient_acceptance",
    "wfs_paging_not_transaction_safe",
]

SnapshotMethod = Literal["single_response", "paged"]
IdentityMode = Literal["properties", "content_multiset"]
IdentityPropertiesRole = Literal[
    "diagnostic_only",
    "paging_sort_key",
    "content_multiset_comparison",
]


class IDECyLWFSSnapshotEvidenceError(RuntimeError):
    """Committed WFS evidence is missing, altered, or inconsistent."""


@dataclass(frozen=True)
class _ExpectedSource:
    endpoint_url: str
    type_name: str
    count_default: int
    observed_feature_count: int
    safe_snapshot_method: SnapshotMethod
    page_size: int | None
    identity_mode: IdentityMode
    identity_properties: tuple[str, ...]
    sort_by: tuple[str, ...]


_EXPECTED_SOURCES = {
    78: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/montes/wfs"
        ),
        type_name="montes:znie_cyl_vvpp_ejes",
        count_default=25_000,
        observed_feature_count=9_657,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="content_multiset",
        identity_properties=(),
        sort_by=(),
    ),
    84: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/viascomunicacion/wfs"
        ),
        type_name="viascomunicacion:carr_cyl_red_vias",
        count_default=50_000,
        observed_feature_count=21_949,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=(),
    ),
    118: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/pesca/wfs"
        ),
        type_name="pesca:pesca_cyl_cangrejo_v",
        count_default=25_000,
        observed_feature_count=2_354,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=(),
    ),
    142: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/montes/wfs"
        ),
        type_name="montes:montes_cyl_propiedad_cyl_vw",
        count_default=25_000,
        observed_feature_count=202,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=(),
    ),
    161: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/energia/wfs"
        ),
        type_name=(
            "energia:energia_cyl_renov_zonas_sens_amb_flora"
        ),
        count_default=25_000,
        observed_feature_count=711,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("cod1x1",),
        sort_by=(),
    ),
    171: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/montes/wfs"
        ),
        type_name="montes:montes_cyl_contratados_jcyl_vw",
        count_default=25_000,
        observed_feature_count=770,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=(),
    ),
    194: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/energia/wfs"
        ),
        type_name=(
            "energia:energia_cyl_renov_zonas_sens_actv_crit"
        ),
        count_default=25_000,
        observed_feature_count=192,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=(),
    ),
    243: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/poblacion/wfs"
        ),
        type_name="poblacion:habitantes_cyl_2024",
        count_default=25_000,
        observed_feature_count=21_450,
        safe_snapshot_method="paged",
        page_size=11_000,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=("fid",),
    ),
    246: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/entidades/wfs"
        ),
        type_name="entidades:limites_cyl_entidad_local_menor",
        count_default=25_000,
        observed_feature_count=2_208,
        safe_snapshot_method="single_response",
        page_size=None,
        identity_mode="properties",
        identity_properties=("c_rel_reg",),
        sort_by=(),
    ),
    281: _ExpectedSource(
        endpoint_url=(
            "https://idecyl.jcyl.es/geoserver/poblacion/wfs"
        ),
        type_name="poblacion:habitantes_cyl_2015",
        count_default=25_000,
        observed_feature_count=21_276,
        safe_snapshot_method="paged",
        page_size=11_000,
        identity_mode="properties",
        identity_properties=("fid",),
        sort_by=("fid",),
    ),
}


@dataclass(frozen=True)
class ReviewedIDECyLWFSSnapshotObservation:
    """One exact, observed WFS identity and its bounded snapshot recipe."""

    audit_layer_id: int
    catalog_identity: dict[str, str]
    endpoint_url: str
    type_name: str
    crs: str
    wfs_version: str
    count_default: int
    observed_feature_count: int
    safe_snapshot_method: SnapshotMethod
    page_size: int | None
    identity_mode: IdentityMode
    identity_properties: tuple[str, ...]
    identity_properties_role: IdentityPropertiesRole
    sort_by: tuple[str, ...]
    observed_response_cap: int | None
    hits_without_count_observed: int | None
    hits_with_count_1_observed: int | None
    required_matching_passes: int
    capabilities: dict[str, bool]
    capabilities_sha256: str
    metadata_binding: dict[str, str]
    observed_started_at: str
    observed_completed_at: str
    license_name: str
    license_url: str
    authorization_effect: str
    authorization_granted: bool
    local_download_authorized: bool
    local_service_authorized: bool


@dataclass(frozen=True)
class _EvidencePackage:
    sources: tuple[ReviewedIDECyLWFSSnapshotObservation, ...]
    sources_by_layer_id: MappingProxyType
    sources_by_identity: MappingProxyType
    identities_by_type_name: MappingProxyType


def reviewed_idecyl_wfs_snapshot(
    *,
    audit_layer_id: int,
    endpoint_url: str,
    type_name: str,
    crs: str,
) -> ReviewedIDECyLWFSSnapshotObservation | None:
    """Return one exact observation and reject known identity drift."""

    package = _committed_evidence_package()
    identity = (audit_layer_id, endpoint_url, type_name, crs)
    source = package.sources_by_identity.get(identity)
    if source is not None:
        return _fresh_source(source)
    if (
        audit_layer_id in package.sources_by_layer_id
        or type_name in package.identities_by_type_name
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "known IDECyL WFS identity changed"
        )
    return None


def idecyl_wfs_snapshot_inventory(
) -> tuple[ReviewedIDECyLWFSSnapshotObservation, ...]:
    """Return fresh projections of all ten committed observations."""

    return tuple(
        _fresh_source(source)
        for source in _committed_evidence_package().sources
    )


def idecyl_wfs_snapshot_evidence(
    source: ReviewedIDECyLWFSSnapshotObservation,
) -> dict[str, Any]:
    """Build exact declarative evidence for a committed observation."""

    if not isinstance(source, ReviewedIDECyLWFSSnapshotObservation):
        raise IDECyLWFSSnapshotEvidenceError(
            "WFS observation has an invalid type"
        )
    committed = _committed_evidence_package().sources_by_layer_id.get(
        source.audit_layer_id
    )
    if committed is None or source != _fresh_source(committed):
        raise IDECyLWFSSnapshotEvidenceError(
            "WFS observation no longer matches committed evidence"
        )
    return _source_evidence(committed)


def idecyl_wfs_snapshot_projection(
    evidence: Any,
) -> dict[str, Any] | None:
    """Validate evidence and return its bounded, non-authorizing recipe.

    A different schema is not this evidence type and returns ``None``.
    Anything claiming this schema must match the committed bytes and values
    exactly or the function raises, so stale evidence cannot silently enable
    a changed WFS identity.
    """

    if not isinstance(evidence, dict):
        return None
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        return None
    layer_id = evidence.get("audit_layer_id")
    if isinstance(layer_id, bool) or not isinstance(layer_id, int):
        raise IDECyLWFSSnapshotEvidenceError(
            "WFS snapshot evidence has no valid layer identity"
        )
    committed = _committed_evidence_package().sources_by_layer_id.get(
        layer_id
    )
    if (
        committed is None
        or evidence != _source_evidence(committed)
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "WFS snapshot evidence no longer matches the manifest"
        )
    projection = {
        "schema": PROJECTION_SCHEMA,
        "manifest_sha256": MANIFEST_SHA256,
        "audit_layer_id": committed.audit_layer_id,
        "endpoint_url": committed.endpoint_url,
        "type_name": committed.type_name,
        "crs": committed.crs,
        "wfs_version": committed.wfs_version,
        "observed_feature_count": committed.observed_feature_count,
        "count_default": committed.count_default,
        "safe_snapshot_method": committed.safe_snapshot_method,
        "page_size": committed.page_size,
        "identity_mode": committed.identity_mode,
        "identity_properties": list(
            committed.identity_properties
        ),
        "identity_properties_role": (
            committed.identity_properties_role
        ),
        "sort_by": list(committed.sort_by),
        "required_matching_passes": (
            committed.required_matching_passes
        ),
        "capabilities": deepcopy(committed.capabilities),
        "capabilities_sha256": committed.capabilities_sha256,
        "classification_manifest_sha256": (
            CLASSIFICATION_MANIFEST_SHA256
        ),
        "metadata_binding": deepcopy(committed.metadata_binding),
        "license_name": committed.license_name,
        "license_url": committed.license_url,
        "authorization_effect": committed.authorization_effect,
        "authorization_granted": committed.authorization_granted,
        "local_download_authorized": (
            committed.local_download_authorized
        ),
        "local_service_authorized": (
            committed.local_service_authorized
        ),
    }
    if committed.observed_response_cap is not None:
        projection.update(
            {
                "observed_response_cap": (
                    committed.observed_response_cap
                ),
                "hits_without_count_observed": (
                    committed.hits_without_count_observed
                ),
                "hits_with_count_1_observed": (
                    committed.hits_with_count_1_observed
                ),
            }
        )
    return projection


@lru_cache(maxsize=1)
def _committed_evidence_package() -> _EvidencePackage:
    return _load_evidence_package(
        _resource_body(MANIFEST_RESOURCE, MAX_MANIFEST_BYTES),
        _resource_body(
            LEGACY_MANIFEST_RESOURCE,
            MAX_LEGACY_MANIFEST_BYTES,
        ),
        _resource_body(
            CLASSIFICATION_MANIFEST_RESOURCE,
            MAX_CLASSIFICATION_MANIFEST_BYTES,
        ),
        expected_manifest_sha256=MANIFEST_SHA256,
    )


def _load_evidence_package(
    manifest_body: bytes,
    legacy_manifest_body: bytes,
    classification_manifest_body: bytes,
    *,
    expected_manifest_sha256: str,
) -> _EvidencePackage:
    """Validate the byte bindings and all semantic observations."""

    _bounded_bytes(manifest_body, MAX_MANIFEST_BYTES)
    _bounded_bytes(legacy_manifest_body, MAX_LEGACY_MANIFEST_BYTES)
    _bounded_bytes(
        classification_manifest_body,
        MAX_CLASSIFICATION_MANIFEST_BYTES,
    )
    _exact_digest(
        manifest_body,
        expected_manifest_sha256,
        "IDECyL WFS observation manifest",
    )
    _exact_digest(
        legacy_manifest_body,
        LEGACY_MANIFEST_SHA256,
        "IDECyL legacy identity inventory",
    )
    _exact_digest(
        classification_manifest_body,
        CLASSIFICATION_MANIFEST_SHA256,
        "IDECyL classification manifest",
    )
    manifest = _json_object(manifest_body)
    if manifest_body != _canonical_json_bytes(manifest):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS observation manifest is not canonical JSON"
        )
    legacy = _json_object(legacy_manifest_body)
    classification_manifest = _json_object(
        classification_manifest_body
    )
    if set(manifest) != {"capture", "schema", "sources"}:
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS observation manifest shape is invalid"
        )
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS observation manifest schema is invalid"
        )
    capture = manifest.get("capture")
    if capture != _expected_capture():
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS observation capture changed"
        )
    legacy_sources = _legacy_catalog_sources(legacy)
    capabilities, classifications = _classification_evidence(
        classification_manifest
    )
    raw_sources = manifest.get("sources")
    if (
        not isinstance(raw_sources, list)
        or len(raw_sources) != len(_EXPECTED_SOURCES)
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS observation inventory is incomplete"
        )

    sources: list[ReviewedIDECyLWFSSnapshotObservation] = []
    by_layer_id: dict[
        int,
        ReviewedIDECyLWFSSnapshotObservation,
    ] = {}
    by_identity: dict[
        tuple[int, str, str, str],
        ReviewedIDECyLWFSSnapshotObservation,
    ] = {}
    by_type_name: dict[
        str,
        tuple[int, str, str, str],
    ] = {}
    observed_ids: list[int] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL WFS observation is invalid"
            )
        layer_id = raw_source.get("audit_layer_id")
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id not in _EXPECTED_SOURCES
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL WFS observation identity is invalid"
            )
        expected = _expected_manifest_source(
            layer_id,
            legacy_sources[layer_id],
            capabilities,
            classifications[layer_id],
        )
        if raw_source != expected:
            raise IDECyLWFSSnapshotEvidenceError(
                f"IDECyL WFS observation {layer_id} changed"
            )
        _validate_source_invariants(raw_source)
        source = _source_from_manifest(raw_source, capture)
        identity = (
            source.audit_layer_id,
            source.endpoint_url,
            source.type_name,
            source.crs,
        )
        if (
            source.audit_layer_id in by_layer_id
            or identity in by_identity
            or source.type_name in by_type_name
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL WFS observation contains a duplicate identity"
            )
        observed_ids.append(source.audit_layer_id)
        sources.append(source)
        by_layer_id[source.audit_layer_id] = source
        by_identity[identity] = source
        by_type_name[source.type_name] = identity
    if observed_ids != sorted(_EXPECTED_SOURCES):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS observations are not in canonical order"
        )
    return _EvidencePackage(
        sources=tuple(sources),
        sources_by_layer_id=MappingProxyType(by_layer_id),
        sources_by_identity=MappingProxyType(by_identity),
        identities_by_type_name=MappingProxyType(by_type_name),
    )


def _expected_capture() -> dict[str, Any]:
    return {
        "authorization_effect": _AUTHORIZATION_EFFECT,
        "authorization_granted": False,
        "classification_manifest": {
            "resource": CLASSIFICATION_MANIFEST_RESOURCE,
            "sha256": CLASSIFICATION_MANIFEST_SHA256,
        },
        "completed_at": _CAPTURE_COMPLETED_AT,
        "feature_count_probe": {
            "count": 1,
            "result_type": "hits",
        },
        "legacy_inventory": {
            "resource": LEGACY_MANIFEST_RESOURCE,
            "sha256": LEGACY_MANIFEST_SHA256,
        },
        "license_name": _LICENSE_NAME,
        "license_url": _LICENSE_URL,
        "local_download_authorized": False,
        "local_service_authorized": False,
        "started_at": _CAPTURE_STARTED_AT,
        "wfs_version": _WFS_VERSION,
    }


def _legacy_catalog_sources(
    legacy: dict[str, Any],
) -> dict[int, dict[str, str]]:
    raw_sources = legacy.get("sources")
    if not isinstance(raw_sources, list):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL legacy identity inventory is invalid"
        )
    result: dict[int, dict[str, str]] = {}
    for item in raw_sources:
        if not isinstance(item, dict):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL legacy source identity is invalid"
            )
        layer_id = item.get("audit_layer_id")
        if layer_id not in _EXPECTED_SOURCES:
            continue
        source_key = item.get("catalog_layer_source_key")
        endpoint = item.get("catalog_endpoint_url")
        remote_name = item.get("catalog_remote_name")
        metadata_fid = item.get("metadata_fid")
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id in result
            or not isinstance(source_key, str)
            or _SOURCE_KEY_RE.fullmatch(source_key) is None
            or not isinstance(endpoint, str)
            or not isinstance(remote_name, str)
            or not remote_name
            or not isinstance(metadata_fid, str)
            or not metadata_fid
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL legacy source identity is invalid"
            )
        result[layer_id] = {
            "catalog_endpoint_url": endpoint,
            "catalog_layer_source_key": source_key,
            "catalog_remote_name": remote_name,
            "metadata_fid": metadata_fid,
        }
    if set(result) != set(_EXPECTED_SOURCES):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL legacy WFS identities are incomplete"
        )
    return result


def _classification_evidence(
    manifest: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[int, dict[str, Any]],
]:
    if (
        set(manifest)
        != {
            "schema",
            "capture",
            "technical_classifications",
            "candidate_source",
            "restricted_https_distributions",
        }
        or manifest.get("schema") != CLASSIFICATION_MANIFEST_SCHEMA
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL classification manifest shape is invalid"
        )
    capture = manifest.get("capture")
    raw_capabilities = (
        capture.get("wfs_capabilities")
        if isinstance(capture, dict)
        else None
    )
    if (
        not isinstance(raw_capabilities, list)
        or len(raw_capabilities) != len(_WFS_WORKSPACES)
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS capability bindings are incomplete"
        )
    capabilities: dict[str, dict[str, Any]] = {}
    for item in raw_capabilities:
        if not isinstance(item, dict) or set(item) != {
            "workspace",
            "url",
            "sha256",
            "access_constraints",
            "paging_is_transaction_safe",
        }:
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL WFS capability binding is invalid"
            )
        workspace = item.get("workspace")
        expected_url = (
            f"https://idecyl.jcyl.es/geoserver/{workspace}/wfs"
            "?service=WFS&request=GetCapabilities&version=2.0.0"
        )
        digest = item.get("sha256")
        if (
            not isinstance(workspace, str)
            or workspace not in _WFS_WORKSPACES
            or workspace in capabilities
            or item.get("url") != expected_url
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or item.get("access_constraints")
            != "www.jcyl.es/licencia-IGCYL-NC"
            or item.get("paging_is_transaction_safe") is not False
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL WFS capability binding changed"
            )
        capabilities[workspace] = item
    if list(capabilities) != sorted(_WFS_WORKSPACES):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS capability bindings are not canonical"
        )

    raw_classifications = manifest.get(
        "technical_classifications"
    )
    if not isinstance(raw_classifications, list):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL technical classifications are invalid"
        )
    classifications: dict[int, dict[str, Any]] = {}
    for item in raw_classifications:
        if not isinstance(item, dict):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL technical classification is invalid"
            )
        layer_id = item.get("audit_layer_id")
        if layer_id not in _EXPECTED_SOURCES:
            continue
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id in classifications
            or set(item)
            != {
                "audit_layer_id",
                "local_service_status",
                "metadata_binding",
                "published_metadata_fid",
                "reason_codes",
            }
            or item.get("local_service_status") != "restricted"
            or item.get("metadata_binding")
            not in {"exact", "mismatch"}
            or not isinstance(
                item.get("published_metadata_fid"),
                str,
            )
            or item.get("reason_codes")
            != _RESTRICTED_WFS_REASON_CODES
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL WFS technical classification changed"
            )
        classifications[layer_id] = item
    if (
        set(classifications) != set(_EXPECTED_SOURCES)
        or {
            layer_id
            for layer_id, item in classifications.items()
            if item["metadata_binding"] == "mismatch"
        }
        != set(_MISMATCH_METADATA_FIDS)
        or any(
            classifications[layer_id]["published_metadata_fid"]
            != published_fid
            for layer_id, published_fid
            in _MISMATCH_METADATA_FIDS.items()
        )
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS technical classifications are incomplete"
        )
    return capabilities, classifications


def _expected_manifest_source(
    layer_id: int,
    legacy: dict[str, str],
    capabilities: dict[str, dict[str, Any]],
    classification: dict[str, Any],
) -> dict[str, Any]:
    expected = _EXPECTED_SOURCES[layer_id]
    expected_metadata_fid = legacy["metadata_fid"]
    published_metadata_fid = classification[
        "published_metadata_fid"
    ]
    if (
        classification["metadata_binding"] == "exact"
        and published_metadata_fid != expected_metadata_fid
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL exact metadata binding changed"
        )
    workspace = expected.type_name.split(":", 1)[0]
    capability = capabilities.get(workspace)
    if capability is None:
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS capability binding is missing"
        )
    if expected.identity_mode == "content_multiset":
        identity_properties_role = "content_multiset_comparison"
    elif expected.safe_snapshot_method == "paged":
        identity_properties_role = "paging_sort_key"
    else:
        identity_properties_role = "diagnostic_only"
    result = {
        "audit_layer_id": layer_id,
        "capabilities": deepcopy(_EXPECTED_CAPABILITIES),
        "capabilities_sha256": capability["sha256"],
        "catalog_identity": {
            "catalog_endpoint_url": legacy["catalog_endpoint_url"],
            "catalog_layer_source_key": (
                legacy["catalog_layer_source_key"]
            ),
            "catalog_remote_name": legacy["catalog_remote_name"],
        },
        "count_default": expected.count_default,
        "crs": _CRS,
        "endpoint_url": expected.endpoint_url,
        "identity_mode": expected.identity_mode,
        "identity_properties": list(
            expected.identity_properties
        ),
        "identity_properties_role": identity_properties_role,
        "metadata_binding": {
            "expected_metadata_fid": expected_metadata_fid,
            "published_metadata_fid": published_metadata_fid,
            "status": (
                classification["metadata_binding"]
            ),
        },
        "observed_feature_count": (
            expected.observed_feature_count
        ),
        "page_size": expected.page_size,
        "required_matching_passes": 2,
        "safe_snapshot_method": expected.safe_snapshot_method,
        "sort_by": list(expected.sort_by),
        "type_name": expected.type_name,
    }
    if expected.safe_snapshot_method == "paged":
        result.update(
            {
                "hits_with_count_1_observed": (
                    expected.observed_feature_count
                ),
                "hits_without_count_observed": 11_000,
                "observed_response_cap": 11_000,
            }
        )
    return result


def _validate_source_invariants(source: dict[str, Any]) -> None:
    endpoint_url = source["endpoint_url"]
    type_name = source["type_name"]
    catalog_identity = source["catalog_identity"]
    try:
        endpoint = urlsplit(endpoint_url)
        catalog_endpoint = urlsplit(
            catalog_identity["catalog_endpoint_url"]
        )
        endpoint_port = endpoint.port
        catalog_port = catalog_endpoint.port
    except (TypeError, ValueError):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS endpoint is invalid"
        ) from None
    path_match = re.fullmatch(
        r"/geoserver/([a-z0-9][a-z0-9_.-]{0,63})/wfs",
        endpoint.path,
        re.ASCII,
    )
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "idecyl.jcyl.es"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint_port not in {None, 443}
        or endpoint.query
        or endpoint.fragment
        or path_match is None
        or catalog_endpoint.scheme != "https"
        or catalog_endpoint.hostname != "idecyl.jcyl.es"
        or catalog_endpoint.username is not None
        or catalog_endpoint.password is not None
        or catalog_port not in {None, 443}
        or catalog_endpoint.query
        or catalog_endpoint.fragment
        or catalog_endpoint.path
        != f"/geoserver/{path_match.group(1)}/wms"
        or not isinstance(type_name, str)
        or _TYPE_NAME_RE.fullmatch(type_name) is None
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS endpoint identity is invalid"
        )
    workspace, remote_name = type_name.split(":", 1)
    if (
        workspace != path_match.group(1)
        or remote_name != catalog_identity["catalog_remote_name"]
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS typeName identity is invalid"
        )
    identity_properties = source["identity_properties"]
    identity_properties_role = source[
        "identity_properties_role"
    ]
    sort_by = source["sort_by"]
    response_cap_keys = {
        "hits_with_count_1_observed",
        "hits_without_count_observed",
        "observed_response_cap",
    }
    if (
        any(
            not isinstance(item, str)
            or _PROPERTY_RE.fullmatch(item) is None
            for item in identity_properties
        )
        or any(
            not isinstance(item, str)
            or _PROPERTY_RE.fullmatch(item) is None
            for item in sort_by
        )
        or len(set(identity_properties)) != len(identity_properties)
        or len(set(sort_by)) != len(sort_by)
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL WFS identity properties are invalid"
        )
    if source["safe_snapshot_method"] == "paged":
        if (
            source["page_size"] != 11_000
            or not sort_by
            or not set(sort_by).issubset(identity_properties)
            or identity_properties_role != "paging_sort_key"
            or source.get("observed_response_cap") != 11_000
            or source.get("hits_without_count_observed")
            != 11_000
            or source.get("hits_with_count_1_observed")
            != source["observed_feature_count"]
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL paged WFS recipe is unsafe"
            )
    elif (
        source["page_size"] is not None
        or sort_by
        or response_cap_keys.intersection(source)
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL single-response WFS recipe is invalid"
        )
    if source["identity_mode"] == "content_multiset":
        if (
            source["audit_layer_id"] != 78
            or identity_properties
            or identity_properties_role
            != "content_multiset_comparison"
        ):
            raise IDECyLWFSSnapshotEvidenceError(
                "IDECyL content multiset identity is invalid"
            )
    elif (
        not identity_properties
        or (
            source["safe_snapshot_method"] == "single_response"
            and identity_properties_role != "diagnostic_only"
        )
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            "IDECyL property identity is empty"
        )


def _source_from_manifest(
    source: dict[str, Any],
    capture: dict[str, Any],
) -> ReviewedIDECyLWFSSnapshotObservation:
    return ReviewedIDECyLWFSSnapshotObservation(
        audit_layer_id=source["audit_layer_id"],
        catalog_identity=deepcopy(source["catalog_identity"]),
        endpoint_url=source["endpoint_url"],
        type_name=source["type_name"],
        crs=source["crs"],
        wfs_version=capture["wfs_version"],
        count_default=source["count_default"],
        observed_feature_count=source["observed_feature_count"],
        safe_snapshot_method=source["safe_snapshot_method"],
        page_size=source["page_size"],
        identity_mode=source["identity_mode"],
        identity_properties=tuple(source["identity_properties"]),
        identity_properties_role=source[
            "identity_properties_role"
        ],
        sort_by=tuple(source["sort_by"]),
        observed_response_cap=source.get(
            "observed_response_cap"
        ),
        hits_without_count_observed=source.get(
            "hits_without_count_observed"
        ),
        hits_with_count_1_observed=source.get(
            "hits_with_count_1_observed"
        ),
        required_matching_passes=source[
            "required_matching_passes"
        ],
        capabilities=deepcopy(source["capabilities"]),
        capabilities_sha256=source["capabilities_sha256"],
        metadata_binding=deepcopy(source["metadata_binding"]),
        observed_started_at=capture["started_at"],
        observed_completed_at=capture["completed_at"],
        license_name=capture["license_name"],
        license_url=capture["license_url"],
        authorization_effect=capture["authorization_effect"],
        authorization_granted=capture["authorization_granted"],
        local_download_authorized=capture[
            "local_download_authorized"
        ],
        local_service_authorized=capture[
            "local_service_authorized"
        ],
    )


def _source_evidence(
    source: ReviewedIDECyLWFSSnapshotObservation,
) -> dict[str, Any]:
    observation = {
        "started_at": source.observed_started_at,
        "completed_at": source.observed_completed_at,
        "observed_feature_count": (
            source.observed_feature_count
        ),
        "count_default": source.count_default,
        "feature_count_probe": {
            "result_type": "hits",
            "count": 1,
        },
        "capabilities": deepcopy(source.capabilities),
        "capabilities_sha256": source.capabilities_sha256,
    }
    if source.observed_response_cap is not None:
        observation.update(
            {
                "observed_response_cap": (
                    source.observed_response_cap
                ),
                "hits_without_count_observed": (
                    source.hits_without_count_observed
                ),
                "hits_with_count_1_observed": (
                    source.hits_with_count_1_observed
                ),
            }
        )
    return {
        "schema": EVIDENCE_SCHEMA,
        "manifest": {
            "resource": MANIFEST_RESOURCE,
            "sha256": MANIFEST_SHA256,
        },
        "classification_manifest": {
            "resource": CLASSIFICATION_MANIFEST_RESOURCE,
            "sha256": CLASSIFICATION_MANIFEST_SHA256,
        },
        "legacy_inventory": {
            "resource": LEGACY_MANIFEST_RESOURCE,
            "sha256": LEGACY_MANIFEST_SHA256,
        },
        "audit_layer_id": source.audit_layer_id,
        "catalog_identity": deepcopy(source.catalog_identity),
        "wfs_identity": {
            "endpoint_url": source.endpoint_url,
            "type_name": source.type_name,
            "crs": source.crs,
            "wfs_version": source.wfs_version,
        },
        "observation": observation,
        "safe_snapshot": {
            "method": source.safe_snapshot_method,
            "page_size": source.page_size,
            "identity_mode": source.identity_mode,
            "identity_properties": list(
                source.identity_properties
            ),
            "identity_properties_role": (
                source.identity_properties_role
            ),
            "sort_by": list(source.sort_by),
            "required_matching_passes": (
                source.required_matching_passes
            ),
        },
        "metadata_binding": deepcopy(source.metadata_binding),
        "license_gate": {
            "license_name": source.license_name,
            "license_url": source.license_url,
            "authorization_effect": source.authorization_effect,
            "authorization_granted": (
                source.authorization_granted
            ),
            "local_download_authorized": (
                source.local_download_authorized
            ),
            "local_service_authorized": (
                source.local_service_authorized
            ),
        },
    }


def _fresh_source(
    source: ReviewedIDECyLWFSSnapshotObservation,
) -> ReviewedIDECyLWFSSnapshotObservation:
    return replace(
        source,
        catalog_identity=deepcopy(source.catalog_identity),
        capabilities=deepcopy(source.capabilities),
        metadata_binding=deepcopy(source.metadata_binding),
    )


def _resource_body(resource_path: str, maximum: int) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    try:
        body = resource.read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError) as exc:
        raise IDECyLWFSSnapshotEvidenceError(
            f"unable to read committed evidence {resource_path}"
        ) from exc
    return _bounded_bytes(body, maximum)


def _bounded_bytes(body: bytes, maximum: int) -> bytes:
    if not isinstance(body, bytes) or not body or len(body) > maximum:
        raise IDECyLWFSSnapshotEvidenceError(
            "committed IDECyL WFS evidence has an invalid size"
        )
    return body


def _exact_digest(body: bytes, expected: str, label: str) -> None:
    if (
        not isinstance(expected, str)
        or _SHA256_RE.fullmatch(expected) is None
        or not hmac.compare_digest(
            hashlib.sha256(body).hexdigest(),
            expected,
        )
    ):
        raise IDECyLWFSSnapshotEvidenceError(
            f"{label} failed its local digest"
        )


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IDECyLWFSSnapshotEvidenceError(
            "committed IDECyL WFS evidence is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise IDECyLWFSSnapshotEvidenceError(
            "committed IDECyL WFS evidence is not a JSON object"
        )
    return value


def _unique_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IDECyLWFSSnapshotEvidenceError(
                "committed IDECyL WFS evidence has duplicate keys"
            )
        result[key] = value
    return result


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IDECyLWFSSnapshotEvidenceError(
            "committed IDECyL WFS evidence is not canonicalizable"
        ) from exc
    return encoded + b"\n"
