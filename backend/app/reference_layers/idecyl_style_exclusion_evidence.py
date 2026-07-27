"""Hash-bound evidence for IDECyL styles that cannot be authored faithfully.

This evidence records why a technically reviewable dataset still has no
reviewed local style.  It is deliberately non-authorizing and never provides
partial recipes: every required catalog style must remain blocked until the
missing upstream artifact or exact symbology is available.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any

from app.reference_layers.idecyl_exact_evidence import (
    MANIFEST_RESOURCE as EXACT_SOURCE_MANIFEST_RESOURCE,
)
from app.reference_layers.idecyl_exact_evidence import (
    MANIFEST_SHA256 as EXACT_SOURCE_MANIFEST_SHA256,
)
from app.reference_layers.idecyl_exact_evidence import (
    ReviewedIDECyLExactSource,
    reviewed_idecyl_exact_source,
)

MANIFEST_SCHEMA = "siur-idecyl-style-exclusion-evidence/v1"
MANIFEST_RESOURCE = "evidence/idecyl_style_exclusions/manifest-v1.json"
MANIFEST_SHA256 = "2e2ae2e50de4880776d0db88bc4efff7" "3dd7ed4be32e72d45541796f2ca75e30"
AUTHORIZATION_EFFECT = "none_without_persisted_human_mirror_review"
MAX_MANIFEST_BYTES = 64 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_EXPECTED_LAYER_IDS = (65, 105)
_EXPECTED_IDENTITY_SHA256 = {
    65: ("f1d3a657d9ce45c7a3917de520ed87fb" "b6dd4ed57efd5f935cb886091988e7bf"),
    105: ("c7dac6a761ae12b3633908f43248b6e" "11dc392ad0abc6cd915ebdb3456454e45"),
}
_EXPECTED_REASONS = {
    65: ("qml_external_raster_marker_missing",),
    105: (
        "complete_local_archive_unavailable",
        "classified_style_categories_and_colors_unavailable",
    ),
}
_EXPECTED_STYLES = {
    65: [
        {
            "catalog_style_source_key": ("eclipse_2026_puntos_recomendados_ico"),
            "is_default": True,
            "remote_name": "eclipse_2026_puntos_recomendados_ico",
            "title": "Representación puntos recomendados icono gafas",
        }
    ],
    105: [
        {
            "catalog_style_source_key": "hidro_cyl_cursos_clasif",
            "is_default": True,
            "remote_name": "hidro_cyl_cursos_clasif",
            "title": "Cursos fluviales azul clasificados tipo",
        },
        {
            "catalog_style_source_key": "hidro_cyl_cursos_simple",
            "is_default": False,
            "remote_name": "hidro_cyl_cursos_simple",
            "title": "Cursos fluviales azul sin clasificar",
        },
    ],
}
_EXPECTED_WMS_CAPABILITIES = {
    65: {
        "sha256": (
            "d49ae8102b6dc6f383168a3fbb96db9" "f31c42434e098baeed4174efa7f8e5c05"
        ),
        "size_bytes": 56_584,
    },
    105: {
        "sha256": (
            "0f027a2764c757398079d84c33a84c09" "2366ec4d6f615777690741951e90274a"
        ),
        "size_bytes": 19_195,
    },
}


class IDECyLStyleExclusionEvidenceError(RuntimeError):
    """Committed style-exclusion evidence is altered or inconsistent."""


@dataclass(frozen=True)
class ReviewedIDECyLStyleExclusion:
    """One exact source for which faithful complete style parity is blocked."""

    audit_layer_id: int
    profile: str
    reason_codes: tuple[str, ...]
    required_catalog_styles: tuple[dict[str, Any], ...]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _EvidencePackage:
    exclusions: tuple[ReviewedIDECyLStyleExclusion, ...]
    by_profile: MappingProxyType


def idecyl_style_exclusion_inventory() -> tuple[ReviewedIDECyLStyleExclusion, ...]:
    """Return fresh, immutable projections of all reviewed exclusions."""

    return tuple(_fresh(item) for item in _committed_package().exclusions)


def reviewed_idecyl_style_exclusion(
    profile: str,
) -> ReviewedIDECyLStyleExclusion | None:
    """Resolve one exclusion by the exact reviewed source profile."""

    item = _committed_package().by_profile.get(profile)
    return _fresh(item) if item is not None else None


def canonical_json_sha256(value: Any) -> str:
    """Hash JSON with the same canonical projection used by source evidence."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=1)
def _committed_package() -> _EvidencePackage:
    return _load_evidence_package(
        _resource_body(MANIFEST_RESOURCE),
        expected_sha256=MANIFEST_SHA256,
    )


