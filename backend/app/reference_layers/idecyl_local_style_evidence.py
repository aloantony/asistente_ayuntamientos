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


MANIFEST_SCHEMA = "siur-idecyl-local-style-evidence-manifest/v2"
PREVIOUS_MANIFEST_SCHEMA = "siur-idecyl-local-style-evidence-manifest/v1"
MANIFEST_RESOURCE = "evidence/idecyl_local_styles/manifest-v2.json"
PREVIOUS_MANIFEST_RESOURCE = (
    "evidence/idecyl_local_styles/manifest-v1.json"
)
MANIFEST_SHA256 = (
    "01948171db8103704d1658ab69f0ed7e"
    "13ecf19f482d8ff68171707b40b35bc6"
)
PREVIOUS_MANIFEST_SHA256 = (
    "c71b6493ff7def4fe57676e558abe658"
    "dfba2595e8417f92eec282d8f40a055a"
)
STYLE_CONFIG_SCHEMA = "siur-reviewed-idecyl-local-style/v1"
GENERATOR_VERSION = "siur-sld-1.0-idecyl-local-adaptation/v1"
AUTHORIZATION_EFFECT = "none_without_persisted_human_mirror_review"
SOURCE_PRIORITY = 5
MAX_MANIFEST_BYTES = 256 * 1024

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
_EXPECTED_STYLE_CAPABILITIES_SNAPSHOT_SHA256 = (
    "b43c4d7659d741ce1f5e871432d1fe9"
    "612d49c438b171784b2f9f6a940388da4"
)
_EXPECTED_STYLE_FAMILY_SLD_SNAPSHOT_SHA256 = (
    "1e15bc83a4efae060f177e227e954db"
    "1b61dc7254f7809c54dcccb78fedb7288"
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
_BOUNDARY_STYLE_TEMPLATES = (
    {
        "suffix": "negro_etiquetado",
        "catalog_title": "Contorno negro y etiqueta texto",
        "upstream_style_title": (
            "Representación contorno negro y etiqueta texto"
        ),
        "outline_color": "#000000",
        "outline_width": 0.1,
        "labelled": True,
        "label_color": "#000000",
        "halo_color": "#ffffff",
    },
    {
        "suffix": "blanco_etiquetado",
        "catalog_title": "Contorno blanco y etiqueta texto",
        "upstream_style_title": (
            "Representación contorno blanco y etiqueta texto"
        ),
        "outline_color": "#ffffff",
        "outline_width": 0.1,
        "labelled": True,
        "label_color": "#ffffff",
        "halo_color": "#000000",
    },
    {
        "suffix": "negro",
        "catalog_title": "Contorno negro",
        "upstream_style_title": "Representación contorno negro",
        "outline_color": "#000000",
        "outline_width": 0.1,
        "labelled": False,
    },
    {
        "suffix": "blanco",
        "catalog_title": "Contorno blanco",
        "upstream_style_title": "Representación contorno blanco",
        "outline_color": "#ffffff",
        "outline_width": 0.1,
        "labelled": False,
    },
    {
        "suffix": "amarillo",
        "catalog_title": "Contorno amarillo resalte",
        "upstream_style_title": "Representación contorno amarillo",
        "outline_color": "#ffff00",
        "outline_width": 1.1,
        "labelled": False,
    },
    {
        "suffix": "fucsia",
        "catalog_title": "Contorno fucsia resalte",
        "upstream_style_title": "Representación contorno fucsia",
        "outline_color": "#ff00ff",
        "outline_width": 1.1,
        "labelled": False,
    },
)
_BOUNDARY_LAYER_SPECS = {
    223: {
        "profile": (
            "idecyl-limites-esp-autonomias-archive-20260727-v3"
        ),
        "catalog_identity": {
            "catalog_endpoint_url": (
                "https://idecyl.jcyl.es/geoserver/limites/wms"
            ),
            "catalog_layer_source_key": (
                "layer:siur:"
                "94eaea7b3d42f35abf3f5a740a38f2b45592492d5f6923e31d3f56329b5fbc0f"
            ),
            "catalog_remote_name": "limites_esp_autonomias",
        },
        "style_prefix": "limites_autonomias_",
        "default_style": "limites_autonomias_negro_etiquetado",
        "label_field": "n_auton",
        "unit_name": "autonomía",
        "dataset_inspection": {
            "archive_member": "limites_esp_autonomias.gpkg",
            "archive_snapshot_sha256": (
                "21628e63a6df3b78b87502ba903fe5f"
                "54c204598e5700c43dcd7af5d9bc3a780"
            ),
            "archive_snapshot_size_bytes": 12_241_947,
            "data_schema": [
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
                    "declared_type": "TEXT(2)",
                    "default": None,
                    "hidden": 0,
                    "name": "c_tip_ent",
                    "not_null": False,
                    "ordinal": 2,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(18)",
                    "default": None,
                    "hidden": 0,
                    "name": "n_tip_ent",
                    "not_null": False,
                    "ordinal": 3,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(26)",
                    "default": None,
                    "hidden": 0,
                    "name": "n_auton",
                    "not_null": False,
                    "ordinal": 4,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(2)",
                    "default": None,
                    "hidden": 0,
                    "name": "c_auton",
                    "not_null": False,
                    "ordinal": 5,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "REAL",
                    "default": None,
                    "hidden": 0,
                    "name": "m_sup_m",
                    "not_null": False,
                    "ordinal": 6,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(24)",
                    "default": None,
                    "hidden": 0,
                    "name": "inspireid",
                    "not_null": False,
                    "ordinal": 7,
                    "primary_key_ordinal": 0,
                },
            ],
            "data_schema_sha256": (
                "6b83f9c9a3bc270281c32b0653f2acd"
                "40c6d1c567227d26d220f25ff91ab1555"
            ),
            "declared_bounds": {
                "east": 4.32778473,
                "north": 43.792379568,
                "south": 27.63772315,
                "west": -18.16118085,
            },
            "empty_geometry_count": 0,
            "feature_count": 19,
            "feature_layer": "limites_esp_autonomias",
            "feature_layers": ["limites_esp_autonomias"],
            "geometry_bounds": {
                "east": 4.32778473,
                "north": 43.792379568,
                "south": 27.63772315,
                "west": -18.16118085,
            },
            "geometry_column": "geometry",
            "geometry_type": "MULTIPOLYGON",
            "geopackage_member_sha256": (
                "17b87ee7df386f4c865e3d9cf467cce7"
                "791a4857c3c9a798b3567ccc86baed5a"
            ),
            "inspection_schema": (
                "reference-geopackage-zip-inspection/v1"
            ),
            "sample_sha256": (
                "06f8fc27aa1835534f47343ccbac0826"
                "b310ed73a7981614241ad55595db6d0b"
            ),
            "srs": "EPSG:4258",
        },
    },
    234: {
        "profile": (
            "idecyl-limites-esp-provincias-archive-20260727-v3"
        ),
        "catalog_identity": {
            "catalog_endpoint_url": (
                "https://idecyl.jcyl.es/geoserver/limites/wms"
            ),
            "catalog_layer_source_key": (
                "layer:siur:"
                "780d5988625831d7e0e03bad3cf5c812af0f65083c60c84c8aa70de761f7d627"
            ),
            "catalog_remote_name": "limites_esp_provincias",
        },
        "style_prefix": "limites_provincias_",
        "default_style": "limites_provincias_negro",
        "label_field": "n_prov",
        "unit_name": "provincia",
        "dataset_inspection": {
            "archive_member": "limites_esp_provincias.gpkg",
            "archive_snapshot_sha256": (
                "bc28f9f682bd332212a8eaed6231e562"
                "3cde2984ad28a5c1f1cb5c08bdc52cd5"
            ),
            "archive_snapshot_size_bytes": 14_668_866,
            "data_schema": [
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
                    "declared_type": "TEXT(2)",
                    "default": None,
                    "hidden": 0,
                    "name": "c_tip_ent",
                    "not_null": False,
                    "ordinal": 2,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(9)",
                    "default": None,
                    "hidden": 0,
                    "name": "n_tip_ent",
                    "not_null": False,
                    "ordinal": 3,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(22)",
                    "default": None,
                    "hidden": 0,
                    "name": "n_prov",
                    "not_null": False,
                    "ordinal": 4,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(26)",
                    "default": None,
                    "hidden": 0,
                    "name": "n_auton",
                    "not_null": False,
                    "ordinal": 5,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(2)",
                    "default": None,
                    "hidden": 0,
                    "name": "c_auton",
                    "not_null": False,
                    "ordinal": 6,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(2)",
                    "default": None,
                    "hidden": 0,
                    "name": "c_prov",
                    "not_null": False,
                    "ordinal": 7,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(11)",
                    "default": None,
                    "hidden": 0,
                    "name": "c_ine",
                    "not_null": False,
                    "ordinal": 8,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "REAL",
                    "default": None,
                    "hidden": 0,
                    "name": "m_sup_m",
                    "not_null": False,
                    "ordinal": 9,
                    "primary_key_ordinal": 0,
                },
                {
                    "declared_type": "TEXT(24)",
                    "default": None,
                    "hidden": 0,
                    "name": "inspireid",
                    "not_null": False,
                    "ordinal": 10,
                    "primary_key_ordinal": 0,
                },
            ],
            "data_schema_sha256": (
                "fbcb56c94b112bfadeda2933deb92fdb"
                "f3d207dffb62ad9fe63ad87f26269aee"
            ),
            "declared_bounds": {
                "east": 4.32778473,
                "north": 43.792379568,
                "south": 27.63772315,
                "west": -18.16118085,
            },
            "empty_geometry_count": 0,
            "feature_count": 50,
            "feature_layer": "limites_esp_provincias",
            "feature_layers": ["limites_esp_provincias"],
            "geometry_bounds": {
                "east": 4.32778473,
                "north": 43.792379568,
                "south": 27.63772315,
                "west": -18.16118085,
            },
            "geometry_column": "geometry",
            "geometry_type": "MULTIPOLYGON",
            "geopackage_member_sha256": (
                "716e27c01cf66e5504e98a55c6d291d"
                "8c34662142418082bf057f6b1607f03f0"
            ),
            "inspection_schema": (
                "reference-geopackage-zip-inspection/v1"
            ),
            "sample_sha256": (
                "f1267b64822796d0c1da696cf64aedcc"
                "8abb5aaeb9ec37fd88fed4f5678d39fb"
            ),
            "srs": "EPSG:4258",
        },
    },
}
_EXPECTED_BOUNDARY_EXCLUSIONS = [
    {
        "audit_layer_id": 279,
        "catalog_style_source_keys": [
            "lineas_limite_municipales_ambito",
            "lineas_limite_municipales_azul",
            "lineas_limite_municipales_estadolegal",
            "lineas_limite_municipales_precision",
        ],
        "dataset_archive_sha256": (
            "9164893deaa7a5e5df0c95f2f740aa9"
            "cc1360ea1c0a733cbb310206f3fff0190"
        ),
        "dataset_member_sha256": (
            "37fa0d12f92835ee63804d4b259b4692"
            "6fa4e5d1118eab61b32d22a4a536ee00"
        ),
        "dataset_schema_sha256": (
            "0a40ad194c4a6bd98631f42ed701b2fb"
            "118e2669f9ee91ecb08916106a76aeca"
        ),
        "implementable_style_source_keys": [
            "lineas_limite_municipales_azul"
        ],
        "reason_code": (
            "catalog_thematic_palette_evidence_incomplete_for_full_parity"
        ),
        "unresolved_style_source_keys": [
            "lineas_limite_municipales_ambito",
            "lineas_limite_municipales_estadolegal",
            "lineas_limite_municipales_precision",
        ],
    }
]


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
    by_profile_style: MappingProxyType


