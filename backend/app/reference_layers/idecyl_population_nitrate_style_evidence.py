"""Closed style evidence for IDECyL population and nitrate archives.

The population ZIP contains one generic SLD which is not any of the six
catalog styles, so it remains an explicit exclusion.  The nitrate ZIP contains
the exact 2021 SLD and all fifteen historical value columns.  This module
allows a normalized SLD 1.0 rendering whose only classification-rule semantic
change is the year field for 2006--2020; the 2021 catalog style remains the
exact embedded artifact.
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
from typing import Any, Mapping

from app.reference_layers.idecyl_archive_style_evidence import (
    MANIFEST_RESOURCE as ARCHIVE_STYLE_MANIFEST_RESOURCE,
    MANIFEST_SHA256 as ARCHIVE_STYLE_MANIFEST_SHA256,
)
from app.reference_layers.idecyl_exact_evidence import (
    MANIFEST_RESOURCE as EXACT_SOURCE_MANIFEST_RESOURCE,
    MANIFEST_SHA256 as EXACT_SOURCE_MANIFEST_SHA256,
    ReviewedIDECyLExactSource,
    reviewed_idecyl_exact_source,
)


MANIFEST_SCHEMA = "siur-idecyl-population-nitrate-style-evidence/v1"
MANIFEST_RESOURCE = (
    "evidence/idecyl_population_nitrate_styles/manifest-v1.json"
)
MANIFEST_SHA256 = (
    "77bf0b1952b6ec9969b0b09073bf51e2"
    "511d76b7e0a25d6e48621afe731723ec"
)
EVIDENCE_SCHEMA = "siur-reviewed-idecyl-temporal-style/v1"
STYLE_CONFIG_SCHEMA = "siur-reviewed-idecyl-local-style/v1"
EXCLUSION_CONFIG_SCHEMA = (
    "siur-reviewed-idecyl-local-style-exclusion/v1"
)
GENERATOR_VERSION = (
    "siur-sld-1.0-idecyl-temporal-semantic-adaptation/v1"
)
AUTHORIZATION_EFFECT = "none_without_persisted_human_mirror_review"
SOURCE_PRIORITY = 5
MAX_MANIFEST_BYTES = 64 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_NITRATE_LAYER_ID = 268
_POPULATION_LAYER_ID = 237
_NITRATE_YEARS = tuple(range(2006, 2021))
_NITRATE_PROFILE = (
    "idecyl-coad-cyl-nitrat-aguas-subterr-archive-20260727-v3"
)
_POPULATION_PROFILE = (
    "idecyl-nucleos-cyl-poblaciones-archive-20260727-v3"
)
_EXPECTED_NITRATE_ARCHIVE_SHA256 = (
    "36a0af1a234321707bbbb3722b7aefcac"
    "9228872366f9851e0d1acfd51faa538"
)
_EXPECTED_NITRATE_MEMBER_SHA256 = (
    "48cf8c88defef0a6ca90bf9132258ca8"
    "a454612063e29b92c1dbbb51dd004e7a"
)
_EXPECTED_NITRATE_SCHEMA_SHA256 = (
    "c4adff9e98640a2207bef95b6d9ce01"
    "b6c036b353466e576139b1e5bbf4b604c"
)
_EXPECTED_NITRATE_SAMPLE_SHA256 = (
    "7b1cec9ebe7c6b3a1a1b192a118396b"
    "48204eb0eeeebc8c55ef2e0ac4702f36b"
)
_EXPECTED_TEMPLATE_SHA256 = (
    "ed4dff6410973ffd0e672a1a4216a7d"
    "11dab2118bc3b0a6056b2d1cfc2b87c38"
)
_EXPECTED_SERIES_SHA256 = (
    "237fd480cafaabfbdab053f6b8741c9"
    "b1edb64833b32f5d034fe1149626b3230"
)
_EXPECTED_POPULATION_EXCLUSION_SHA256 = (
    "2468c24e3589efc6a755c7d15202a078"
    "602a412fe6f89dba37716e8fec903466"
)
_EXPECTED_POPULATION_SLD_SHA256 = (
    "67fabe03db7b2ad98d4916e965791f78"
    "092a178911a181c130099d71716da21d"
)
_EXPECTED_RULES = [
    {
        "fill_color": "#cccccc",
        "fill_opacity": 0.3,
        "filter": {"operator": "is_null"},
        "name": "Sin datos",
        "stroke_color": "#6e6e6e",
        "stroke_linejoin": "bevel",
        "stroke_width": 0.5,
    },
    {
        "fill_color": "#08bd08",
        "fill_opacity": 0.75,
        "filter": {
            "lower": 0,
            "lower_operator": "greater_than",
            "operator": "range",
            "upper": 25,
            "upper_operator": "less_than",
        },
        "name": "0-25",
        "stroke_color": "#6e6e6e",
        "stroke_linejoin": "bevel",
        "stroke_width": 0.5,
    },
    {
        "fill_color": "#ffff00",
        "fill_opacity": 0.75,
        "filter": {
            "lower": 25,
            "lower_operator": "greater_than_or_equal",
            "operator": "range",
            "upper": 30,
            "upper_operator": "less_than",
        },
        "name": "25-30",
        "stroke_color": "#6e6e6e",
        "stroke_linejoin": "bevel",
        "stroke_width": 0.5,
    },
    {
        "fill_color": "#ff9900",
        "fill_opacity": 0.75,
        "filter": {
            "lower": 30,
            "lower_operator": "greater_than_or_equal",
            "operator": "range",
            "upper": 37.5,
            "upper_operator": "less_than",
        },
        "name": "30-37.5",
        "stroke_color": "#6e6e6e",
        "stroke_linejoin": "bevel",
        "stroke_width": 0.5,
    },
    {
        "fill_color": "#ff0000",
        "fill_opacity": 0.75,
        "filter": {
            "lower": 37.5,
            "lower_operator": "greater_than_or_equal",
            "operator": "lower_bound",
        },
        "name": "> 37,5",
        "stroke_color": "#6e6e6e",
        "stroke_linejoin": "bevel",
        "stroke_width": 0.5,
    },
]


class IDECyLPopulationNitrateStyleEvidenceError(RuntimeError):
    """Committed population/nitrate evidence is altered or inconsistent."""


@dataclass(frozen=True)
class ReviewedIDECyLNitrateStyle:
    """One normalized historical style derived from 2021 rule semantics."""

    audit_layer_id: int
    profile: str
    style_kind: str
    catalog_layer_source_key: str
    catalog_endpoint_url: str
    catalog_remote_name: str
    catalog_style_source_key: str
    remote_style_name: str
    style_title: str
    is_default: bool
    selected_layer_name: str
    year: int
    property_name: str
    recipe_identity_sha256: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _EvidencePackage:
    styles: tuple[ReviewedIDECyLNitrateStyle, ...]
    population_exclusion: MappingProxyType


def idecyl_nitrate_style_inventory() -> tuple[ReviewedIDECyLNitrateStyle, ...]:
    return tuple(_fresh(item) for item in _committed_package().styles)


def reviewed_idecyl_nitrate_styles(
    profile: str,
) -> tuple[ReviewedIDECyLNitrateStyle, ...]:
    if profile != _NITRATE_PROFILE:
        return ()
    return idecyl_nitrate_style_inventory()


def reviewed_idecyl_nitrate_styles_for_source(
    source: ReviewedIDECyLExactSource,
) -> tuple[ReviewedIDECyLNitrateStyle, ...]:
    styles = reviewed_idecyl_nitrate_styles(source.profile)
    if not styles:
        return ()
    first = styles[0]
    if (
        source.audit_layer_id != _NITRATE_LAYER_ID
        or source.catalog_layer_source_key != first.catalog_layer_source_key
        or source.catalog_endpoint_url != first.catalog_endpoint_url
        or source.catalog_remote_name != first.catalog_remote_name
        or source.local_service_status != "candidate"
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate source identity changed"
        )
    return styles


def idecyl_population_style_exclusion() -> dict[str, Any]:
    return deepcopy(dict(_committed_package().population_exclusion))


def reviewed_idecyl_population_style_exclusion(
    profile: str,
) -> dict[str, Any] | None:
    if profile != _POPULATION_PROFILE:
        return None
    return idecyl_population_style_exclusion()


def reviewed_idecyl_population_style_exclusion_for_source(
    source: ReviewedIDECyLExactSource,
) -> dict[str, Any] | None:
    exclusion = reviewed_idecyl_population_style_exclusion(source.profile)
    if exclusion is None:
        return None
    catalog = exclusion["catalog_identity"]
    if (
        source.audit_layer_id != _POPULATION_LAYER_ID
        or source.catalog_layer_source_key
        != catalog["catalog_layer_source_key"]
        or source.catalog_endpoint_url != catalog["catalog_endpoint_url"]
        or source.catalog_remote_name != catalog["catalog_remote_name"]
        or source.local_service_status != "candidate"
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population exclusion source identity changed"
        )
    return exclusion


def idecyl_population_style_exclusion_config(
    exclusion: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        exclusion.get("profile") != _POPULATION_PROFILE
        or exclusion.get("audit_layer_id") != _POPULATION_LAYER_ID
        or exclusion.get("local_service_eligible") is not False
        or exclusion.get("exclusion_identity_sha256")
        != _EXPECTED_POPULATION_EXCLUSION_SHA256
        or not isinstance(exclusion.get("reason_codes"), list)
        or not isinstance(exclusion.get("catalog_styles"), list)
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population exclusion configuration changed"
        )
    return {
        "schema": EXCLUSION_CONFIG_SCHEMA,
        "audit_layer_id": _POPULATION_LAYER_ID,
        "profile": _POPULATION_PROFILE,
        "exclusion_identity_sha256": (
            _EXPECTED_POPULATION_EXCLUSION_SHA256
        ),
        "local_service_eligible": False,
        "catalog_style_count": len(exclusion["catalog_styles"]),
        "reason_codes": deepcopy(exclusion["reason_codes"]),
    }


def idecyl_population_expected_source_definition(
    exclusion: Mapping[str, Any],
) -> dict[str, Any]:
    source = _source_from_catalog(exclusion.get("catalog_identity"))
    if source.audit_layer_id != _POPULATION_LAYER_ID:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population exclusion source changed"
        )
    config = deepcopy(source.candidate_config)
    if config is None:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population source configuration is missing"
        )
    config.update(
        {
            "reviewed_equivalence": deepcopy(source.evidence),
            "reviewed_local_style_exclusion": (
                idecyl_population_style_exclusion_config(exclusion)
            ),
        }
    )
    return {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": SOURCE_PRIORITY,
        "config": config,
    }


def idecyl_nitrate_style_config(
    item: ReviewedIDECyLNitrateStyle,
) -> dict[str, Any]:
    return {
        "schema": STYLE_CONFIG_SCHEMA,
        "audit_layer_id": item.audit_layer_id,
        "profile": item.profile,
        "recipe_identity_sha256": item.recipe_identity_sha256,
        "catalog_style_source_key": item.catalog_style_source_key,
        "remote_name": item.remote_style_name,
        "is_default": item.is_default,
    }


def idecyl_nitrate_expected_source_definition(
    item: ReviewedIDECyLNitrateStyle,
) -> dict[str, Any]:
    """Build the only source definition accepted for the mixed style set."""

    source = _exact_source(item)
    styles = reviewed_idecyl_nitrate_styles(item.profile)
    if item.catalog_style_source_key not in {
        style.catalog_style_source_key for style in styles
    }:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate style is not in its reviewed series"
        )
    config = deepcopy(source.candidate_config)
    archive = source.evidence.get("archive_style_evidence")
    if config is None or not isinstance(archive, dict):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate source lacks its exact archive style"
        )
    exact_styles = archive.get("archive_styles")
    catalog_styles = archive.get("catalog_styles")
    capture = archive.get("archive_capture")
    if (
        not isinstance(exact_styles, list)
        or len(exact_styles) != 1
        or exact_styles[0].get("catalog_style_source_key")
        != "coad_cyl_nitrat_aguas_subterr_2021"
        or not isinstance(catalog_styles, list)
        or not isinstance(capture, dict)
        or capture.get("archive_sha256")
        != _EXPECTED_NITRATE_ARCHIVE_SHA256
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate exact style evidence changed"
        )
    adapted_keys = {
        style.catalog_style_source_key for style in styles
    }
    catalog_keys = {
        style.get("catalog_style_source_key") for style in catalog_styles
    }
    if adapted_keys | {
        exact_styles[0]["catalog_style_source_key"]
    } != catalog_keys:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate combined style coverage is incomplete"
        )
    config.update(
        {
            "archive_styles": deepcopy(exact_styles),
            "archive_style_catalog": deepcopy(catalog_styles),
            "reviewed_local_styles": [
                idecyl_nitrate_style_config(style) for style in styles
            ],
            "reviewed_equivalence": deepcopy(source.evidence),
        }
    )
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
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL style evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(body).hexdigest()


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
        or _SHA256_RE.fullmatch(str(expected_sha256)) is None
        or hashlib.sha256(body).hexdigest() != expected_sha256
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population/nitrate manifest failed its digest"
        )
    manifest = _json_object(body)
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
        body != canonical
        or set(manifest)
        != {"schema", "capture", "nitrate_series", "population_exclusion"}
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("capture") != _expected_capture()
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population/nitrate manifest shape changed"
        )
    styles = _parse_nitrate_series(manifest.get("nitrate_series"))
    exclusion = _parse_population_exclusion(
        manifest.get("population_exclusion")
    )
    return _EvidencePackage(
        styles=styles,
        population_exclusion=MappingProxyType(exclusion),
    )


def _expected_capture() -> dict[str, Any]:
    return {
        "archive_style_manifest_resource": ARCHIVE_STYLE_MANIFEST_RESOURCE,
        "archive_style_manifest_sha256": ARCHIVE_STYLE_MANIFEST_SHA256,
        "authorization_effect": AUTHORIZATION_EFFECT,
        "catalog_snapshot_sha256": (
            "950eb0dbfb9226a39257ca61d27ef815"
            "353bcad76d00c5c18348489480ba53fc"
        ),
        "exact_source_manifest_resource": EXACT_SOURCE_MANIFEST_RESOURCE,
        "exact_source_manifest_sha256": EXACT_SOURCE_MANIFEST_SHA256,
        "generator_version": GENERATOR_VERSION,
        "inspected_on": "2026-07-27",
        "network_access_during_style_authorship": False,
    }


def _parse_nitrate_series(
    value: Any,
) -> tuple[ReviewedIDECyLNitrateStyle, ...]:
    expected_keys = {
        "adapted_catalog_styles",
        "audit_layer_id",
        "catalog_identity",
        "dataset_inspection",
        "exact_archive_style",
        "exact_style_claim",
        "parity_kind",
        "profile",
        "series_identity_sha256",
        "source_binding",
        "style_template",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value.get("audit_layer_id") != _NITRATE_LAYER_ID
        or value.get("profile") != _NITRATE_PROFILE
        or value.get("parity_kind") != "adapted"
        or value.get("exact_style_claim") is not False
        or value.get("series_identity_sha256") != _EXPECTED_SERIES_SHA256
        or canonical_json_sha256(
            {
                key: item
                for key, item in value.items()
                if key != "series_identity_sha256"
            }
        )
        != _EXPECTED_SERIES_SHA256
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate series identity changed"
        )
    source = _source_from_catalog(value.get("catalog_identity"))
    _validate_source_binding(value.get("source_binding"), source)
    dataset = value.get("dataset_inspection")
    _validate_nitrate_dataset(dataset)
    template = value.get("style_template")
    _validate_nitrate_template(template, source)
    exact = value.get("exact_archive_style")
    if exact != {
        "catalog_style_source_key": (
            "coad_cyl_nitrat_aguas_subterr_2021"
        ),
        "is_default": True,
        "remote_name": "coad_cyl_nitrat_aguas_subterr_2021",
        "title": "Recintos municipales 2021 paleta color",
    }:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate exact style identity changed"
        )
    raw_styles = value.get("adapted_catalog_styles")
    if not isinstance(raw_styles, list) or len(raw_styles) != 15:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate adapted style inventory is incomplete"
        )
    result: list[ReviewedIDECyLNitrateStyle] = []
    for year, style in zip(_NITRATE_YEARS, raw_styles, strict=True):
        expected_style = {
            "catalog_style_source_key": (
                f"coad_cyl_nitrat_aguas_subterr_{year}"
            ),
            "is_default": False,
            "property_name": f"v_nitr{year}",
            "remote_name": f"coad_cyl_nitrat_aguas_subterr_{year}",
            "title": f"Recintos municipales {year} paleta color",
            "year": year,
        }
        if style != expected_style:
            raise IDECyLPopulationNitrateStyleEvidenceError(
                "IDECyL nitrate catalog style identity changed"
            )
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "audit_layer_id": _NITRATE_LAYER_ID,
            "profile": _NITRATE_PROFILE,
            "catalog_identity": deepcopy(value["catalog_identity"]),
            "catalog_style": deepcopy(style),
            "dataset_inspection": deepcopy(dataset),
            "source_binding": deepcopy(value["source_binding"]),
            "style_template": deepcopy(template),
            "temporal_adaptation": {
                "schema": (
                    "siur-idecyl-temporal-rule-semantic-adaptation/v1"
                ),
                "source_property_name": "v_nitr2021",
                "target_property_name": style["property_name"],
                "classification_rule_semantics_preserved": True,
                "property_name_is_only_rule_semantic_change": True,
                "runtime_sld_version": "1.0.0",
                "catalog_style_identity_applied": True,
                "source_descriptions_omitted": True,
                "year": year,
            },
            "parity_kind": "adapted",
            "exact_style_claim": False,
            "series_identity_sha256": _EXPECTED_SERIES_SHA256,
        }
        recipe_identity = canonical_json_sha256(evidence)
        evidence["recipe_identity_sha256"] = recipe_identity
        result.append(
            ReviewedIDECyLNitrateStyle(
                audit_layer_id=_NITRATE_LAYER_ID,
                profile=_NITRATE_PROFILE,
                style_kind="idecyl_nitrate_year",
                catalog_layer_source_key=source.catalog_layer_source_key,
                catalog_endpoint_url=source.catalog_endpoint_url,
                catalog_remote_name=source.catalog_remote_name,
                catalog_style_source_key=style[
                    "catalog_style_source_key"
                ],
                remote_style_name=style["remote_name"],
                style_title=style["title"],
                is_default=False,
                selected_layer_name=source.candidate_config["input_layer"],
                year=year,
                property_name=style["property_name"],
                recipe_identity_sha256=recipe_identity,
                evidence=evidence,
            )
        )
    return tuple(result)


def _validate_nitrate_dataset(value: Any) -> None:
    if not isinstance(value, dict):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate dataset inspection is missing"
        )
    schema = value.get("data_schema")
    names = [
        item.get("name") for item in schema if isinstance(item, dict)
    ] if isinstance(schema, list) else []
    if (
        value.get("inspection_schema")
        != "reference-geopackage-zip-inspection/v1"
        or value.get("archive_snapshot_sha256")
        != _EXPECTED_NITRATE_ARCHIVE_SHA256
        or value.get("archive_snapshot_size_bytes") != 5_652_120
        or value.get("archive_member")
        != "coad_cyl_nitrat_aguas_subterr.gpkg"
        or value.get("geopackage_member_sha256")
        != _EXPECTED_NITRATE_MEMBER_SHA256
        or value.get("data_schema_sha256")
        != _EXPECTED_NITRATE_SCHEMA_SHA256
        or canonical_json_sha256(schema)
        != _EXPECTED_NITRATE_SCHEMA_SHA256
        or names
        != [
            "id",
            "geometry",
            "fid",
            "n_mun",
            "n_prov",
            "c_prov_mun",
            "n_comar_ag",
            "c_comar_ag",
            *(f"v_nitr{year}" for year in range(2006, 2022)),
        ]
        or value.get("feature_count") != 2_298
        or value.get("feature_layer")
        != "coad_cyl_nitrat_aguas_subterr"
        or value.get("feature_layers")
        != ["coad_cyl_nitrat_aguas_subterr"]
        or value.get("geometry_column") != "geometry"
        or value.get("geometry_type") != "MULTIPOLYGON"
        or value.get("empty_geometry_count") != 0
        or value.get("sample_sha256")
        != _EXPECTED_NITRATE_SAMPLE_SHA256
        or value.get("srs") != "EPSG:25830"
        or value.get("declared_bounds") != value.get("geometry_bounds")
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate dataset inspection changed"
        )


def _validate_nitrate_template(
    value: Any,
    source: ReviewedIDECyLExactSource,
) -> None:
    archive = source.evidence.get("archive_style_evidence")
    exact = (
        archive.get("archive_styles", [None])[0]
        if isinstance(archive, dict)
        else None
    )
    if (
        not isinstance(value, dict)
        or value.get("adaptation_contract")
        != {
            "catalog_style_identity_applied": True,
            "classification_rule_semantics_preserved": True,
            "property_name_is_only_rule_semantic_change": True,
            "runtime_sld_version": "1.0.0",
            "source_descriptions_omitted": True,
            "source_sld_version": "1.1.0",
        }
        or value.get("archive_member")
        != "coad_cyl_nitrat_aguas_subterr.sld"
        or value.get("archive_member_crc32") != "e91a0f43"
        or value.get("archive_member_sha256") != _EXPECTED_TEMPLATE_SHA256
        or value.get("archive_member_size_bytes") != 6_693
        or value.get("classification_rules") != _EXPECTED_RULES
        or value.get("sld_layer_name")
        != "coad_cyl_nitrat_aguas_subterr"
        or value.get("sld_style_name")
        != "coad_cyl_nitrat_aguas_subterr"
        or value.get("template_catalog_style_source_key")
        != "coad_cyl_nitrat_aguas_subterr_2021"
        or value.get("template_property_name") != "v_nitr2021"
        or not isinstance(exact, dict)
        or exact.get("archive_member") != value.get("archive_member")
        or exact.get("sha256") != value.get("archive_member_sha256")
        or exact.get("size_bytes") != value.get("archive_member_size_bytes")
        or exact.get("crc32") != value.get("archive_member_crc32")
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate SLD template changed"
        )


def _parse_population_exclusion(value: Any) -> dict[str, Any]:
    embedded_sld = (
        value.get("embedded_sld")
        if isinstance(value, dict)
        else None
    )
    dataset = (
        value.get("dataset_inspection")
        if isinstance(value, dict)
        else None
    )
    if (
        not isinstance(value, dict)
        or not isinstance(embedded_sld, dict)
        or not isinstance(dataset, dict)
        or value.get("audit_layer_id") != _POPULATION_LAYER_ID
        or value.get("local_service_eligible") is not False
        or value.get("exclusion_identity_sha256")
        != _EXPECTED_POPULATION_EXCLUSION_SHA256
        or canonical_json_sha256(
            {
                key: item
                for key, item in value.items()
                if key != "exclusion_identity_sha256"
            }
        )
        != _EXPECTED_POPULATION_EXCLUSION_SHA256
        or embedded_sld.get("archive_member_sha256")
        != _EXPECTED_POPULATION_SLD_SHA256
        or dataset.get("data_schema_sha256")
        != (
            "214ba5474e319515a343587a3df4b9cf"
            "e87a25a89ea0996970bd8a32b9ea5bcd"
        )
        or value.get("reason_codes")
        != [
            "embedded_sld_has_no_catalog_style_identity",
            "embedded_sld_uses_unlisted_two_category_palette",
            "catalog_gray_and_label_variants_not_locally_evidenced",
            "complete_catalog_style_coverage_unproven",
        ]
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population exclusion changed"
        )
    source = _source_from_catalog(value.get("catalog_identity"))
    if source.audit_layer_id != _POPULATION_LAYER_ID:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population exclusion source changed"
        )
    _validate_source_binding(value.get("source_binding"), source)
    styles = value.get("catalog_styles")
    if (
        not isinstance(styles, list)
        or len(styles) != 6
        or not all(isinstance(item, dict) for item in styles)
        or sum(item.get("is_default") is True for item in styles) != 1
        or [item.get("catalog_style_source_key") for item in styles]
        != sorted(item.get("catalog_style_source_key") for item in styles)
        or {
            item.get("catalog_style_source_key") for item in styles
        }
        & {
            value["embedded_sld"]["sld_style_name"],
        }
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population catalog coverage evidence changed"
        )
    return deepcopy(value)


def _source_from_catalog(value: Any) -> ReviewedIDECyLExactSource:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "catalog_layer_source_key",
            "catalog_endpoint_url",
            "catalog_remote_name",
        }
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL catalog identity is invalid"
        )
    source = reviewed_idecyl_exact_source(
        catalog_layer_source_key=value["catalog_layer_source_key"],
        catalog_endpoint_url=value["catalog_endpoint_url"],
        catalog_remote_name=value["catalog_remote_name"],
    )
    if (
        source is None
        or source.local_service_status != "candidate"
        or source.candidate_config is None
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL exact source is unavailable"
        )
    return source


def _validate_source_binding(
    value: Any,
    source: ReviewedIDECyLExactSource,
) -> None:
    config = deepcopy(source.candidate_config)
    if config is None:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL source configuration is missing"
        )
    config["reviewed_equivalence"] = deepcopy(source.evidence)
    definition = {
        "protocol": source.protocol,
        "target_kind": source.target_kind,
        "endpoint_url": source.endpoint_url,
        "remote_name": source.remote_name,
        "sync_strategy": source.sync_strategy,
        "priority": SOURCE_PRIORITY,
        "config": config,
    }
    expected = {
        "base_source_definition_sha256": canonical_json_sha256(definition),
        "reviewed_equivalence_sha256": canonical_json_sha256(
            source.evidence
        ),
        "selected_endpoint_url": source.endpoint_url,
        "selected_remote_name": source.remote_name,
    }
    if source.audit_layer_id == _NITRATE_LAYER_ID:
        expected.update(
            {
                "data_format": source.candidate_config["data_format"],
                "selected_protocol": source.protocol,
                "selected_sync_strategy": source.sync_strategy,
                "selected_target_kind": source.target_kind,
            }
        )
    if value != expected:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population/nitrate source binding changed"
        )


def _exact_source(
    item: ReviewedIDECyLNitrateStyle,
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
        or source.candidate_config is None
        or source.protocol != "download"
        or source.target_kind != "vector"
        or source.sync_strategy != "conditional_get"
    ):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL nitrate exact source changed"
        )
    return source


def _fresh(
    item: ReviewedIDECyLNitrateStyle,
) -> ReviewedIDECyLNitrateStyle:
    return ReviewedIDECyLNitrateStyle(
        audit_layer_id=item.audit_layer_id,
        profile=item.profile,
        style_kind=item.style_kind,
        catalog_layer_source_key=item.catalog_layer_source_key,
        catalog_endpoint_url=item.catalog_endpoint_url,
        catalog_remote_name=item.catalog_remote_name,
        catalog_style_source_key=item.catalog_style_source_key,
        remote_style_name=item.remote_style_name,
        style_title=item.style_title,
        is_default=item.is_default,
        selected_layer_name=item.selected_layer_name,
        year=item.year,
        property_name=item.property_name,
        recipe_identity_sha256=item.recipe_identity_sha256,
        evidence=deepcopy(item.evidence),
    )


def _json_object(body: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise IDECyLPopulationNitrateStyleEvidenceError(
                    "IDECyL manifest has duplicate keys"
                )
            result[key] = item
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(item)
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL manifest is not strict UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL manifest is not an object"
        )
    return value


def _resource_body(resource: str) -> bytes:
    try:
        return files("app.reference_layers").joinpath(resource).read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise IDECyLPopulationNitrateStyleEvidenceError(
            "IDECyL population/nitrate manifest is unavailable"
        ) from error