def _load_evidence_package(
    body: bytes,
    *,
    expected_sha256: str,
) -> _EvidencePackage:
    if (
        not isinstance(body, bytes)
        or not 1 <= len(body) <= MAX_MANIFEST_BYTES
        or _SHA256_RE.fullmatch(expected_sha256) is None
        or hashlib.sha256(body).hexdigest() != expected_sha256
    ):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion manifest failed its local digest"
        )
    try:
        manifest = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion manifest is invalid JSON"
        ) from error
    canonical = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if (
        canonical != body
        or not isinstance(manifest, dict)
        or set(manifest) != {"capture", "layers", "schema"}
        or manifest.get("schema") != MANIFEST_SCHEMA
    ):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion manifest shape is invalid"
        )
    _validate_capture(manifest.get("capture"))
    raw_layers = manifest.get("layers")
    if not isinstance(raw_layers, list):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion layers are invalid"
        )
    exclusions = tuple(_parse_layer(value) for value in raw_layers)
    if tuple(item.audit_layer_id for item in exclusions) != (_EXPECTED_LAYER_IDS):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion inventory changed"
        )
    by_profile = {item.profile: item for item in exclusions}
    if len(by_profile) != len(exclusions):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion profiles are ambiguous"
        )
    return _EvidencePackage(
        exclusions=exclusions,
        by_profile=MappingProxyType(by_profile),
    )


def _validate_capture(value: Any) -> None:
    expected = {
        "authorization_effect": AUTHORIZATION_EFFECT,
        "captured_on": "2026-07-27",
        "catalog_snapshot_sha256": (
            "950eb0dbfb9226a39257ca61d27ef815" "353bcad76d00c5c18348489480ba53fc"
        ),
        "exact_source_manifest_resource": EXACT_SOURCE_MANIFEST_RESOURCE,
        "exact_source_manifest_sha256": EXACT_SOURCE_MANIFEST_SHA256,
        "network_access_during_authorship": False,
        "previous_local_style_manifest_resource": (
            "evidence/idecyl_local_styles/manifest-v1.json"
        ),
        "previous_local_style_manifest_sha256": (
            "c71b6493ff7def4fe57676e558abe658" "dfba2595e8417f92eec282d8f40a055a"
        ),
        "review_scope": "faithful_local_style_parity_exclusions_only",
    }
    if value != expected:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion capture changed"
        )


def _parse_layer(value: Any) -> ReviewedIDECyLStyleExclusion:
    expected_keys = {
        "audit_layer_id",
        "catalog_identity",
        "evidence_identity_sha256",
        "local_artifact_observation",
        "outcome",
        "required_catalog_styles",
        "source_binding",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion layer shape is invalid"
        )
    layer_id = value.get("audit_layer_id")
    identity = value.get("evidence_identity_sha256")
    if (
        isinstance(layer_id, bool)
        or layer_id not in _EXPECTED_LAYER_IDS
        or identity != _EXPECTED_IDENTITY_SHA256[layer_id]
    ):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion layer identity changed"
        )
    unhashed = {
        key: item for key, item in value.items() if key != "evidence_identity_sha256"
    }
    if canonical_json_sha256(unhashed) != identity:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion layer digest changed"
        )
    source = _exact_source(value.get("catalog_identity"), layer_id)
    _validate_source_binding(value.get("source_binding"), source)
    _validate_styles(value.get("required_catalog_styles"), layer_id)
    _validate_outcome(value.get("outcome"), layer_id)
    _validate_local_observation(
        value.get("local_artifact_observation"),
        source=source,
        layer_id=layer_id,
    )
    return ReviewedIDECyLStyleExclusion(
        audit_layer_id=layer_id,
        profile=source.profile,
        reason_codes=_EXPECTED_REASONS[layer_id],
        required_catalog_styles=tuple(deepcopy(value["required_catalog_styles"])),
        evidence=deepcopy(value),
    )


def _exact_source(
    value: Any,
    layer_id: int,
) -> ReviewedIDECyLExactSource:
    if not isinstance(value, dict):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion catalog identity is invalid"
        )
    source = reviewed_idecyl_exact_source(
        catalog_layer_source_key=value.get("catalog_layer_source_key"),
        catalog_endpoint_url=value.get("catalog_endpoint_url"),
        catalog_remote_name=value.get("catalog_remote_name"),
    )
    if (
        source is None
        or source.audit_layer_id != layer_id
        or source.local_service_status != "candidate"
        or source.candidate_config is None
    ):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion exact source changed"
        )
    expected = {
        "catalog_endpoint_url": source.catalog_endpoint_url,
        "catalog_layer_source_key": source.catalog_layer_source_key,
        "catalog_remote_name": source.catalog_remote_name,
    }
    if value != expected:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion catalog identity changed"
        )
    return source