def idecyl_local_style_inventory() -> tuple[ReviewedIDECyLLocalStyle, ...]:
    """Return fresh projections of all authored IDECyL style recipes."""

    return tuple(_fresh(item) for item in _committed_package().recipes)


def reviewed_idecyl_local_style(
    profile: str,
    catalog_style_source_key: str | None = None,
) -> ReviewedIDECyLLocalStyle | None:
    """Resolve one unambiguous recipe by profile and optional style key."""

    if catalog_style_source_key is not None:
        item = _committed_package().by_profile_style.get(
            (profile, catalog_style_source_key)
        )
        return _fresh(item) if item is not None else None
    items = _committed_package().by_profile.get(profile, ())
    item = items[0] if len(items) == 1 else None
    return _fresh(item) if item is not None else None


def reviewed_idecyl_local_styles(
    profile: str,
) -> tuple[ReviewedIDECyLLocalStyle, ...]:
    """Resolve every style recipe bound to one exact source profile."""

    return tuple(
        _fresh(item)
        for item in _committed_package().by_profile.get(profile, ())
    )


def reviewed_idecyl_local_style_for_source(
    source: ReviewedIDECyLExactSource,
) -> ReviewedIDECyLLocalStyle | None:
    """Compatibility wrapper for source profiles with exactly one recipe."""

    items = reviewed_idecyl_local_styles_for_source(source)
    if not items:
        return None
    if len(items) != 1:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style source defines multiple recipes"
        )
    return items[0]


