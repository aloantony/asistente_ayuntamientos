"""Closed, hash-bound local-style recipes for exact IDECyL archives.

The source classification proves where an exact dataset may be acquired, but
does not claim that the upstream WMS styling can be reproduced.  This module
accepts only separately authored, explicitly adapted recipes whose catalog
identity and inspected vector schema are committed byte-for-byte.

The recipes are non-authorizing.  They cannot bypass the persisted human
mirror review required by the source evidence.
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
from typing import Any

from app.reference_layers.idecyl_exact_evidence import (
    MANIFEST_RESOURCE as EXACT_SOURCE_MANIFEST_RESOURCE,
    MANIFEST_SHA256 as EXACT_SOURCE_MANIFEST_SHA256,
    ReviewedIDECyLExactSource,
    reviewed_idecyl_exact_source,
)


MANIFEST_SCHEMA = "siur-idecyl-local-style-evidence-manifest/v1"
MANIFEST_RESOURCE = "evidence/idecyl_local_styles/manifest-v1.json"
MANIFEST_SHA256 = (
    "c71b6493ff7def4fe57676e558abe658"
    "dfba2595e8417f92eec282d8f40a055a"
)
STYLE_CONFIG_SCHEMA = "siur-reviewed-idecyl-local-style/v1"
GENERATOR_VERSION = "siur-sld-1.0-idecyl-local-adaptation/v1"
AUTHORIZATION_EFFECT = "none_without_persisted_human_mirror_review"
SOURCE_PRIORITY = 5
MAX_MANIFEST_BYTES = 64 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SUPPORTED_AUDIT_LAYER_ID = 86
_EXPECTED_EXCLUSIONS = [
    {
        "audit_layer_id": 65,
        "reason_code": (
            "qml_external_raster_marker_missing_and_"
            "geopackage_envelopes_unverifiable"
        ),
    },
    {
        "audit_layer_id": 66,
        "reason_code": "complete_local_archive_unavailable",
    },
    {
        "audit_layer_id": 232,
        "reason_code": "complete_local_archive_unavailable",
    },
    {
        "audit_layer_id": 296,
        "reason_code": "complete_local_archive_unavailable",
    },
]
_EXPECTED_CATALOG_IDENTITY = {
    "catalog_endpoint_url": (
        "https://idecyl.jcyl.es/geoserver/mineria/wms"
    ),
    "catalog_layer_source_key": (
        "layer:siur:"
        "7c51e023e8eb888f7d969ebf5cc42257f15c098823e34cd0deec85adfecd92df"
    ),
    "catalog_remote_name": "cami_cyl_cuadricula",
}
_EXPECTED_CATALOG_STYLE = {
    "catalog_style_source_key": "cami_cyl_cuadricula_default",
    "is_default": True,
    "remote_name": "cami_cyl_cuadricula_default",
    "title": "Borde celdas negro",
}
_EXPECTED_CATALOG_SNAPSHOT_SHA256 = (
    "950eb0dbfb9226a39257ca61d27ef815"
    "353bcad76d00c5c18348489480ba53fc"
)
_EXPECTED_ARCHIVE_SNAPSHOT_SHA256 = (
    "46510f58ff7dc73176b99480588a31ec"
    "a4227fd84eb339a901e92823eedbd84d"
)
_EXPECTED_GEOPACKAGE_MEMBER_SHA256 = (
    "24929cfcd0ad1024d34ec396e56b05be"
    "a83f258dce7254498eb12a0aca24522a"
)
_EXPECTED_SAMPLE_SHA256 = (
    "9e3a10309297cb44c7b13c00b3eb03d"
    "db542f6d788d8ededa0d681e1291b6c2e"
)
_EXPECTED_BOUNDS = {
    "east": 645386.371,
    "north": 4799753.280999999,
    "south": 4430038.848999999,
    "west": 127292.888,
}
_EXPECTED_DATA_SCHEMA = [
    {
        "declared_type": "INTEGER",
        "default": None,
        "hidden": 0,
        "name": "fid",
        "not_null": True,
        "ordinal": 0,
        "primary_key_ordinal": 1,
    },
    {
        "declared_type": "MULTIPOLYGON",
        "default": None,
        "hidden": 0,
        "name": "geometry",
        "not_null": False,
        "ordinal": 1,
        "primary_key_ordinal": 0,
    },
    {
        "declared_type": "REAL",
        "default": None,
        "hidden": 0,
        "name": "area",
        "not_null": False,
        "ordinal": 2,
        "primary_key_ordinal": 0,
    },
]
_EXPECTED_DATA_SCHEMA_SHA256 = (
    "0d0bf9bf6263c2e843e978df26d42b6"
    "875ddcdaf7d3d03b231080756b5a0475b"
)
_EXPECTED_VISUAL_RECIPE = {
    "evidence_basis": {
        "catalog_style_title": "Borde celdas negro",
        "geometry_type": "MULTIPOLYGON",
    },
    "fill_color": "#ffffff",
    "fill_opacity": 0,
    "outline_color": "#000000",
    "outline_width": 1,
    "schema": "siur-idecyl-simple-vector-style/v1",
    "symbolizer": "polygon",
}


class IDECyLLocalStyleEvidenceError(RuntimeError):
    """The committed local-style evidence is missing or inconsistent."""


@dataclass(frozen=True)
class ReviewedIDECyLLocalStyle:
    """One exact catalog style with a reviewed local adaptation."""

    audit_layer_id: int
    profile: str
    catalog_layer_source_key: str
    catalog_endpoint_url: str
    catalog_remote_name: str
    catalog_style_source_key: str
    remote_style_name: str
    style_title: str
    is_default: bool
    selected_layer_name: str
    recipe_identity_sha256: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _EvidencePackage:
    recipes: tuple[ReviewedIDECyLLocalStyle, ...]
    by_profile: MappingProxyType


def idecyl_local_style_inventory() -> tuple[ReviewedIDECyLLocalStyle, ...]:
    """Return fresh projections of all authored IDECyL style recipes."""

    return tuple(_fresh(item) for item in _committed_package().recipes)


def reviewed_idecyl_local_style(
    profile: str,
) -> ReviewedIDECyLLocalStyle | None:
    """Resolve one recipe by the exact source profile."""

    item = _committed_package().by_profile.get(profile)
    return _fresh(item) if item is not None else None


def reviewed_idecyl_local_style_for_source(
    source: ReviewedIDECyLExactSource,
) -> ReviewedIDECyLLocalStyle | None:
    """Resolve a recipe only when every source identity still matches."""

    item = reviewed_idecyl_local_style(source.profile)
    if item is None:
        return None
    if (
        source.audit_layer_id != item.audit_layer_id
        or source.catalog_layer_source_key
        != item.catalog_layer_source_key
        or source.catalog_endpoint_url != item.catalog_endpoint_url
        or source.catalog_remote_name != item.catalog_remote_name
        or source.local_service_status != "candidate"
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style source identity changed"
        )
    return item


def idecyl_local_style_config(
    item: ReviewedIDECyLLocalStyle,
) -> dict[str, Any]:
    """Return the compact recipe identity stored in a source definition."""

    return {
        "schema": STYLE_CONFIG_SCHEMA,
        "audit_layer_id": item.audit_layer_id,
        "profile": item.profile,
        "recipe_identity_sha256": item.recipe_identity_sha256,
        "catalog_style_source_key": item.catalog_style_source_key,
        "remote_name": item.remote_style_name,
        "is_default": item.is_default,
    }


def idecyl_local_style_expected_source_definition(
    item: ReviewedIDECyLLocalStyle,
) -> dict[str, Any]:
    """Build the full canonical definition to which authored SLDs are bound."""

    source = _exact_source(item)
    config = deepcopy(source.candidate_config)
    if config is None:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style source has no candidate configuration"
        )
    config["reviewed_local_style"] = idecyl_local_style_config(item)
    config["reviewed_equivalence"] = deepcopy(source.evidence)
    return {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": SOURCE_PRIORITY,
        "config": config,
    }


def canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style evidence is not canonical JSON"
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
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style manifest failed its local digest"
        )
    try:
        manifest = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style manifest is invalid JSON"
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
    if canonical != body or not isinstance(manifest, dict):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style manifest is not canonical"
        )
    if (
        set(manifest)
        != {"schema", "capture", "excluded_priority_layers", "recipes"}
        or manifest.get("schema") != MANIFEST_SCHEMA
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style manifest shape is invalid"
        )
    _validate_capture(manifest.get("capture"))
    if manifest.get("excluded_priority_layers") != _EXPECTED_EXCLUSIONS:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style exclusions changed"
        )
    raw_recipes = manifest.get("recipes")
    if not isinstance(raw_recipes, list) or len(raw_recipes) != 1:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style recipe inventory changed"
        )
    recipes = tuple(_parse_recipe(item) for item in raw_recipes)
    by_profile = {item.profile: item for item in recipes}
    if len(by_profile) != len(recipes):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style profiles are ambiguous"
        )
    return _EvidencePackage(
        recipes=recipes,
        by_profile=MappingProxyType(by_profile),
    )


def _validate_capture(value: Any) -> None:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "authorization_effect",
            "catalog_snapshot_sha256",
            "exact_source_manifest_resource",
            "exact_source_manifest_sha256",
            "generator_version",
            "inspected_on",
            "network_access_during_style_authorship",
        }
        or value.get("authorization_effect") != AUTHORIZATION_EFFECT
        or value.get("exact_source_manifest_resource")
        != EXACT_SOURCE_MANIFEST_RESOURCE
        or value.get("exact_source_manifest_sha256")
        != EXACT_SOURCE_MANIFEST_SHA256
        or value.get("generator_version") != GENERATOR_VERSION
        or value.get("inspected_on") != "2026-07-27"
        or value.get("network_access_during_style_authorship") is not False
        or value.get("catalog_snapshot_sha256")
        != _EXPECTED_CATALOG_SNAPSHOT_SHA256
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style capture changed"
        )


def _parse_recipe(value: Any) -> ReviewedIDECyLLocalStyle:
    expected_keys = {
        "audit_layer_id",
        "catalog_identity",
        "catalog_style",
        "dataset_inspection",
        "exact_style_claim",
        "parity_kind",
        "profile",
        "recipe_identity_sha256",
        "source_binding",
        "visual_recipe",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("audit_layer_id") != _SUPPORTED_AUDIT_LAYER_ID
        or value.get("catalog_identity") != _EXPECTED_CATALOG_IDENTITY
        or value.get("catalog_style") != _EXPECTED_CATALOG_STYLE
        or value.get("parity_kind") != "adapted"
        or value.get("exact_style_claim") is not False
        or value.get("visual_recipe") != _EXPECTED_VISUAL_RECIPE
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style recipe changed"
        )
    profile = value.get("profile")
    identity_sha256 = value.get("recipe_identity_sha256")
    if (
        not isinstance(profile, str)
        or not profile
        or _SHA256_RE.fullmatch(str(identity_sha256)) is None
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style recipe identity is invalid"
        )
    unhashed = {
        key: item
        for key, item in value.items()
        if key != "recipe_identity_sha256"
    }
    if canonical_json_sha256(unhashed) != identity_sha256:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style recipe digest changed"
        )
    dataset = value.get("dataset_inspection")
    _validate_dataset_inspection(dataset)
    source = reviewed_idecyl_exact_source(
        catalog_layer_source_key=_EXPECTED_CATALOG_IDENTITY[
            "catalog_layer_source_key"
        ],
        catalog_endpoint_url=_EXPECTED_CATALOG_IDENTITY[
            "catalog_endpoint_url"
        ],
        catalog_remote_name=_EXPECTED_CATALOG_IDENTITY[
            "catalog_remote_name"
        ],
    )
    if (
        source is None
        or source.audit_layer_id != _SUPPORTED_AUDIT_LAYER_ID
        or source.profile != profile
        or source.local_service_status != "candidate"
        or source.candidate_config is None
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style exact source changed"
        )
    _validate_source_binding(value.get("source_binding"), source)
    return ReviewedIDECyLLocalStyle(
        audit_layer_id=_SUPPORTED_AUDIT_LAYER_ID,
        profile=profile,
        catalog_layer_source_key=source.catalog_layer_source_key,
        catalog_endpoint_url=source.catalog_endpoint_url,
        catalog_remote_name=source.catalog_remote_name,
        catalog_style_source_key=_EXPECTED_CATALOG_STYLE[
            "catalog_style_source_key"
        ],
        remote_style_name=_EXPECTED_CATALOG_STYLE["remote_name"],
        style_title=_EXPECTED_CATALOG_STYLE["title"],
        is_default=True,
        selected_layer_name=source.candidate_config["input_layer"],
        recipe_identity_sha256=identity_sha256,
        evidence=deepcopy(value),
    )


def _validate_dataset_inspection(value: Any) -> None:
    expected_keys = {
        "archive_member",
        "archive_snapshot_sha256",
        "archive_snapshot_size_bytes",
        "data_schema",
        "data_schema_sha256",
        "declared_bounds",
        "empty_geometry_count",
        "feature_count",
        "feature_layer",
        "feature_layers",
        "geometry_bounds",
        "geometry_column",
        "geometry_type",
        "geopackage_member_sha256",
        "inspection_schema",
        "sample_sha256",
        "srs",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("archive_member") != "cami_cyl.gpkg"
        or value.get("archive_snapshot_size_bytes") != 47_065_050
        or value.get("data_schema") != _EXPECTED_DATA_SCHEMA
        or value.get("data_schema_sha256")
        != _EXPECTED_DATA_SCHEMA_SHA256
        or canonical_json_sha256(value.get("data_schema"))
        != _EXPECTED_DATA_SCHEMA_SHA256
        or value.get("empty_geometry_count") != 0
        or value.get("feature_count") != 669_365
        or value.get("feature_layer") != "cuadricula"
        or value.get("feature_layers") != ["cuadricula"]
        or value.get("geometry_column") != "geometry"
        or value.get("geometry_type") != "MULTIPOLYGON"
        or value.get("inspection_schema")
        != "reference-geopackage-zip-inspection/v1"
        or value.get("srs") != "EPSG:25830"
        or value.get("archive_snapshot_sha256")
        != _EXPECTED_ARCHIVE_SNAPSHOT_SHA256
        or value.get("geopackage_member_sha256")
        != _EXPECTED_GEOPACKAGE_MEMBER_SHA256
        or value.get("sample_sha256") != _EXPECTED_SAMPLE_SHA256
        or value.get("declared_bounds") != _EXPECTED_BOUNDS
        or value.get("geometry_bounds") != _EXPECTED_BOUNDS
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style dataset inspection changed"
        )


def _validate_source_binding(
    value: Any,
    source: ReviewedIDECyLExactSource,
) -> None:
    base_config = deepcopy(source.candidate_config)
    if base_config is None:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style source configuration is missing"
        )
    base_config["reviewed_equivalence"] = deepcopy(source.evidence)
    base_definition = {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": SOURCE_PRIORITY,
        "config": base_config,
    }
    expected = {
        "base_source_definition_sha256": canonical_json_sha256(
            base_definition
        ),
        "data_format": source.candidate_config["data_format"],
        "reviewed_equivalence_sha256": canonical_json_sha256(
            source.evidence
        ),
        "selected_endpoint_url": source.endpoint_url,
        "selected_protocol": source.protocol,
        "selected_remote_name": source.remote_name,
        "selected_sync_strategy": source.sync_strategy,
        "selected_target_kind": source.target_kind,
    }
    if value != expected:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style source binding changed"
        )


def _exact_source(
    item: ReviewedIDECyLLocalStyle,
) -> ReviewedIDECyLExactSource:
    source = reviewed_idecyl_exact_source(
        catalog_layer_source_key=item.catalog_layer_source_key,
        catalog_endpoint_url=item.catalog_endpoint_url,
        catalog_remote_name=item.catalog_remote_name,
    )
    if (
        source is None
        or source.audit_layer_id != item.audit_layer_id
        or source.profile != item.profile
        or source.local_service_status != "candidate"
        or source.protocol is None
        or source.target_kind is None
        or source.endpoint_url is None
        or source.remote_name is None
        or source.sync_strategy is None
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style exact source is unavailable"
        )
    return source


def _fresh(
    item: ReviewedIDECyLLocalStyle,
) -> ReviewedIDECyLLocalStyle:
    return ReviewedIDECyLLocalStyle(
        audit_layer_id=item.audit_layer_id,
        profile=item.profile,
        catalog_layer_source_key=item.catalog_layer_source_key,
        catalog_endpoint_url=item.catalog_endpoint_url,
        catalog_remote_name=item.catalog_remote_name,
        catalog_style_source_key=item.catalog_style_source_key,
        remote_style_name=item.remote_style_name,
        style_title=item.style_title,
        is_default=item.is_default,
        selected_layer_name=item.selected_layer_name,
        recipe_identity_sha256=item.recipe_identity_sha256,
        evidence=deepcopy(item.evidence),
    )


def _resource_body(resource: str) -> bytes:
    try:
        return files("app.reference_layers").joinpath(resource).read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style manifest is unavailable"
        ) from error