def _validate_source_binding(
    value: Any,
    source: ReviewedIDECyLExactSource,
) -> None:
    expected = {
        "candidate_config_sha256": canonical_json_sha256(source.candidate_config),
        "exact_source_evidence_sha256": canonical_json_sha256(source.evidence),
        "profile": source.profile,
        "selected_endpoint_url": source.endpoint_url,
        "selected_protocol": source.protocol,
        "selected_remote_name": source.remote_name,
        "selected_sync_strategy": source.sync_strategy,
        "selected_target_kind": source.target_kind,
    }
    if value != expected:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion source binding changed"
        )


def _validate_styles(value: Any, layer_id: int) -> None:
    if value != _EXPECTED_STYLES[layer_id]:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL required style inventory changed"
        )


def _validate_outcome(value: Any, layer_id: int) -> None:
    expected = {
        "authorization_effect": AUTHORIZATION_EFFECT,
        "complete_style_parity": False,
        "reason_codes": list(_EXPECTED_REASONS[layer_id]),
        "status": "excluded",
    }
    if value != expected:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion outcome changed"
        )


def _validate_local_observation(
    value: Any,
    *,
    source: ReviewedIDECyLExactSource,
    layer_id: int,
) -> None:
    if not isinstance(value, dict):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL local artifact observation is invalid"
        )
    capture = value.get("archive_capture")
    baseline = source.evidence.get("audit_capture")
    response = baseline.get("baseline_response") if isinstance(baseline, dict) else None
    if not isinstance(capture, dict) or not isinstance(response, dict):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL archive capture observation is invalid"
        )
    expected_common = {
        "capture_kind": baseline.get("capture_kind"),
        "capture_length": baseline.get("tail_length"),
        "capture_range_start": baseline.get("tail_range_start"),
        "content_length": response.get("content_length"),
        "etag": response.get("etag"),
        "last_modified": response.get("last_modified"),
    }
    for key, expected in expected_common.items():
        if capture.get(key) != expected:
            raise IDECyLStyleExclusionEvidenceError(
                "IDECyL archive capture identity changed"
            )
    capture_hash_key = "archive_sha256" if layer_id == 65 else "capture_sha256"
    if (
        capture.get(capture_hash_key) != baseline.get("tail_sha256")
        or value.get("wms_capabilities_capture") != _EXPECTED_WMS_CAPABILITIES[layer_id]
    ):
        raise IDECyLStyleExclusionEvidenceError("IDECyL local artifact digest changed")
    if layer_id == 65:
        style = value.get("style_artifact")
        dataset = value.get("dataset_inspection")
        if (
            set(value)
            != {
                "archive_capture",
                "dataset_inspection",
                "style_artifact",
                "wms_capabilities_capture",
            }
            or not isinstance(style, dict)
            or style.get("archive_member") != "eclipse_2026_puntos_recomendados.qml"
            or style.get("archive_member_sha256")
            != ("1819a46a1998a52d22939939c9ca8f70" "12ec858d9a4dee5c94061f2945da37e5")
            or style.get("renderer") != "RasterMarker"
            or style.get("external_resource_path")
            != "D:/Temp/eclipse/pcivil/png/observa_ico.png"
            or style.get("external_resource_archive_member_matches") != 0
            or style.get("self_contained") is not False
            or not isinstance(dataset, dict)
            or dataset.get("integrity_check") != "ok"
            or dataset.get("feature_count") != 75
            or dataset.get("empty_geometry_count") != 0
            or dataset.get("geometry_type") != "POINT"
            or dataset.get("declared_bounds") != dataset.get("geometry_bounds")
        ):
            raise IDECyLStyleExclusionEvidenceError(
                "IDECyL missing marker evidence changed"
            )
    elif (
        set(value)
        != {
            "archive_capture",
            "complete_archive_available_locally",
            "wms_capabilities_capture",
        }
        or value.get("complete_archive_available_locally") is not False
    ):
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL incomplete archive evidence changed"
        )


def _fresh(
    item: ReviewedIDECyLStyleExclusion,
) -> ReviewedIDECyLStyleExclusion:
    return ReviewedIDECyLStyleExclusion(
        audit_layer_id=item.audit_layer_id,
        profile=item.profile,
        reason_codes=item.reason_codes,
        required_catalog_styles=tuple(
            deepcopy(value) for value in item.required_catalog_styles
        ),
        evidence=deepcopy(item.evidence),
    )


def _resource_body(resource: str) -> bytes:
    try:
        return files("app.reference_layers").joinpath(resource).read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise IDECyLStyleExclusionEvidenceError(
            "IDECyL style-exclusion manifest is unavailable"
        ) from error