def reviewed_idecyl_local_styles_for_source(
    source: ReviewedIDECyLExactSource,
) -> tuple[ReviewedIDECyLLocalStyle, ...]:
    """Resolve recipes only when every source identity still matches."""

    items = reviewed_idecyl_local_styles(source.profile)
    if not items:
        return ()
    item = items[0]
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
    if any(
        candidate.audit_layer_id != item.audit_layer_id
        or candidate.catalog_layer_source_key
        != item.catalog_layer_source_key
        or candidate.catalog_endpoint_url != item.catalog_endpoint_url
        or candidate.catalog_remote_name != item.catalog_remote_name
        or candidate.profile != item.profile
        or candidate.selected_layer_name != item.selected_layer_name
        for candidate in items
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style recipe set is internally inconsistent"
        )
    return items


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
    source_styles = reviewed_idecyl_local_styles(item.profile)
    if not source_styles:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style source recipe set is missing"
        )
    if len(source_styles) == 1:
        config["reviewed_local_style"] = idecyl_local_style_config(
            source_styles[0]
        )
    else:
        config["reviewed_local_styles"] = sorted(
            (
                idecyl_local_style_config(candidate)
                for candidate in source_styles
            ),
            key=lambda value: value["catalog_style_source_key"],
        )
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
        previous_body=_resource_body(PREVIOUS_MANIFEST_RESOURCE),
        expected_sha256=MANIFEST_SHA256,
    )


