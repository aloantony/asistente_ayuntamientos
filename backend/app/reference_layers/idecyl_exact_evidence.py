"""Hash-bound, fail-closed IDECyL local-service classifications.

The original v1 audit recorded possible acquisition routes.  It did not prove
that those routes could be retained and served locally.  This module treats
that file only as an immutable catalog identity inventory and applies the
superseding v3 technical classification:

* 12 exact identities remain restricted, including two SIGPAC identities with a
  deterministic official HTTPS distribution that still require IGCYL-NC
  review; and
* 19 exact identities may produce technical download candidates, while every
  download and local service remains subject to separately persisted human
  mirror authorization.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import re
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from app.reference_layers.reviewed_archive_integrity import (
    ReviewedArchiveIntegrityError,
    configured_reviewed_archive_integrity,
)
from app.reference_layers.source_content_parity import (
    PARITY_SPEC_SCHEMA,
    SourceContentParityError,
    configured_parity_spec,
)


EVIDENCE_SCHEMA = "siur-idecyl-local-service-evidence/v3"
MANIFEST_SCHEMA = "siur-idecyl-local-service-classification/v3"
LEGACY_MANIFEST_SCHEMA = "siur-idecyl-exact-evidence-manifest/v1"
MANIFEST_RESOURCE = "evidence/idecyl_exact/decision-manifest-v3.json"
PREVIOUS_MANIFEST_RESOURCE = (
    "evidence/idecyl_exact/decision-manifest-v2.json"
)
LEGACY_MANIFEST_RESOURCE = "evidence/idecyl_exact/manifest-v1.json"
RECORDS_RESOURCE = "evidence/idecyl_exact/records-20260727.xml"
MANIFEST_SHA256 = (
    "201deb9372cc1201bf1645ec8249cdf9"
    "fdac69784915d71492a0db7b36a01eb5"
)
PREVIOUS_MANIFEST_SHA256 = (
    "3b642924170117914ef23b10da606aad"
    "c7476674d60fee39eace25457642c948"
)
LEGACY_MANIFEST_SHA256 = (
    "5b60099dfbe8000e286b1da30491e846"
    "73a8691baea64c32aa4f284af56cbf07"
)
RECORDS_SHA256 = (
    "9e7eb47a169eb22303267378cd762357a"
    "be0c97137267e37e6e80c4f720e07d5"
)
SOURCE_SNAPSHOT_SHA256 = (
    "ed7b6ea8256313aa1977e6cad89bd069"
    "4d752bf519a55705d5c562ecf5180a56"
)
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PREVIOUS_MANIFEST_BYTES = 256 * 1024
MAX_LEGACY_MANIFEST_BYTES = 256 * 1024
MAX_RECORDS_BYTES = 2 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LAYER_SOURCE_KEY_RE = re.compile(r"^layer:siur:[0-9a-f]{64}$", re.ASCII)
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,500}$", re.ASCII)
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$", re.ASCII)
_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.ASCII)
_CANDIDATE_LAYER_ID = 39
_REVIEWABLE_ARCHIVE_LAYER_IDS = frozenset(
    {
        65,
        66,
        86,
        98,
        105,
        137,
        151,
        197,
        213,
        223,
        225,
        230,
        232,
        234,
        237,
        268,
        279,
        296,
    }
)
_SIGPAC_LAYER_YEARS = {
    123: "2024",
    166: "2022",
}
_SIGPAC_PROVINCE_ARCHIVES = (
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
_SIGPAC_INDEX_SHA256 = {
    123: (
        "28d5a28dee3dcee8cc1d21801d508ed9"
        "59cbe212143664013e8acceb256655b4"
    ),
    166: (
        "6f7d6c29bf2c42660f17a75dcb0efb7"
        "b8321e5de69a60a8c7ebdb69fdeae7ebd"
    ),
}
_SIGPAC_PROVINCE_INDEX_SHA256 = {
    123: (
        "7a0cf5e12369b9bd6f6380220aa43a48"
        "fca910bc01d0c2967f87903455bd0479"
    ),
    166: (
        "4ef68079eb3f3dea123397398015b1f16"
        "afde5ad8bac9c9a1120b0cc0ca47f5b"
    ),
}
_IGCYL_NC_LICENSE_URL = (
    "https://ftp.itacyl.es/cartografia/LICENCIA-IGCYL-NC-2012.pdf"
)
_WFS_LAYER_IDS = frozenset(
    {78, 84, 118, 142, 161, 171, 194, 243, 246, 281}
)
_MISSING_METADATA_LAYER_IDS = frozenset({65, 66})
_MISMATCH_METADATA_FIDS = {
    137: "spagobcyltemenaprorn2zec",
    142: "spagobcyltemmonprocomcyl",
    171: "spagobcyltemmontesconrep",
    213: "spagobcyltemenaproznizhi",
    225: "spagobcyltemespprofauacr",
    230: "spagobcyltemenproznizpp",
    268: "spagobcyltemcadnitagusub",
}
_CANDIDATE_ENDPOINT = (
    "https://opendata.jcyl.es/ficheros/carto/comunicaciones/telecom/"
    "telefonia_movil_cyl_cobertura_carreteras_gpkg.zip"
)
_CANDIDATE_ARCHIVE_SHA256 = (
    "a2ef017ba261e9acf35836a6110b019d"
    "14529c5c8b893b176367be8d4bd2d80f"
)
_CANDIDATE_PARITY_SPEC_SHA256 = (
    "fb86dd729e3f679cc8899bf47f340f35"
    "ced01ba6dea82daa6d12cb57ea6f56e8"
)
_AUTHORIZATION_EFFECT = "none_without_persisted_human_mirror_review"
_IGCYL_NC_LICENSE_SHA256_BY_CRC32 = {
    "39a70ae3": (
        "e29e4917c93192afeae57ea66e18ac3"
        "cdcaf0664651d76b1aaa253ba36c1d96d"
    ),
    "f0b4f4e6": (
        "dce2515c6a09b198c7c94aeea62f5f6"
        "08b079dc3e2fba5636488dcb7045260ff"
    ),
    "b660cf14": (
        "5d77e43b5c34d710c0cb68e45ee2f38"
        "a6992ec48f5b6f038853fbcbfd2e06968"
    ),
}
_IGCYL_NC_REVIEW_REQUIREMENTS = [
    (
        "Confirmar que cada destinatario acepta expresamente la licencia "
        "IGCYL-NC antes de acceder al servicio local."
    ),
    (
        "Confirmar que la entrega local muestra de forma visible "
        "«© Junta de Castilla y León»."
    ),
    (
        "Confirmar que el uso es no comercial o aportar la licencia "
        "comercial específica de la Junta de Castilla y León."
    ),
]

LocalServiceStatus = Literal[
    "candidate",
    "restricted",
    "permission_pending",
]


class IDECyLExactEvidenceError(RuntimeError):
    """Committed IDECyL evidence is missing, altered or inconsistent."""


@dataclass(frozen=True)
class ReviewedIDECyLExactSource:
    """One exact catalog identity and its local-service classification."""

    profile: str
    audit_layer_id: int
    catalog_layer_source_key: str
    catalog_endpoint_url: str
    catalog_remote_name: str
    local_service_status: LocalServiceStatus
    reason_codes: tuple[str, ...]
    protocol: str | None
    target_kind: str | None
    endpoint_url: str | None
    remote_name: str | None
    sync_strategy: str | None
    candidate_config: dict[str, Any] | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _EvidencePackage:
    sources: tuple[ReviewedIDECyLExactSource, ...]
    sources_by_identity: MappingProxyType
    identities_by_remote: MappingProxyType


def is_idecyl_geoserver_catalog_endpoint(endpoint_url: str) -> bool:
    """Return whether an already-canonical URL is an IDECyL WMS catalog."""

    try:
        parts = urlsplit(endpoint_url)
    except (TypeError, ValueError):
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == "idecyl.jcyl.es"
        and parts.username is None
        and parts.password is None
        and parts.port in {None, 443}
        and parts.query == ""
        and parts.fragment == ""
        and re.fullmatch(
            r"/geoserver/[A-Za-z0-9_.-]+/(?:wms|ows)/?",
            parts.path,
            re.ASCII,
        )
        is not None
    )


def reviewed_idecyl_exact_source(
    *,
    catalog_layer_source_key: str,
    catalog_endpoint_url: str,
    catalog_remote_name: str,
) -> ReviewedIDECyLExactSource | None:
    """Return a fresh exact classification, rejecting known identity drift."""

    package = _committed_evidence_package()
    identity = (
        catalog_layer_source_key,
        catalog_endpoint_url,
        catalog_remote_name,
    )
    source = package.sources_by_identity.get(identity)
    if source is not None:
        return _fresh_source(source)
    known_identity = package.identities_by_remote.get(
        (catalog_endpoint_url, catalog_remote_name)
    )
    if known_identity is not None:
        raise IDECyLExactEvidenceError(
            "known IDECyL catalog layer source key changed"
        )
    return None


def idecyl_exact_source_inventory() -> tuple[ReviewedIDECyLExactSource, ...]:
    """Return fresh projections of all 31 fail-closed classifications."""

    return tuple(
        _fresh_source(source)
        for source in _committed_evidence_package().sources
    )


@lru_cache(maxsize=1)
def _committed_evidence_package() -> _EvidencePackage:
    return _load_evidence_package(
        _resource_body(MANIFEST_RESOURCE, MAX_MANIFEST_BYTES),
        _resource_body(
            PREVIOUS_MANIFEST_RESOURCE,
            MAX_PREVIOUS_MANIFEST_BYTES,
        ),
        _resource_body(
            LEGACY_MANIFEST_RESOURCE,
            MAX_LEGACY_MANIFEST_BYTES,
        ),
        _resource_body(RECORDS_RESOURCE, MAX_RECORDS_BYTES),
        expected_manifest_sha256=MANIFEST_SHA256,
    )


def _load_evidence_package(
    manifest_body: bytes,
    previous_manifest_body: bytes,
    legacy_manifest_body: bytes,
    records_body: bytes,
    *,
    expected_manifest_sha256: str,
) -> _EvidencePackage:
    """Validate exact bytes and semantic links before exposing a candidate."""

    _bounded_bytes(manifest_body, MAX_MANIFEST_BYTES)
    _bounded_bytes(
        previous_manifest_body,
        MAX_PREVIOUS_MANIFEST_BYTES,
    )
    _bounded_bytes(legacy_manifest_body, MAX_LEGACY_MANIFEST_BYTES)
    _bounded_bytes(records_body, MAX_RECORDS_BYTES)
    _exact_digest(
        manifest_body,
        expected_manifest_sha256,
        "IDECyL classification manifest",
    )
    _exact_digest(
        previous_manifest_body,
        PREVIOUS_MANIFEST_SHA256,
        "IDECyL previous classification manifest",
    )
    _exact_digest(
        legacy_manifest_body,
        LEGACY_MANIFEST_SHA256,
        "IDECyL legacy inventory",
    )
    _exact_digest(
        records_body,
        RECORDS_SHA256,
        "IDECyL metadata records",
    )

    manifest = _json_object(manifest_body)
    legacy = _json_object(legacy_manifest_body)
    if set(manifest) != {
        "schema",
        "capture",
        "technical_classifications",
        "candidate_source",
        "restricted_https_distributions",
        "reviewable_archive_sources",
    } or manifest.get("schema") != MANIFEST_SCHEMA:
        raise IDECyLExactEvidenceError(
            "IDECyL classification manifest shape is invalid"
        )
    legacy_sources, legacy_records = _legacy_inventory(legacy)
    capture = _capture(manifest.get("capture"))
    classifications = _classifications(
        manifest.get("technical_classifications"),
        legacy_sources,
    )
    candidate = _candidate_source(
        manifest.get("candidate_source"),
        legacy_sources,
        legacy_records,
    )
    reviewable_archives = _reviewable_archive_sources(
        manifest.get("reviewable_archive_sources"),
        legacy_sources,
    )
    restricted_distributions = _restricted_https_distributions(
        manifest.get("restricted_https_distributions"),
        legacy_sources,
    )

    sources: list[ReviewedIDECyLExactSource] = []
    by_identity: dict[
        tuple[str, str, str],
        ReviewedIDECyLExactSource,
    ] = {}
    by_remote: dict[tuple[str, str], tuple[str, str, str]] = {}
    for legacy_source in sorted(
        legacy_sources.values(),
        key=lambda item: item["audit_layer_id"],
    ):
        layer_id = legacy_source["audit_layer_id"]
        classification = classifications[layer_id]
        selected = (
            candidate
            if layer_id == _CANDIDATE_LAYER_ID
            else reviewable_archives.get(layer_id)
        )
        restricted_distribution = restricted_distributions.get(layer_id)
        profile = (
            selected["profile"]
            if selected is not None
            else (
                restricted_distribution["profile"]
                if restricted_distribution is not None
                else legacy_source["profile"]
            )
        )
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "profile": profile,
            "audit_layer_id": layer_id,
            "catalog_identity": {
                "catalog_layer_source_key": legacy_source[
                    "catalog_layer_source_key"
                ],
                "catalog_endpoint_url": legacy_source[
                    "catalog_endpoint_url"
                ],
                "catalog_remote_name": legacy_source[
                    "catalog_remote_name"
                ],
            },
            "metadata_binding": {
                "status": classification["metadata_binding"],
                "expected_metadata_fid": legacy_source["metadata_fid"],
                "published_metadata_fid": classification[
                    "published_metadata_fid"
                ],
            },
            "local_service_status": classification[
                "local_service_status"
            ],
            "reason_codes": deepcopy(classification["reason_codes"]),
            "evidence_binding": {
                "classification_manifest_resource": MANIFEST_RESOURCE,
                "classification_manifest_sha256": expected_manifest_sha256,
                "previous_classification_manifest_resource": (
                    PREVIOUS_MANIFEST_RESOURCE
                ),
                "previous_classification_manifest_sha256": (
                    PREVIOUS_MANIFEST_SHA256
                ),
                "legacy_inventory_resource": LEGACY_MANIFEST_RESOURCE,
                "legacy_inventory_sha256": LEGACY_MANIFEST_SHA256,
                "metadata_records_resource": RECORDS_RESOURCE,
                "metadata_records_sha256": RECORDS_SHA256,
                "source_snapshot_sha256": SOURCE_SNAPSHOT_SHA256,
                "capability_capture_sha256": _canonical_json_sha256(
                    {
                        "wms_capabilities": capture[
                            "wms_capabilities"
                        ],
                        "wfs_capabilities": capture[
                            "wfs_capabilities"
                        ],
                    }
                ),
                "wms_capability_snapshot_count": len(
                    capture["wms_capabilities"]
                ),
                "wfs_capability_snapshot_count": len(
                    capture["wfs_capabilities"]
                ),
            },
            "authorization_effect": _AUTHORIZATION_EFFECT,
        }
        if restricted_distribution is not None:
            evidence["official_https_distribution"] = deepcopy(
                restricted_distribution
            )
        candidate_config: dict[str, Any] | None = None
        if selected is not None:
            if layer_id == _CANDIDATE_LAYER_ID:
                evidence["official_metadata"] = deepcopy(
                    selected["official_metadata"]
                )
                evidence["direct_distribution"] = deepcopy(
                    selected["direct_distribution"]
                )
            else:
                evidence["audit_capture"] = deepcopy(
                    selected["audit_capture"]
                )
                evidence["license_evidence"] = deepcopy(
                    selected["license_evidence"]
                )
            evidence["review_requirements"] = deepcopy(
                selected["review_requirements"]
            )
            candidate_config = {}
            for key in (
                "data_format",
                "media_type",
                "archive_member",
                "input_layer",
                "archive_max_uncompressed_bytes",
                "archive_styles",
                "source_content_parity",
                "reviewed_archive_integrity",
            ):
                if key in selected:
                    candidate_config[key] = deepcopy(selected[key])
        source = ReviewedIDECyLExactSource(
            profile=profile,
            audit_layer_id=layer_id,
            catalog_layer_source_key=legacy_source[
                "catalog_layer_source_key"
            ],
            catalog_endpoint_url=legacy_source["catalog_endpoint_url"],
            catalog_remote_name=legacy_source["catalog_remote_name"],
            local_service_status=classification["local_service_status"],
            reason_codes=tuple(classification["reason_codes"]),
            protocol=(
                selected["selected_protocol"]
                if selected is not None
                else None
            ),
            target_kind=(
                selected["selected_target_kind"]
                if selected is not None
                else None
            ),
            endpoint_url=(
                selected["selected_endpoint_url"]
                if selected is not None
                else None
            ),
            remote_name=(
                selected["selected_remote_name"]
                if selected is not None
                else None
            ),
            sync_strategy=(
                selected["selected_sync_strategy"]
                if selected is not None
                else None
            ),
            candidate_config=candidate_config,
            evidence=evidence,
        )
        identity = (
            source.catalog_layer_source_key,
            source.catalog_endpoint_url,
            source.catalog_remote_name,
        )
        remote_identity = (
            source.catalog_endpoint_url,
            source.catalog_remote_name,
        )
        if identity in by_identity or remote_identity in by_remote:
            raise IDECyLExactEvidenceError(
                "IDECyL classification contains a duplicate identity"
            )
        by_identity[identity] = source
        by_remote[remote_identity] = identity
        sources.append(source)
    return _EvidencePackage(
        sources=tuple(sources),
        sources_by_identity=MappingProxyType(by_identity),
        identities_by_remote=MappingProxyType(by_remote),
    )


def _legacy_inventory(
    legacy: dict[str, Any],
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    if (
        set(legacy) != {"schema", "capture", "records", "sources"}
        or legacy.get("schema") != LEGACY_MANIFEST_SCHEMA
        or not isinstance(legacy.get("sources"), list)
        or len(legacy["sources"]) != 31
        or not isinstance(legacy.get("records"), list)
        or len(legacy["records"]) != 30
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL legacy identity inventory is invalid"
        )
    capture = legacy.get("capture")
    if (
        not isinstance(capture, dict)
        or capture.get("source_snapshot_sha256")
        != SOURCE_SNAPSHOT_SHA256
        or capture.get("bundle_sha256") != RECORDS_SHA256
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL legacy capture binding is invalid"
        )
    sources: dict[int, dict[str, Any]] = {}
    identities: set[tuple[str, str, str]] = set()
    for item in legacy["sources"]:
        if not isinstance(item, dict):
            raise IDECyLExactEvidenceError(
                "IDECyL legacy source identity is invalid"
            )
        layer_id = item.get("audit_layer_id")
        profile = item.get("profile")
        source_key = item.get("catalog_layer_source_key")
        endpoint = item.get("catalog_endpoint_url")
        remote = item.get("catalog_remote_name")
        fid = item.get("metadata_fid")
        identity = (source_key, endpoint, remote)
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id < 1
            or layer_id in sources
            or not isinstance(profile, str)
            or _PROFILE_RE.fullmatch(profile) is None
            or not isinstance(source_key, str)
            or _LAYER_SOURCE_KEY_RE.fullmatch(source_key) is None
            or not isinstance(endpoint, str)
            or not is_idecyl_geoserver_catalog_endpoint(endpoint)
            or not isinstance(remote, str)
            or _REMOTE_NAME_RE.fullmatch(remote) is None
            or not isinstance(fid, str)
            or not fid
            or len(fid) > 200
            or identity in identities
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL legacy source identity is invalid"
            )
        identities.add(identity)
        sources[layer_id] = item
    records: dict[str, dict[str, Any]] = {}
    for item in legacy["records"]:
        if not isinstance(item, dict):
            raise IDECyLExactEvidenceError(
                "IDECyL legacy metadata record is invalid"
            )
        fid = item.get("fid")
        digest = item.get("record_raw_sha256")
        if (
            not isinstance(fid, str)
            or not fid
            or fid.casefold() in records
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL legacy metadata record is invalid"
            )
        records[fid.casefold()] = item
    return sources, records


def _capture(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "captured_at",
        "legacy_inventory",
        "metadata_records",
        "supersedes_classification",
        "wms_capabilities",
        "wfs_capabilities",
    }:
        raise IDECyLExactEvidenceError(
            "IDECyL classification capture is invalid"
        )
    if value.get("captured_at") != "2026-07-27T03:15:00Z":
        raise IDECyLExactEvidenceError(
            "IDECyL classification capture time is invalid"
        )
    _resource_binding(
        value["supersedes_classification"],
        resource=PREVIOUS_MANIFEST_RESOURCE,
        digest=PREVIOUS_MANIFEST_SHA256,
        source_snapshot=False,
    )
    _resource_binding(
        value["legacy_inventory"],
        resource=LEGACY_MANIFEST_RESOURCE,
        digest=LEGACY_MANIFEST_SHA256,
        source_snapshot=False,
    )
    _resource_binding(
        value["metadata_records"],
        resource=RECORDS_RESOURCE,
        digest=RECORDS_SHA256,
        source_snapshot=True,
    )
    _capability_bindings(
        value["wms_capabilities"],
        service="wms",
        expected_count=16,
    )
    _capability_bindings(
        value["wfs_capabilities"],
        service="wfs",
        expected_count=6,
    )
    return value


def _resource_binding(
    value: Any,
    *,
    resource: str,
    digest: str,
    source_snapshot: bool,
) -> None:
    expected_keys = {"resource", "sha256"}
    if source_snapshot:
        expected_keys.add("source_snapshot_sha256")
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("resource") != resource
        or value.get("sha256") != digest
        or (
            source_snapshot
            and value.get("source_snapshot_sha256")
            != SOURCE_SNAPSHOT_SHA256
        )
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL classification resource binding is invalid"
        )


def _capability_bindings(
    value: Any,
    *,
    service: Literal["wms", "wfs"],
    expected_count: int,
) -> None:
    if not isinstance(value, list) or len(value) != expected_count:
        raise IDECyLExactEvidenceError(
            "IDECyL capability evidence inventory is invalid"
        )
    workspaces: list[str] = []
    for item in value:
        keys = {"workspace", "url", "sha256"}
        if service == "wfs":
            keys.update(
                {
                    "access_constraints",
                    "paging_is_transaction_safe",
                }
            )
        if not isinstance(item, dict) or set(item) != keys:
            raise IDECyLExactEvidenceError(
                "IDECyL capability evidence is invalid"
            )
        workspace = item.get("workspace")
        expected_url = (
            f"https://idecyl.jcyl.es/geoserver/{workspace}/{service}"
            f"?service={service.upper()}&request=GetCapabilities"
            f"&version={'1.3.0' if service == 'wms' else '2.0.0'}"
        )
        if (
            not isinstance(workspace, str)
            or _WORKSPACE_RE.fullmatch(workspace) is None
            or item.get("url") != expected_url
            or _SHA256_RE.fullmatch(str(item.get("sha256"))) is None
            or (
                service == "wfs"
                and (
                    item.get("access_constraints")
                    != "www.jcyl.es/licencia-IGCYL-NC"
                    or item.get("paging_is_transaction_safe") is not False
                )
            )
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL capability evidence identity is invalid"
            )
        workspaces.append(workspace)
    if workspaces != sorted(set(workspaces)):
        raise IDECyLExactEvidenceError(
            "IDECyL capability evidence is not canonical"
        )


def _classifications(
    value: Any,
    legacy_sources: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 31:
        raise IDECyLExactEvidenceError(
            "IDECyL technical classifications are incomplete"
        )
    result: dict[int, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "audit_layer_id",
            "local_service_status",
            "metadata_binding",
            "published_metadata_fid",
            "reason_codes",
        }:
            raise IDECyLExactEvidenceError(
                "IDECyL technical classification is invalid"
            )
        layer_id = item.get("audit_layer_id")
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id not in legacy_sources
            or layer_id in result
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL technical classification identity is invalid"
            )
        expected_status, expected_reasons = _expected_classification(
            layer_id
        )
        expected_binding, expected_published = _expected_metadata_binding(
            layer_id,
            legacy_sources[layer_id]["metadata_fid"],
        )
        if (
            item.get("local_service_status") != expected_status
            or item.get("metadata_binding") != expected_binding
            or item.get("published_metadata_fid")
            != expected_published
            or item.get("reason_codes") != expected_reasons
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL technical classification changed"
            )
        result[layer_id] = item
    if list(result) != sorted(result) or set(result) != set(legacy_sources):
        raise IDECyLExactEvidenceError(
            "IDECyL technical classifications are not canonical"
        )
    return result


def _expected_classification(
    layer_id: int,
) -> tuple[LocalServiceStatus, list[str]]:
    if layer_id == _CANDIDATE_LAYER_ID:
        return (
            "candidate",
            [
                "archive_without_igcyl_nc_file",
                "external_subscription_lineage_requires_human_review",
                "general_open_data_terms_linked",
            ],
        )
    if layer_id in _REVIEWABLE_ARCHIVE_LAYER_IDS:
        return (
            "candidate",
            [
                "igcyl_nc_recipient_acceptance_requires_review",
                "igcyl_nc_visible_attribution_requires_review",
                "igcyl_nc_commercial_license_required_if_commercial",
                "local_service_requires_persisted_human_review",
            ],
        )
    if layer_id in _SIGPAC_LAYER_YEARS:
        return (
            "restricted",
            [
                "https_directory_igcyl_nc_requires_recipient_acceptance",
                "local_service_requires_persisted_human_review",
            ],
        )
    if layer_id in _WFS_LAYER_IDS:
        return (
            "restricted",
            [
                "wfs_igcyl_nc_requires_recipient_acceptance",
                "wfs_paging_not_transaction_safe",
            ],
        )
    raise IDECyLExactEvidenceError(
        "IDECyL classification has no reviewed technical route"
    )


def _expected_metadata_binding(
    layer_id: int,
    expected_fid: str,
) -> tuple[str, str | None]:
    if layer_id in _MISSING_METADATA_LAYER_IDS:
        return "missing", None
    if layer_id in _MISMATCH_METADATA_FIDS:
        return "mismatch", _MISMATCH_METADATA_FIDS[layer_id]
    return "exact", expected_fid


def _restricted_https_distributions(
    value: Any,
    legacy_sources: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Validate the exact non-executable SIGPAC HTTPS acquisition plans."""

    if not isinstance(value, list) or len(value) != len(_SIGPAC_LAYER_YEARS):
        raise IDECyLExactEvidenceError(
            "IDECyL restricted HTTPS distributions are incomplete"
        )
    expected_keys = {
        "audit_layer_id",
        "profile",
        "verified_on",
        "root_directory_url",
        "province_directory_url",
        "root_index_sha256",
        "province_index_sha256",
        "archive_names",
        "selection_rule",
        "license_name",
        "license_url",
        "authorization_effect",
    }
    result: dict[int, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise IDECyLExactEvidenceError(
                "IDECyL restricted HTTPS distribution shape is invalid"
            )
        layer_id = item.get("audit_layer_id")
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id not in _SIGPAC_LAYER_YEARS
            or layer_id in result
            or layer_id not in legacy_sources
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL restricted HTTPS distribution identity is invalid"
            )
        year = _SIGPAC_LAYER_YEARS[layer_id]
        root_url = (
            f"https://ftp.itacyl.es/cartografia/05_SIGPAC/"
            f"{year}_ETRS89/"
        )
        province_url = (
            root_url + "Parcelario_SIGPAC_CyL_Provincias/"
        )
        expected_profile = (
            f"idecyl-sigpac-{year}-https-provinces-20260727-v2"
        )
        if (
            item.get("profile") != expected_profile
            or item.get("verified_on") != "2026-07-27"
            or item.get("root_directory_url") != root_url
            or item.get("province_directory_url") != province_url
            or item.get("root_index_sha256")
            != _SIGPAC_INDEX_SHA256[layer_id]
            or item.get("province_index_sha256")
            != _SIGPAC_PROVINCE_INDEX_SHA256[layer_id]
            or item.get("archive_names")
            != list(_SIGPAC_PROVINCE_ARCHIVES)
            or item.get("selection_rule")
            != "exact_nine_castilla_y_leon_province_archives_v1"
            or item.get("license_name") != "LICENCIA-IGCYL-NC"
            or item.get("license_url") != _IGCYL_NC_LICENSE_URL
            or item.get("authorization_effect") != _AUTHORIZATION_EFFECT
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL restricted HTTPS distribution changed"
            )
        _https_url(root_url, allow_query=False)
        _https_url(province_url, allow_query=False)
        _https_url(item["license_url"], allow_query=False)
        if any(
            "/" in name
            or "\\" in name
            or not re.fullmatch(r"[A-Z]+\.zip", name, re.ASCII)
            for name in item["archive_names"]
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL restricted HTTPS archive set is invalid"
            )
        result[layer_id] = item
    if list(result) != sorted(_SIGPAC_LAYER_YEARS):
        raise IDECyLExactEvidenceError(
            "IDECyL restricted HTTPS distributions are not canonical"
        )
    return result


