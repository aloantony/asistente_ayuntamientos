"""Hash-bound IGN PNOA Histórico substitutions for SIUR ortho layers.

The SIUR catalog still points at twenty ITACyL WMS layers.  This module does
not infer equivalence from their names.  It loads one committed profile and
the exact ITACyL and IGN GetCapabilities snapshots reviewed on 2026-07-27,
verifies all three documents byte-for-byte, and exposes only the twenty
explicit mappings.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import re
from typing import Any
import unicodedata
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree


PROFILE_SCHEMA = "siur-reviewed-ign-pnoa-historico-profile/v1"
EQUIVALENCE_SCHEMA = "siur-reviewed-ign-pnoa-historico-equivalence/v1"
PUBLIC_SUBSTITUTION_SCHEMA = "siur-public-ortho-substitution/v1"
PROFILE_RESOURCE = (
    "evidence/ign_pnoa_historico/equivalence-profile-v1.json"
)
CAPABILITIES_RESOURCE = (
    "evidence/ign_pnoa_historico/capabilities-20260727.xml"
)
CATALOG_CAPABILITIES_RESOURCE = (
    "evidence/ign_pnoa_historico/itacyl-capabilities-20260727.xml"
)
PROFILE_SHA256 = (
    "285037c16462406560a71c738fca14c2"
    "402e76f0159d16e9a608974ee5e396dd"
)
CAPABILITIES_SHA256 = (
    "1a0fede7e1d1bfd2746656b7b2e731df"
    "6376e5c42f55d8f7c70cf3b1b5e9c8e8"
)
CATALOG_CAPABILITIES_SHA256 = (
    "70f38ef5aee1e78cdc2ac64985e804a7"
    "b3f38ab37e13bc435c093c69bbac98ff"
)
CATALOG_ENDPOINT_URL = "https://orto.wms.itacyl.es/WMS"
CATALOG_CAPABILITIES_URL = (
    "https://orto.wms.itacyl.es/WMS"
    "?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0"
)
SELECTED_ENDPOINT_URL = "https://www.ign.es/wms/pnoa-historico"
CAPABILITIES_URL = (
    "https://www.ign.es/wms/pnoa-historico"
    "?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0"
)
LICENSE_URL = (
    "https://www.ign.es/resources/licencia/Condiciones_licenciaUso_IGN.pdf"
)
REQUIRED_ATTRIBUTION = (
    "Sistema Cartográfico Nacional · "
    "Instituto Geográfico Nacional de España"
)
SOURCE_PRIORITY = 5
MAX_PROFILE_BYTES = 128 * 1024
MAX_CAPABILITIES_BYTES = 512 * 1024

_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "audit_date",
        "catalog_identity",
        "catalog_capabilities_snapshot",
        "selected_source",
        "capabilities_snapshot",
        "license_evidence",
        "operational_profile",
        "mappings",
    }
)
_MAPPING_KEYS = frozenset(
    {
        "profile",
        "catalog_layer",
        "selected_layer",
        "selected_title",
        "selected_abstract",
        "metadata_record_id",
        "metadata_url",
        "equivalence_status",
        "public_notice",
        "parity_requirements",
    }
)
_EXPECTED_CATALOG_LAYERS = frozenset(
    {
        "Ortofoto_1956",
        "Ortofoto_1973-83",
        "Ortofoto_1997",
        "Ortofoto_1999",
        "Ortofoto_2000",
        "Ortofoto_2001",
        "Ortofoto_2002",
        "Ortofoto_2004",
        "Ortofoto_2005",
        "Ortofoto_2006",
        "Ortofoto_2007",
        "Ortofoto_2008",
        "Ortofoto_2009",
        "Ortofoto_2010",
        "Ortofoto_2011",
        "Ortofoto_2014",
        "Ortofoto_2017",
        "Ortofoto_2020",
        "Ortofoto_2021",
        "Ortofoto_2023",
    }
)
_EXACT_CATALOG_LAYERS = frozenset(
    {
        "Ortofoto_2020",
        "Ortofoto_2017",
        "Ortofoto_2014",
        "Ortofoto_2010",
        "Ortofoto_2006",
        "Ortofoto_2005",
    }
)
_EXPECTED_OPERATIONAL_PROFILE = {
    "bounds": {
        "west": -7.6,
        "south": 39.9,
        "east": -1.3,
        "north": 43.4,
    },
    "min_zoom": 0,
    "max_zoom": 15,
    "coverage_profile": "siur-castilla-y-leon-ortho-native-z15-v1",
    "wms_supertile_size": 8,
    "max_tile_count": 2_000_000,
}
_WMS_NS = {"wms": "http://www.opengis.net/wms"}
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


class ReviewedOrthoEvidenceError(RuntimeError):
    """Committed ortho evidence is missing, changed or internally invalid."""


@dataclass(frozen=True)
class ReviewedIgnOrthoSubstitution:
    """One exact catalog identity and its reviewed IGN source mapping."""

    profile: str
    catalog_endpoint_url: str
    catalog_layer: str
    catalog_title: str
    catalog_abstract: str
    catalog_capabilities_sha256: str
    selected_endpoint_url: str
    selected_layer: str
    selected_title: str
    selected_abstract: str
    metadata_record_id: str
    capabilities_url: str
    capabilities_sha256: str
    evidence_profile_sha256: str
    image_format: str
    style_name: str
    equivalence_status: str
    comparison_basis: str
    declared_coverage: str
    catalog_declared_resolutions_metres: tuple[float, ...]
    declared_resolutions_metres: tuple[float, ...]
    promotion_eligible: bool
    public_notice: str
    parity_requirements: tuple[str, ...]
    license_name: str
    license_url: str
    required_attribution: str
    bounds: dict[str, float]
    min_zoom: int
    max_zoom: int
    coverage_profile: str
    wms_supertile_size: int
    max_tile_count: int


def reviewed_ign_ortho_substitution(
    catalog_endpoint_url: str,
    catalog_layer: str,
) -> ReviewedIgnOrthoSubstitution | None:
    """Return a mapping only for one exact reviewed ITACyL identity."""

    if catalog_endpoint_url != CATALOG_ENDPOINT_URL:
        return None
    return _reviewed_substitutions().get(catalog_layer)


def reviewed_ign_ortho_equivalence(
    reviewed: ReviewedIgnOrthoSubstitution,
) -> dict[str, Any]:
    """Return the exact hash-bound source configuration evidence."""

    return {
        "schema": EQUIVALENCE_SCHEMA,
        "profile": reviewed.profile,
        "evidence_profile_sha256": reviewed.evidence_profile_sha256,
        "capabilities_sha256": reviewed.capabilities_sha256,
        "catalog_capabilities_sha256": (
            reviewed.catalog_capabilities_sha256
        ),
        "capabilities_url": reviewed.capabilities_url,
        "license_name": reviewed.license_name,
        "license_url": reviewed.license_url,
        "required_attribution": reviewed.required_attribution,
        "catalog_layer": reviewed.catalog_layer,
        "catalog_title": reviewed.catalog_title,
        "catalog_abstract_sha256": hashlib.sha256(
            reviewed.catalog_abstract.encode("utf-8")
        ).hexdigest(),
        "selected_layer": reviewed.selected_layer,
        "selected_title": reviewed.selected_title,
        "metadata_record_id": reviewed.metadata_record_id,
        "equivalence_status": reviewed.equivalence_status,
        "comparison_basis": reviewed.comparison_basis,
        "declared_coverage": reviewed.declared_coverage,
        "catalog_declared_resolutions_metres": list(
            reviewed.catalog_declared_resolutions_metres
        ),
        "declared_resolutions_metres": list(
            reviewed.declared_resolutions_metres
        ),
        "promotion_eligible": reviewed.promotion_eligible,
        "public_notice": reviewed.public_notice,
        "parity_requirements": list(reviewed.parity_requirements),
    }


def reviewed_ign_ortho_public_projection(
    source_config: Any,
) -> dict[str, Any] | None:
    """Project reviewed substitution evidence without acquisition URLs.

    Configurations carrying this schema are accepted only when every value is
    identical to the committed profile.  A changed source definition must not
    keep an old public equivalence claim.
    """

    if not isinstance(source_config, dict):
        return None
    raw = source_config.get("reviewed_equivalence")
    if not isinstance(raw, dict) or raw.get("schema") != EQUIVALENCE_SCHEMA:
        return None
    profile = raw.get("profile")
    if not isinstance(profile, str):
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho source has no valid profile"
        )
    reviewed = _reviewed_substitutions_by_profile().get(profile)
    if reviewed is None or raw != reviewed_ign_ortho_equivalence(reviewed):
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho source no longer matches committed evidence"
        )
    return {
        "schema": PUBLIC_SUBSTITUTION_SCHEMA,
        "profile": reviewed.profile,
        "evidence_profile_sha256": reviewed.evidence_profile_sha256,
        "capabilities_sha256": reviewed.capabilities_sha256,
        "catalog_capabilities_sha256": (
            reviewed.catalog_capabilities_sha256
        ),
        "catalog_layer": reviewed.catalog_layer,
        "catalog_title": reviewed.catalog_title,
        "catalog_abstract_sha256": hashlib.sha256(
            reviewed.catalog_abstract.encode("utf-8")
        ).hexdigest(),
        "selected_layer": reviewed.selected_layer,
        "selected_title": reviewed.selected_title,
        "metadata_record_id": reviewed.metadata_record_id,
        "equivalence_status": reviewed.equivalence_status,
        "comparison_basis": reviewed.comparison_basis,
        "declared_coverage": reviewed.declared_coverage,
        "catalog_declared_resolutions_metres": list(
            reviewed.catalog_declared_resolutions_metres
        ),
        "declared_resolutions_metres": list(
            reviewed.declared_resolutions_metres
        ),
        "promotion_eligible": reviewed.promotion_eligible,
        "public_notice": reviewed.public_notice,
        "parity_requirements": list(reviewed.parity_requirements),
    }


def reviewed_ign_ortho_source_projection(
    source_definition: Any,
) -> dict[str, Any] | None:
    """Validate the complete selected source before projecting its claim."""

    if not isinstance(source_definition, dict):
        return None
    projection = reviewed_ign_ortho_public_projection(
        source_definition.get("config")
    )
    if projection is None:
        if _is_controlled_ign_ortho_source(source_definition):
            raise ReviewedOrthoEvidenceError(
                "reviewed ortho source is missing committed evidence"
            )
        return None
    reviewed = _reviewed_substitutions_by_profile()[projection["profile"]]
    if source_definition != reviewed_ign_ortho_expected_source_definition(
        reviewed
    ):
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho source identity differs from committed evidence"
        )
    return projection


def _is_controlled_ign_ortho_source(source_definition: dict[str, Any]) -> bool:
    """Recognize reviewed IGN identities even if their marker was removed."""

    if source_definition.get("endpoint_url") != SELECTED_ENDPOINT_URL:
        return False
    remote_name = source_definition.get("remote_name")
    if not isinstance(remote_name, str):
        return False
    return remote_name in {
        reviewed.selected_layer
        for reviewed in _reviewed_substitutions().values()
    }


def reviewed_ign_ortho_expected_source_definition(
    reviewed: ReviewedIgnOrthoSubstitution,
) -> dict[str, Any]:
    """Return the one complete source definition bound to the assessment."""

    return {
        "protocol": "wms_tiles",
        "target_kind": "tiles",
        "endpoint_url": reviewed.selected_endpoint_url,
        "remote_name": reviewed.selected_layer,
        "sync_strategy": "tile_seed",
        "priority": SOURCE_PRIORITY,
        "config": {
            "bounds": dict(reviewed.bounds),
            "min_zoom": reviewed.min_zoom,
            "max_zoom": reviewed.max_zoom,
            "format": reviewed.image_format,
            "style_name": reviewed.style_name,
            "coverage_required": True,
            "max_tile_count": reviewed.max_tile_count,
            "coverage_profile": reviewed.coverage_profile,
            "wms_supertile_size": reviewed.wms_supertile_size,
            "reviewed_equivalence": reviewed_ign_ortho_equivalence(reviewed),
        },
    }


def reviewed_ign_ortho_catalog_projection(
    catalog_endpoint_url: str,
    catalog_layer: str,
) -> dict[str, Any] | None:
    """Project the reviewed assessment even when no source is eligible.

    This is intentionally catalog-scoped.  It lets the API explain why a
    partial or semantically different IGN layer was not configured as a local
    source, without representing that rejected layer as an active source.
    """

    reviewed = reviewed_ign_ortho_substitution(
        catalog_endpoint_url,
        catalog_layer,
    )
    if reviewed is None:
        return None
    return reviewed_ign_ortho_public_projection(
        {"reviewed_equivalence": reviewed_ign_ortho_equivalence(reviewed)}
    )


def require_reviewed_ign_ortho_acquisition_allowed(
    source_definition: Any,
) -> dict[str, Any] | None:
    """Fail closed before acquiring a reviewed but non-equivalent mapping."""

    projection = reviewed_ign_ortho_source_projection(source_definition)
    if projection is not None and projection["promotion_eligible"] is not True:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho substitution is not eligible for acquisition"
        )
    return projection


@lru_cache(maxsize=1)
def _reviewed_substitutions() -> dict[str, ReviewedIgnOrthoSubstitution]:
    profile_body = _resource_bytes(PROFILE_RESOURCE, MAX_PROFILE_BYTES)
    if hashlib.sha256(profile_body).hexdigest() != PROFILE_SHA256:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho profile digest does not match"
        )
    capabilities_body = _resource_bytes(
        CAPABILITIES_RESOURCE,
        MAX_CAPABILITIES_BYTES,
    )
    if hashlib.sha256(capabilities_body).hexdigest() != CAPABILITIES_SHA256:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN capabilities digest does not match"
        )
    catalog_capabilities_body = _resource_bytes(
        CATALOG_CAPABILITIES_RESOURCE,
        MAX_CAPABILITIES_BYTES,
    )
    if (
        hashlib.sha256(catalog_capabilities_body).hexdigest()
        != CATALOG_CAPABILITIES_SHA256
    ):
        raise ReviewedOrthoEvidenceError(
            "reviewed ITACyL capabilities digest does not match"
        )
    document = _strict_json(profile_body)
    if set(document) != _PROFILE_KEYS:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho profile keys are invalid"
        )
    if (
        document.get("schema_version") != PROFILE_SCHEMA
        or document.get("audit_date") != "2026-07-27"
    ):
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho profile identity is invalid"
        )
    _validate_profile_header(document)
    advertised_layers = _capabilities_layers(capabilities_body)
    catalog_layers = _catalog_capabilities_layers(
        catalog_capabilities_body
    )
    raw_mappings = document.get("mappings")
    if not isinstance(raw_mappings, list) or len(raw_mappings) != 20:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho profile must contain twenty mappings"
        )

    result: dict[str, ReviewedIgnOrthoSubstitution] = {}
    profiles: set[str] = set()
    exact_count = 0
    degraded_count = 0
    blocked_count = 0
    source = document["selected_source"]
    license_value = document["license_evidence"]
    operations = document["operational_profile"]
    for raw in raw_mappings:
        if not isinstance(raw, dict) or set(raw) != _MAPPING_KEYS:
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN ortho mapping keys are invalid"
            )
        catalog_layer = _text(raw.get("catalog_layer"), "catalog layer", 100)
        selected_layer = _text(
            raw.get("selected_layer"),
            "selected layer",
            100,
        )
        profile_name = _text(raw.get("profile"), "profile", 200)
        if catalog_layer in result or profile_name in profiles:
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN ortho profile repeats an identity"
            )
        advertised = advertised_layers.get(selected_layer)
        if advertised is None:
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN ortho layer is not in capabilities"
            )
        catalog_advertised = catalog_layers.get(catalog_layer)
        if catalog_advertised is None:
            raise ReviewedOrthoEvidenceError(
                "reviewed ITACyL ortho layer is not in capabilities"
            )
        selected_title = _text(raw.get("selected_title"), "title", 500)
        selected_abstract = _text(
            raw.get("selected_abstract"),
            "abstract",
            20_000,
        )
        metadata_url = _https_url(
            raw.get("metadata_url"),
            "metadata URL",
        )
        metadata_record_id = _text(
            raw.get("metadata_record_id"),
            "metadata record id",
            255,
        )
        if (
            selected_title != advertised["title"]
            or selected_abstract != advertised["abstract"]
            or metadata_url != advertised["metadata_url"]
            or parse_qs(urlsplit(metadata_url).query).get("ID")
            != [metadata_record_id]
        ):
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN mapping differs from capabilities"
            )
        declared_coverage = _declared_castilla_y_leon_coverage(
            selected_layer,
            selected_abstract,
        )
        declared_resolutions = _declared_resolutions_metres(
            selected_abstract
        )
        catalog_abstract = catalog_advertised["abstract"]
        catalog_resolutions = _declared_resolutions_metres(
            catalog_abstract
        )
        expected_status = _derived_equivalence_status(
            catalog_layer=catalog_layer,
            selected_layer=selected_layer,
            declared_coverage=declared_coverage,
            catalog_abstract=catalog_abstract,
            selected_abstract=selected_abstract,
        )
        status = raw.get("equivalence_status")
        if status != expected_status:
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN status contradicts declared coverage or semantics"
            )
        if status == "exact":
            exact_count += 1
        elif status == "substitute_degraded":
            degraded_count += 1
        elif status == "blocked":
            blocked_count += 1
        else:
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN equivalence status is invalid"
            )
        requirements = _text_list(
            raw.get("parity_requirements"),
            "parity requirements",
            maximum=16,
        )
        notice = _text(raw.get("public_notice"), "public notice", 2_000)
        if status == "substitute_degraded" and (
            "explicit_public_degradation_notice" not in requirements
            or "immutable_degraded_delivery_classification"
            not in requirements
        ):
            raise ReviewedOrthoEvidenceError(
                "degraded ortho mapping lacks its immutable public gates"
            )
        if status == "blocked" and (
            "explicit_public_degradation_notice" not in requirements
            or "promotion_blocked_until_equivalence_evidence"
            not in requirements
        ):
            raise ReviewedOrthoEvidenceError(
                "blocked ortho mapping lacks its fail-closed gates"
            )
        if status == "exact" and (
            not declared_resolutions or not catalog_resolutions
        ):
            raise ReviewedOrthoEvidenceError(
                "exact ortho mapping has no declared resolution comparison"
            )
        reviewed = ReviewedIgnOrthoSubstitution(
            profile=profile_name,
            catalog_endpoint_url=CATALOG_ENDPOINT_URL,
            catalog_layer=catalog_layer,
            catalog_title=catalog_advertised["title"],
            catalog_abstract=catalog_abstract,
            catalog_capabilities_sha256=(
                CATALOG_CAPABILITIES_SHA256
            ),
            selected_endpoint_url=SELECTED_ENDPOINT_URL,
            selected_layer=selected_layer,
            selected_title=selected_title,
            selected_abstract=selected_abstract,
            metadata_record_id=metadata_record_id,
            capabilities_url=CAPABILITIES_URL,
            capabilities_sha256=CAPABILITIES_SHA256,
            evidence_profile_sha256=PROFILE_SHA256,
            image_format=source["image_format"],
            style_name=source["style_name"],
            equivalence_status=status,
            comparison_basis=_comparison_basis(status),
            declared_coverage=declared_coverage,
            catalog_declared_resolutions_metres=catalog_resolutions,
            declared_resolutions_metres=declared_resolutions,
            promotion_eligible=status != "blocked",
            public_notice=notice,
            parity_requirements=requirements,
            license_name=license_value["name"],
            license_url=license_value["url"],
            required_attribution=license_value["attribution"],
            bounds=dict(operations["bounds"]),
            min_zoom=operations["min_zoom"],
            max_zoom=operations["max_zoom"],
            coverage_profile=operations["coverage_profile"],
            wms_supertile_size=operations["wms_supertile_size"],
            max_tile_count=operations["max_tile_count"],
        )
        result[catalog_layer] = reviewed
        profiles.add(profile_name)
    if (
        frozenset(result) != _EXPECTED_CATALOG_LAYERS
        or exact_count != 6
        or degraded_count != 13
        or blocked_count != 1
    ):
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho coverage is incomplete"
        )
    return result


def _declared_castilla_y_leon_coverage(
    selected_layer: str,
    abstract: str,
) -> str:
    """Derive the regional coverage claim from the committed WMS abstract."""

    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", abstract)
        if not unicodedata.combining(character)
    ).casefold()
    region = "castilla y leon"
    if region not in normalized:
        if "totalidad del territorio espanol" in normalized:
            return "full"
        if "parte del territorio espanol" in normalized:
            return "unknown"
        return "none" if selected_layer.startswith("PNOA") else "unknown"
    directional = set(
        re.findall(
            r"(?:parte\s+)?(norte|sur|nw|ne|sw|se)\s+de\s+castilla y leon",
            normalized,
        )
    )
    if not directional or {"norte", "sur"}.issubset(directional):
        return "full"
    return "partial"


def _declared_resolutions_metres(abstract: str) -> tuple[float, ...]:
    values: set[float] = set()
    for number, unit in re.findall(
        r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(cm|m)(?![a-z])",
        abstract.casefold(),
    ):
        value = float(number.replace(",", "."))
        if unit == "cm":
            value /= 100
        if 0 < value <= 100:
            values.add(value)
    return tuple(sorted(values))


def _derived_equivalence_status(
    *,
    catalog_layer: str,
    selected_layer: str,
    declared_coverage: str,
    catalog_abstract: str,
    selected_abstract: str,
) -> str:
    year = catalog_layer.removeprefix("Ortofoto_")
    catalog_normalized = _normalized_text(catalog_abstract)
    selected_normalized = _normalized_text(selected_abstract)
    if catalog_layer == "Ortofoto_2021":
        if (
            selected_layer != "PNOA2021"
            or declared_coverage != "none"
            or "2021" not in catalog_normalized
            or "cyl" not in catalog_normalized
            or "2021" not in selected_normalized
        ):
            raise ReviewedOrthoEvidenceError(
                "PNOA2021 blocked assessment no longer matches capabilities"
            )
        return "blocked"
    if catalog_layer in _EXACT_CATALOG_LAYERS:
        catalog_resolutions = set(
            _declared_resolutions_metres(catalog_abstract)
        )
        selected_resolutions = set(
            _declared_resolutions_metres(selected_abstract)
        )
        if (
            selected_layer != f"PNOA{year}"
            or year not in catalog_normalized
            or year not in selected_normalized
            or "pnoa" not in catalog_normalized
            or "pnoa" not in selected_normalized
            or not catalog_resolutions
            or not catalog_resolutions.issubset(selected_resolutions)
        ):
            raise ReviewedOrthoEvidenceError(
                "exact PNOA comparison no longer matches both capabilities"
            )
        return "exact"
    return "substitute_degraded"


def _comparison_basis(status: str) -> str:
    if status == "exact":
        return "catalog_and_selected_capabilities_match"
    if status == "blocked":
        return "selected_source_has_no_declared_regional_coverage"
    return "catalog_and_selected_capabilities_require_degraded_delivery"


def _normalized_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    ).casefold()


@lru_cache(maxsize=1)
def _reviewed_substitutions_by_profile(
) -> dict[str, ReviewedIgnOrthoSubstitution]:
    return {item.profile: item for item in _reviewed_substitutions().values()}


def _validate_profile_header(document: dict[str, Any]) -> None:
    catalog = document.get("catalog_identity")
    catalog_capabilities = document.get("catalog_capabilities_snapshot")
    selected = document.get("selected_source")
    capabilities = document.get("capabilities_snapshot")
    license_value = document.get("license_evidence")
    operations = document.get("operational_profile")
    if catalog != {
        "endpoint_url": CATALOG_ENDPOINT_URL,
        "layer_prefix": "Ortofoto_",
    }:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho catalog identity is invalid"
        )
    if catalog_capabilities != {
        "url": CATALOG_CAPABILITIES_URL,
        "local_resource": CATALOG_CAPABILITIES_RESOURCE,
        "sha256": CATALOG_CAPABILITIES_SHA256,
        "service_version": "1.3.0",
    }:
        raise ReviewedOrthoEvidenceError(
            "reviewed ITACyL capabilities identity is invalid"
        )
    if selected != {
        "endpoint_url": SELECTED_ENDPOINT_URL,
        "protocol": "wms_tiles",
        "target_kind": "tiles",
        "image_format": "image/jpeg",
        "style_name": "",
    }:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho selected source is invalid"
        )
    if capabilities != {
        "url": CAPABILITIES_URL,
        "local_resource": CAPABILITIES_RESOURCE,
        "sha256": CAPABILITIES_SHA256,
        "service_version": "1.3.0",
        "update_sequence": "2619",
    }:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho capabilities identity is invalid"
        )
    if license_value != {
        "name": (
            "Creative Commons Attribution 4.0 International (CC BY 4.0)"
        ),
        "url": LICENSE_URL,
        "attribution": REQUIRED_ATTRIBUTION,
    }:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho license evidence is invalid"
        )
    if operations != _EXPECTED_OPERATIONAL_PROFILE:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho operational profile is invalid"
        )


def _capabilities_layers(body: bytes) -> dict[str, dict[str, str]]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN capabilities XML is invalid"
        ) from error
    if (
        root.tag != "{http://www.opengis.net/wms}WMS_Capabilities"
        or root.get("version") != "1.3.0"
        or root.get("updateSequence") != "2619"
    ):
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN capabilities service identity is invalid"
        )
    result: dict[str, dict[str, str]] = {}
    for layer in root.findall(".//wms:Layer", _WMS_NS):
        name = layer.findtext("wms:Name", namespaces=_WMS_NS)
        if not name:
            continue
        metadata = layer.find(
            "./wms:MetadataURL/wms:OnlineResource",
            _WMS_NS,
        )
        metadata_url = metadata.get(_XLINK_HREF) if metadata is not None else None
        title = layer.findtext("wms:Title", namespaces=_WMS_NS)
        abstract = layer.findtext("wms:Abstract", namespaces=_WMS_NS)
        if not title or not abstract or not metadata_url:
            continue
        if name in result:
            raise ReviewedOrthoEvidenceError(
                "reviewed IGN capabilities repeat a layer"
            )
        result[name] = {
            "title": title,
            "abstract": abstract.strip(),
            "metadata_url": metadata_url,
        }
    return result


def _catalog_capabilities_layers(body: bytes) -> dict[str, dict[str, str]]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ReviewedOrthoEvidenceError(
            "reviewed ITACyL capabilities XML is invalid"
        ) from error
    if (
        root.tag != "{http://www.opengis.net/wms}WMS_Capabilities"
        or root.get("version") != "1.3.0"
    ):
        raise ReviewedOrthoEvidenceError(
            "reviewed ITACyL capabilities service identity is invalid"
        )
    result: dict[str, dict[str, str]] = {}
    for layer in root.findall(".//wms:Layer", _WMS_NS):
        name = layer.findtext("wms:Name", namespaces=_WMS_NS)
        if not name or not name.startswith("Ortofoto_"):
            continue
        title = layer.findtext("wms:Title", namespaces=_WMS_NS)
        abstract = layer.findtext("wms:Abstract", namespaces=_WMS_NS)
        if not title:
            continue
        if name in result:
            raise ReviewedOrthoEvidenceError(
                "reviewed ITACyL capabilities repeat a layer"
            )
        result[name] = {
            "title": title.strip(),
            "abstract": "" if abstract is None else abstract.strip(),
        }
    if not _EXPECTED_CATALOG_LAYERS.issubset(result):
        raise ReviewedOrthoEvidenceError(
            "reviewed ITACyL capabilities omit an ortho layer"
        )
    return result


def _resource_bytes(name: str, maximum: int) -> bytes:
    try:
        body = files("app.reference_layers").joinpath(name).read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho evidence resource is unavailable"
        ) from error
    if not 1 <= len(body) <= maximum:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho evidence size is invalid"
        )
    return body


def _strict_json(body: bytes) -> dict[str, Any]:
    def unique(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho profile is not strict JSON"
        ) from error
    if not isinstance(value, dict):
        raise ReviewedOrthoEvidenceError(
            "reviewed IGN ortho profile root is invalid"
        )
    return value


def _text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ReviewedOrthoEvidenceError(f"reviewed ortho {label} is invalid")
    return value


def _text_list(value: Any, label: str, *, maximum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item) > 200
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise ReviewedOrthoEvidenceError(f"reviewed ortho {label} are invalid")
    return tuple(value)


def _https_url(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 8_192:
        raise ReviewedOrthoEvidenceError(f"reviewed ortho {label} is invalid")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ReviewedOrthoEvidenceError(
            f"reviewed ortho {label} is invalid"
        ) from error
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in {None, 443}
    ):
        raise ReviewedOrthoEvidenceError(f"reviewed ortho {label} is invalid")
    return value