def _load_evidence_package(
    body: bytes,
    *,
    previous_body: bytes | None = None,
    expected_sha256: str,
) -> _EvidencePackage:
    manifest = _canonical_manifest(body, expected_sha256)
    if previous_body is None:
        previous_body = _resource_body(PREVIOUS_MANIFEST_RESOURCE)
    previous = _canonical_manifest(
        previous_body,
        PREVIOUS_MANIFEST_SHA256,
    )
    if (
        set(manifest)
        != {"schema", "capture", "excluded_layers", "sources"}
        or manifest.get("schema") != MANIFEST_SCHEMA
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style manifest shape is invalid"
        )
    _validate_boundary_capture(manifest.get("capture"))
    if manifest.get("excluded_layers") != _EXPECTED_BOUNDARY_EXCLUSIONS:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style exclusions changed"
        )
    previous_recipes = _parse_previous_manifest(previous)
    raw_sources = manifest.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != 2:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style source inventory changed"
        )
    boundary_recipes = tuple(
        recipe
        for source in raw_sources
        for recipe in _parse_boundary_source(source)
    )
    recipes = tuple(
        sorted(
            (*previous_recipes, *boundary_recipes),
            key=lambda item: (
                item.audit_layer_id,
                item.catalog_style_source_key,
            ),
        )
    )
    grouped: dict[str, list[ReviewedIDECyLLocalStyle]] = {}
    by_profile_style: dict[
        tuple[str, str],
        ReviewedIDECyLLocalStyle,
    ] = {}
    for item in recipes:
        grouped.setdefault(item.profile, []).append(item)
        identity = (item.profile, item.catalog_style_source_key)
        if identity in by_profile_style:
            raise IDECyLLocalStyleEvidenceError(
                "IDECyL local-style profile/style identities are ambiguous"
            )
        by_profile_style[identity] = item
    by_profile = {
        profile: tuple(
            sorted(
                items,
                key=lambda item: item.catalog_style_source_key,
            )
        )
        for profile, items in grouped.items()
    }
    if len(by_profile_style) != len(recipes):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL local-style identities are ambiguous"
        )
    return _EvidencePackage(
        recipes=recipes,
        by_profile=MappingProxyType(by_profile),
        by_profile_style=MappingProxyType(by_profile_style),
    )


def _canonical_manifest(body: bytes, expected_sha256: str) -> dict[str, Any]:
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
    return manifest


def _parse_previous_manifest(
    manifest: dict[str, Any],
) -> tuple[ReviewedIDECyLLocalStyle, ...]:
    if (
        set(manifest)
        != {"schema", "capture", "excluded_priority_layers", "recipes"}
        or manifest.get("schema") != PREVIOUS_MANIFEST_SCHEMA
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL previous local-style manifest shape is invalid"
        )
    _validate_previous_capture(manifest.get("capture"))
    if manifest.get("excluded_priority_layers") != _EXPECTED_EXCLUSIONS:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL previous local-style exclusions changed"
        )
    raw_recipes = manifest.get("recipes")
    if not isinstance(raw_recipes, list) or len(raw_recipes) != 1:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL previous local-style recipe inventory changed"
        )
    return tuple(_parse_recipe(item) for item in raw_recipes)