def _candidate_source(
    value: Any,
    legacy_sources: dict[int, dict[str, Any]],
    legacy_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    keys = {
        "audit_layer_id",
        "profile",
        "selected_protocol",
        "selected_target_kind",
        "selected_endpoint_url",
        "selected_remote_name",
        "selected_sync_strategy",
        "data_format",
        "media_type",
        "archive_member",
        "input_layer",
        "archive_max_uncompressed_bytes",
        "direct_distribution",
        "official_metadata",
        "archive_styles",
        "source_content_parity",
        "review_requirements",
        "authorization_effect",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise IDECyLExactEvidenceError(
            "IDECyL technical candidate shape is invalid"
        )
    legacy = legacy_sources[_CANDIDATE_LAYER_ID]
    if (
        value.get("audit_layer_id") != _CANDIDATE_LAYER_ID
        or value.get("profile")
        != "idecyl-telefonia-carreteras-gpkgzip-20260727-v2"
        or value.get("selected_protocol") != "download"
        or value.get("selected_target_kind") != "vector"
        or value.get("selected_endpoint_url") != _CANDIDATE_ENDPOINT
        or value.get("selected_remote_name")
        != legacy["catalog_remote_name"]
        or value.get("selected_sync_strategy") != "conditional_get"
        or value.get("data_format") != "geopackage-zip"
        or value.get("media_type") != "application/x-zip-compressed"
        or value.get("archive_member")
        != "telefonia_movil_cyl_cobertura_carreteras.gpkg"
        or value.get("input_layer") != legacy["catalog_remote_name"]
        or value.get("archive_max_uncompressed_bytes") != 64 * 1024 * 1024
        or value.get("authorization_effect") != _AUTHORIZATION_EFFECT
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL technical candidate identity is invalid"
        )
    _https_url(value["selected_endpoint_url"], allow_query=False)
    distribution = value.get("direct_distribution")
    if (
        not isinstance(distribution, dict)
        or set(distribution)
        != {
            "archive_sha256",
            "content_length",
            "entry_count",
            "uncompressed_bytes",
            "last_modified",
            "etag",
            "accept_ranges",
        }
        or distribution.get("archive_sha256")
        != _CANDIDATE_ARCHIVE_SHA256
        or distribution.get("content_length") != 15_128_443
        or distribution.get("entry_count") != 9
        or distribution.get("uncompressed_bytes") != 30_507_569
        or distribution.get("accept_ranges") is not True
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL direct distribution evidence is invalid"
        )
    metadata = value.get("official_metadata")
    record = legacy_records.get(legacy["metadata_fid"].casefold())
    if (
        record is None
        or not isinstance(metadata, dict)
        or set(metadata)
        != {
            "fid",
            "title",
            "record_raw_sha256",
            "terms_url",
            "attribution",
            "reuse_summary",
            "archive_metadata_sha256",
            "archive_readme_sha256",
            "lineage_caveat",
        }
        or metadata.get("fid") != record.get("fid")
        or metadata.get("title") != record.get("title")
        or metadata.get("record_raw_sha256")
        != record.get("record_raw_sha256")
        or record.get("terms_urls") != [metadata.get("terms_url")]
        or metadata.get("attribution") != "© Junta de Castilla y León"
        or "suscripción externa" not in str(metadata.get("lineage_caveat"))
        or "GeoHash" not in str(metadata.get("lineage_caveat"))
        or _SHA256_RE.fullmatch(
            str(metadata.get("archive_metadata_sha256"))
        )
        is None
        or _SHA256_RE.fullmatch(
            str(metadata.get("archive_readme_sha256"))
        )
        is None
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL official metadata evidence is invalid"
        )
    _https_url(metadata["terms_url"], allow_query=False)
    styles = value.get("archive_styles")
    if (
        not isinstance(styles, list)
        or len(styles) != 3
        or [item.get("remote_name") for item in styles]
        != sorted(item.get("remote_name") for item in styles)
        or any(
            not isinstance(item, dict)
            or set(item) != {"remote_name", "archive_member", "sha256"}
            or _REMOTE_NAME_RE.fullmatch(str(item.get("remote_name")))
            is None
            or not str(item.get("archive_member")).endswith(".sld")
            or "/" in str(item.get("archive_member"))
            or "\\" in str(item.get("archive_member"))
            or _SHA256_RE.fullmatch(str(item.get("sha256"))) is None
            for item in styles
        )
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL archive style evidence is invalid"
        )
    try:
        configured = configured_parity_spec(
            {"source_content_parity": value["source_content_parity"]}
        )
    except SourceContentParityError as error:
        raise IDECyLExactEvidenceError(
            "IDECyL source-content parity evidence is invalid"
        ) from error
    if (
        configured is None
        or configured[1] != _CANDIDATE_PARITY_SPEC_SHA256
        or configured[0].get("archive_sha256")
        != _CANDIDATE_ARCHIVE_SHA256
        or value["source_content_parity"].get("schema_version")
        != PARITY_SPEC_SCHEMA
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL source-content parity identity changed"
        )
    requirements = value.get("review_requirements")
    if (
        not isinstance(requirements, list)
        or len(requirements) != 3
        or any(
            not isinstance(item, str) or not 20 <= len(item) <= 500
            for item in requirements
        )
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL human review requirements are invalid"
        )
    if _contains_forbidden_authorization(value):
        raise IDECyLExactEvidenceError(
            "IDECyL evidence fabricates a human authorization"
        )
    return value


def _reviewable_archive_sources(
    value: Any,
    legacy_sources: dict[int, dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Validate the 18 live ZIP routes without treating baseline bytes as fixed."""

    if (
        not isinstance(value, list)
        or len(value) != len(_REVIEWABLE_ARCHIVE_LAYER_IDS)
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL reviewable archive sources are incomplete"
        )
    keys = {
        "audit_layer_id",
        "profile",
        "selected_protocol",
        "selected_target_kind",
        "selected_endpoint_url",
        "selected_remote_name",
        "selected_sync_strategy",
        "data_format",
        "media_type",
        "archive_member",
        "input_layer",
        "archive_max_uncompressed_bytes",
        "reviewed_archive_integrity",
        "audit_capture",
        "license_evidence",
        "review_requirements",
        "authorization_effect",
    }
    result: dict[int, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != keys:
            raise IDECyLExactEvidenceError(
                "IDECyL reviewable archive source shape is invalid"
            )
        layer_id = item.get("audit_layer_id")
        if (
            isinstance(layer_id, bool)
            or not isinstance(layer_id, int)
            or layer_id not in _REVIEWABLE_ARCHIVE_LAYER_IDS
            or layer_id in result
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL reviewable archive identity is invalid"
            )
        legacy = legacy_sources[layer_id]
        proposed = legacy.get("audit_proposed_acquisition")
        profile = item.get("profile")
        member = item.get("archive_member")
        input_layer = item.get("input_layer")
        data_format = item.get("data_format")
        maximum = item.get("archive_max_uncompressed_bytes")
        if (
            not isinstance(proposed, dict)
            or proposed.get("kind") != "download_archive"
            or item.get("selected_endpoint_url") != proposed.get("url")
            or item.get("selected_remote_name")
            != legacy["catalog_remote_name"]
            or item.get("selected_protocol") != "download"
            or item.get("selected_target_kind") != "vector"
            or item.get("selected_sync_strategy") != "conditional_get"
            or item.get("media_type")
            != "application/x-zip-compressed"
            or not isinstance(profile, str)
            or _PROFILE_RE.fullmatch(profile) is None
            or not profile.endswith("-archive-20260727-v3")
            or not isinstance(member, str)
            or not member
            or len(member) > 4_096
            or "/" in member
            or "\\" in member
            or not isinstance(input_layer, str)
            or _REMOTE_NAME_RE.fullmatch(input_layer) is None
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 64 * 1024 * 1024
            <= maximum
            <= 4 * 1024 * 1024 * 1024
            or (
                layer_id == 105
                and (
                    data_format != "shapefile-zip"
                    or not member.casefold().endswith(".shp")
                )
            )
            or (
                layer_id != 105
                and (
                    data_format != "geopackage-zip"
                    or not member.casefold().endswith(".gpkg")
                )
            )
            or item.get("review_requirements")
            != _IGCYL_NC_REVIEW_REQUIREMENTS
            or item.get("authorization_effect") != _AUTHORIZATION_EFFECT
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL reviewable archive source changed"
            )
        _https_url(item["selected_endpoint_url"], allow_query=False)
        try:
            configured = configured_reviewed_archive_integrity(
                {
                    "reviewed_archive_integrity": item[
                        "reviewed_archive_integrity"
                    ]
                }
            )
        except ReviewedArchiveIntegrityError as error:
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed archive integrity is invalid"
            ) from error
        if configured is None:
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed archive integrity is missing"
            )
        semantic = configured[0]
        constraints = semantic["archive_constraints"]
        response_constraints = semantic["response_constraints"]
        required_members = sorted([member, "Licencia-IGCYL.txt"])
        if data_format == "shapefile-zip":
            shapefile_base = member[:-4]
            required_members = sorted(
                [
                    "Licencia-IGCYL.txt",
                    *[
                        f"{shapefile_base}{suffix}"
                        for suffix in (
                            ".dbf",
                            ".prj",
                            ".shp",
                            ".shx",
                        )
                    ],
                ]
            )
        if (
            constraints["max_uncompressed_bytes"] != maximum
            or constraints["required_members"]
            != required_members
            or constraints["license_member"] != "Licencia-IGCYL.txt"
            or constraints["license_sha256_allowlist"]
            != sorted(set(_IGCYL_NC_LICENSE_SHA256_BY_CRC32.values()))
            or response_constraints["content_type"]
            != item["media_type"]
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL reviewed archive bounds changed"
            )
        baseline_entries, baseline_response = _archive_audit_capture(
            item.get("audit_capture"),
        )
        entry_by_name = {
            entry["name"]: entry for entry in baseline_entries
        }
        if (
            member not in entry_by_name
            or "Licencia-IGCYL.txt" not in entry_by_name
            or len(baseline_entries) > constraints["max_entries"]
            or sum(
                entry["uncompressed_bytes"]
                for entry in baseline_entries
            )
            > maximum
            or baseline_response["content_length"]
            > response_constraints["max_content_length"]
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL archive baseline exceeds its live bounds"
            )
        license_evidence = item.get("license_evidence")
        license_entry = entry_by_name["Licencia-IGCYL.txt"]
        if (
            not isinstance(license_evidence, dict)
            or set(license_evidence)
            != {
                "license_name",
                "license_url",
                "archive_member",
                "crc32",
                "uncompressed_bytes",
                "sha256",
                "required_attribution",
                "recipient_acceptance_required",
                "commercial_license_required_if_commercial",
            }
            or license_evidence.get("license_name") != "IGCYL-NC"
            or license_evidence.get("license_url")
            != _IGCYL_NC_LICENSE_URL
            or license_evidence.get("archive_member")
            != "Licencia-IGCYL.txt"
            or license_evidence.get("crc32")
            != license_entry["crc32"]
            or license_evidence.get("uncompressed_bytes")
            != license_entry["uncompressed_bytes"]
            or license_evidence.get("sha256")
            != _IGCYL_NC_LICENSE_SHA256_BY_CRC32.get(
                license_entry["crc32"]
            )
            or license_evidence.get("required_attribution")
            != "© Junta de Castilla y León"
            or license_evidence.get("recipient_acceptance_required")
            is not True
            or license_evidence.get(
                "commercial_license_required_if_commercial"
            )
            is not True
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL archive license evidence changed"
            )
        _https_url(license_evidence["license_url"], allow_query=False)
        if _contains_forbidden_authorization(item):
            raise IDECyLExactEvidenceError(
                "IDECyL evidence fabricates a human authorization"
            )
        result[layer_id] = item
    if list(result) != sorted(_REVIEWABLE_ARCHIVE_LAYER_IDS):
        raise IDECyLExactEvidenceError(
            "IDECyL reviewable archives are not canonical"
        )
    return result


def _archive_audit_capture(
    value: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {
        "verified_on",
        "headers_sha256",
        "tail_sha256",
        "tail_range_start",
        "tail_length",
        "capture_kind",
        "accept_ranges",
        "baseline_response",
        "baseline_entries",
    }:
        raise IDECyLExactEvidenceError(
            "IDECyL archive audit capture is invalid"
        )
    response = value.get("baseline_response")
    entries = value.get("baseline_entries")
    if (
        value.get("verified_on") != "2026-07-27"
        or _SHA256_RE.fullmatch(str(value.get("headers_sha256"))) is None
        or _SHA256_RE.fullmatch(str(value.get("tail_sha256"))) is None
        or value.get("accept_ranges") is not True
        or not isinstance(response, dict)
        or set(response)
        != {"content_length", "content_type", "etag", "last_modified"}
        or response.get("content_type")
        != "application/x-zip-compressed"
        or isinstance(response.get("content_length"), bool)
        or not isinstance(response.get("content_length"), int)
        or response["content_length"] < 22
        or not isinstance(response.get("etag"), str)
        or not response["etag"]
        or not isinstance(response.get("last_modified"), str)
        or not response["last_modified"]
        or isinstance(value.get("tail_range_start"), bool)
        or not isinstance(value.get("tail_range_start"), int)
        or value["tail_range_start"] < 0
        or isinstance(value.get("tail_length"), bool)
        or not isinstance(value.get("tail_length"), int)
        or value["tail_length"] < 22
        or value["tail_range_start"] + value["tail_length"]
        != response["content_length"]
        or value.get("capture_kind")
        not in {"complete_archive", "http_range_suffix"}
        or (
            (value["tail_range_start"] == 0)
            != (value["capture_kind"] == "complete_archive")
        )
        or not isinstance(entries, list)
        or not 2 <= len(entries) <= 4_096
    ):
        raise IDECyLExactEvidenceError(
            "IDECyL archive audit capture changed"
        )
    names: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {
                "name",
                "crc32",
                "compressed_bytes",
                "uncompressed_bytes",
                "compression",
            }
            or not isinstance(entry.get("name"), str)
            or not entry["name"]
            or "/" in entry["name"]
            or "\\" in entry["name"]
            or entry["name"].casefold() in names
            or re.fullmatch(
                r"[0-9a-f]{8}",
                str(entry.get("crc32")),
                re.ASCII,
            )
            is None
            or any(
                isinstance(entry.get(key), bool)
                or not isinstance(entry.get(key), int)
                or entry[key] < 0
                for key in ("compressed_bytes", "uncompressed_bytes")
            )
            or entry.get("compression") not in {"stored", "deflate"}
        ):
            raise IDECyLExactEvidenceError(
                "IDECyL archive baseline entry is invalid"
            )
        names.add(entry["name"].casefold())
    return entries, response


def _contains_forbidden_authorization(value: Any) -> bool:
    forbidden = {
        "reviewer",
        "reviewed_at",
        "allow_download",
        "allow_cache",
        "allow_redistribution",
    }
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if forbidden.intersection(current):
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _https_url(value: Any, *, allow_query: bool) -> str:
    if not isinstance(value, str) or len(value) > 8_192:
        raise IDECyLExactEvidenceError("IDECyL evidence URL is invalid")
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence URL is invalid"
        ) from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port not in {None, 443}
        or not parts.path.startswith("/")
        or parts.fragment
        or (parts.query and not allow_query)
    ):
        raise IDECyLExactEvidenceError("IDECyL evidence URL is invalid")
    return value


def _fresh_source(
    source: ReviewedIDECyLExactSource,
) -> ReviewedIDECyLExactSource:
    return ReviewedIDECyLExactSource(
        profile=source.profile,
        audit_layer_id=source.audit_layer_id,
        catalog_layer_source_key=source.catalog_layer_source_key,
        catalog_endpoint_url=source.catalog_endpoint_url,
        catalog_remote_name=source.catalog_remote_name,
        local_service_status=source.local_service_status,
        reason_codes=source.reason_codes,
        protocol=source.protocol,
        target_kind=source.target_kind,
        endpoint_url=source.endpoint_url,
        remote_name=source.remote_name,
        sync_strategy=source.sync_strategy,
        candidate_config=deepcopy(source.candidate_config),
        evidence=deepcopy(source.evidence),
    )


def _resource_body(resource_path: str, maximum: int) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    try:
        body = resource.read_bytes()
    except OSError as error:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence resource is unavailable"
        ) from error
    _bounded_bytes(body, maximum)
    return body


def _bounded_bytes(body: bytes, maximum: int) -> None:
    if not isinstance(body, bytes) or not 1 <= len(body) <= maximum:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence resource size is invalid"
        )


def _exact_digest(body: bytes, expected: str, label: str) -> None:
    if (
        _SHA256_RE.fullmatch(expected) is None
        or hashlib.sha256(body).hexdigest() != expected
    ):
        raise IDECyLExactEvidenceError(f"{label} failed its local digest")


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence JSON is invalid"
        ) from error
    if not isinstance(value, dict):
        raise IDECyLExactEvidenceError(
            "IDECyL evidence JSON root is invalid"
        )
    return value


def _canonical_json_sha256(value: Any) -> str:
    try:
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise IDECyLExactEvidenceError(
            "IDECyL evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(body).hexdigest()