def _validate_previous_capture(value: Any) -> None:
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


def _validate_boundary_capture(value: Any) -> None:
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
            "previous_manifest",
            "style_capabilities_snapshot_sha256",
            "style_family_sld_snapshot_sha256",
        }
        or value.get("authorization_effect") != AUTHORIZATION_EFFECT
        or value.get("catalog_snapshot_sha256")
        != _EXPECTED_CATALOG_SNAPSHOT_SHA256
        or value.get("exact_source_manifest_resource")
        != EXACT_SOURCE_MANIFEST_RESOURCE
        or value.get("exact_source_manifest_sha256")
        != EXACT_SOURCE_MANIFEST_SHA256
        or value.get("generator_version") != GENERATOR_VERSION
        or value.get("inspected_on") != "2026-07-27"
        or value.get("network_access_during_style_authorship") is not False
        or value.get("previous_manifest")
        != {
            "resource": PREVIOUS_MANIFEST_RESOURCE,
            "sha256": PREVIOUS_MANIFEST_SHA256,
        }
        or value.get("style_capabilities_snapshot_sha256")
        != _EXPECTED_STYLE_CAPABILITIES_SNAPSHOT_SHA256
        or value.get("style_family_sld_snapshot_sha256")
        != _EXPECTED_STYLE_FAMILY_SLD_SNAPSHOT_SHA256
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style capture changed"
        )


def _expected_boundary_styles(
    spec: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for template in _BOUNDARY_STYLE_TEMPLATES:
        style_name = spec["style_prefix"] + template["suffix"]
        labelled = template["labelled"]
        visual: dict[str, Any] = {
            "evidence_basis": {
                "catalog_style_title": template["catalog_title"],
                "geometry_type": "MULTIPOLYGON",
                "label_field": spec["label_field"] if labelled else None,
                "style_family_reference_name": (
                    "limites_municipios_" + template["suffix"]
                ),
                "style_family_sld_snapshot_sha256": (
                    _EXPECTED_STYLE_FAMILY_SLD_SNAPSHOT_SHA256
                ),
                "unit_name": spec["unit_name"],
                "upstream_capabilities_snapshot_sha256": (
                    _EXPECTED_STYLE_CAPABILITIES_SNAPSHOT_SHA256
                ),
                "upstream_style_title": template[
                    "upstream_style_title"
                ],
            },
            "fill_color": "#ffffff",
            "fill_opacity": 0,
            "outline_color": template["outline_color"],
            "outline_width": template["outline_width"],
            "schema": "siur-idecyl-boundary-style/v1",
            "symbolizer": (
                "polygon-and-label" if labelled else "polygon"
            ),
        }
        if labelled:
            visual.update(
                {
                    "font_family": "DejaVu Sans",
                    "font_size": 10,
                    "halo_color": template["halo_color"],
                    "halo_radius": 1,
                    "label_color": template["label_color"],
                    "label_field": spec["label_field"],
                }
            )
        expected[style_name] = {
            "catalog_style": {
                "catalog_style_source_key": style_name,
                "is_default": style_name == spec["default_style"],
                "remote_name": style_name,
                "title": template["catalog_title"],
            },
            "exact_style_claim": False,
            "parity_kind": "adapted",
            "visual_recipe": visual,
        }
    return expected


def _parse_boundary_source(
    value: Any,
) -> tuple[ReviewedIDECyLLocalStyle, ...]:
    expected_keys = {
        "audit_layer_id",
        "catalog_identity",
        "dataset_inspection",
        "profile",
        "source_binding",
        "source_evidence_sha256",
        "styles",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style source shape changed"
        )
    layer_id = value.get("audit_layer_id")
    spec = _BOUNDARY_LAYER_SPECS.get(layer_id)
    if (
        spec is None
        or value.get("profile") != spec["profile"]
        or value.get("catalog_identity") != spec["catalog_identity"]
        or value.get("dataset_inspection")
        != spec["dataset_inspection"]
        or canonical_json_sha256(
            value["dataset_inspection"]["data_schema"]
        )
        != value["dataset_inspection"]["data_schema_sha256"]
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style source evidence changed"
        )
    source_unhashed = {
        key: item
        for key, item in value.items()
        if key not in {"source_evidence_sha256", "styles"}
    }
    source_evidence_sha256 = value.get("source_evidence_sha256")
    if (
        _SHA256_RE.fullmatch(str(source_evidence_sha256)) is None
        or canonical_json_sha256(source_unhashed)
        != source_evidence_sha256
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style source digest changed"
        )
    identity = spec["catalog_identity"]
    source = reviewed_idecyl_exact_source(
        catalog_layer_source_key=identity["catalog_layer_source_key"],
        catalog_endpoint_url=identity["catalog_endpoint_url"],
        catalog_remote_name=identity["catalog_remote_name"],
    )
    if (
        source is None
        or source.audit_layer_id != layer_id
        or source.profile != spec["profile"]
        or source.local_service_status != "candidate"
        or source.candidate_config is None
        or source.candidate_config.get("input_layer")
        != spec["dataset_inspection"]["feature_layer"]
    ):
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style exact source changed"
        )
    _validate_source_binding(value.get("source_binding"), source)
    expected_styles = _expected_boundary_styles(spec)
    raw_styles = value.get("styles")
    if not isinstance(raw_styles, list) or len(raw_styles) != 6:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style inventory changed"
        )
    parsed: list[ReviewedIDECyLLocalStyle] = []
    seen: set[str] = set()
    for raw_style in raw_styles:
        if not isinstance(raw_style, dict):
            raise IDECyLLocalStyleEvidenceError(
                "IDECyL boundary-style recipe shape changed"
            )
        catalog_style = raw_style.get("catalog_style")
        style_key = (
            catalog_style.get("catalog_style_source_key")
            if isinstance(catalog_style, dict)
            else None
        )
        expected_style = expected_styles.get(str(style_key))
        expected_style_keys = {
            "catalog_style",
            "exact_style_claim",
            "parity_kind",
            "recipe_identity_sha256",
            "visual_recipe",
        }
        if (
            expected_style is None
            or style_key in seen
            or set(raw_style) != expected_style_keys
            or {
                key: item
                for key, item in raw_style.items()
                if key != "recipe_identity_sha256"
            }
            != expected_style
        ):
            raise IDECyLLocalStyleEvidenceError(
                "IDECyL boundary-style recipe changed"
            )
        recipe_unhashed = {
            "source_evidence_sha256": source_evidence_sha256,
            **expected_style,
        }
        recipe_identity_sha256 = raw_style.get(
            "recipe_identity_sha256"
        )
        if (
            _SHA256_RE.fullmatch(str(recipe_identity_sha256)) is None
            or canonical_json_sha256(recipe_unhashed)
            != recipe_identity_sha256
        ):
            raise IDECyLLocalStyleEvidenceError(
                "IDECyL boundary-style recipe digest changed"
            )
        evidence = {
            "audit_layer_id": layer_id,
            "catalog_identity": deepcopy(identity),
            "catalog_style": deepcopy(catalog_style),
            "dataset_inspection": deepcopy(
                spec["dataset_inspection"]
            ),
            "exact_style_claim": False,
            "parity_kind": "adapted",
            "profile": spec["profile"],
            "recipe_identity_sha256": recipe_identity_sha256,
            "source_binding": deepcopy(value["source_binding"]),
            "source_evidence_sha256": source_evidence_sha256,
            "visual_recipe": deepcopy(raw_style["visual_recipe"]),
        }
        parsed.append(
            ReviewedIDECyLLocalStyle(
                audit_layer_id=layer_id,
                profile=spec["profile"],
                catalog_layer_source_key=identity[
                    "catalog_layer_source_key"
                ],
                catalog_endpoint_url=identity[
                    "catalog_endpoint_url"
                ],
                catalog_remote_name=identity["catalog_remote_name"],
                catalog_style_source_key=style_key,
                remote_style_name=catalog_style["remote_name"],
                style_title=catalog_style["title"],
                is_default=catalog_style["is_default"],
                selected_layer_name=source.candidate_config[
                    "input_layer"
                ],
                recipe_identity_sha256=recipe_identity_sha256,
                evidence=evidence,
            )
        )
        seen.add(style_key)
    if seen != set(expected_styles) or sum(
        item.is_default for item in parsed
    ) != 1:
        raise IDECyLLocalStyleEvidenceError(
            "IDECyL boundary-style catalog coverage changed"
        )
    return tuple(parsed)


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
