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
LIVE_CAPABILITIES_GATE_SCHEMA = (
    "siur-reviewed-ortho-live-capabilities-gate/v1"
)
PARITY_POLICY_SCHEMA = "siur-reviewed-ortho-parity-policy/v1"
PARITY_GATE_SCHEMA = "siur-reviewed-ortho-parity-gate/v1"
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
    "951082e32627c1744e7f04bb4592a315"
    "857c08047c3240cf47187c5ebefd1187"
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
ATTRIBUTION_RULE = (
    "official_product_and_date_derived_work_formula"
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
        "review_notes",
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
    review_notes: tuple[str, ...]
    parity_policy: dict[str, Any]
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
        "catalog_capabilities_url": CATALOG_CAPABILITIES_URL,
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
        "parity_policy": reviewed.parity_policy,
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
        "parity_policy": reviewed.parity_policy,
        "required_attribution": reviewed.required_attribution,
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


def reviewed_ign_ortho_live_capabilities_gate(
    source_definition: Any,
    document: bytes,
    *,
    phase: str,
) -> dict[str, Any] | None:
    """Compare live WMS semantics with the exact committed review snapshot.

    A harmless byte-level change is allowed, but every field that affects the
    identity, rendering, coverage, attribution or metadata of the selected
    layer must remain identical.  The returned hashes are suitable for
    persistence in the acquisition observation and delivery validation gate.
    """

    projection = require_reviewed_ign_ortho_acquisition_allowed(
        source_definition
    )
    if projection is None:
        return None
    if phase not in {"pre_download", "pre_promotion"}:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho capabilities phase is invalid"
        )
    reviewed = _reviewed_substitutions_by_profile()[projection["profile"]]
    expected_document = _resource_bytes(
        CAPABILITIES_RESOURCE,
        MAX_CAPABILITIES_BYTES,
    )
    expected = _wms_semantic_snapshot(
        expected_document,
        reviewed.selected_layer,
    )
    live = _wms_semantic_snapshot(document, reviewed.selected_layer)
    if live != expected:
        raise ReviewedOrthoEvidenceError(
            "live IGN WMS semantics changed since the reviewed snapshot"
        )
    semantic_sha256 = _canonical_sha256(expected)
    return {
        "schema_version": LIVE_CAPABILITIES_GATE_SCHEMA,
        "passed": True,
        "phase": phase,
        "profile": reviewed.profile,
        "selected_layer": reviewed.selected_layer,
        "snapshot_sha256": reviewed.capabilities_sha256,
        "live_sha256": hashlib.sha256(document).hexdigest(),
        "semantic_sha256": semantic_sha256,
        "semantic_fields": [
            "layer",
            "title",
            "abstract",
            "crs",
            "formats",
            "styles",
            "bounds",
            "attribution",
            "access_constraints",
            "metadata",
        ],
    }


def reviewed_ign_ortho_catalog_capabilities_gate(
    source_definition: Any,
    document: bytes,
) -> dict[str, Any] | None:
    """Bind the compared ITACyL layer to its reviewed live WMS semantics."""

    projection = require_reviewed_ign_ortho_acquisition_allowed(
        source_definition
    )
    if projection is None:
        return None
    reviewed = _reviewed_substitutions_by_profile()[projection["profile"]]
    expected_document = _resource_bytes(
        CATALOG_CAPABILITIES_RESOURCE,
        MAX_CAPABILITIES_BYTES,
    )
    expected = _wms_semantic_snapshot(
        expected_document,
        reviewed.catalog_layer,
    )
    live = _wms_semantic_snapshot(document, reviewed.catalog_layer)
    if live != expected:
        raise ReviewedOrthoEvidenceError(
            "live ITACyL WMS semantics changed since the reviewed snapshot"
        )
    return {
        "schema_version": LIVE_CAPABILITIES_GATE_SCHEMA,
        "passed": True,
        "phase": "parity_catalog",
        "profile": reviewed.profile,
        "selected_layer": reviewed.catalog_layer,
        "snapshot_sha256": reviewed.catalog_capabilities_sha256,
        "live_sha256": hashlib.sha256(document).hexdigest(),
        "semantic_sha256": _canonical_sha256(expected),
        "semantic_fields": [
            "layer",
            "title",
            "abstract",
            "crs",
            "formats",
            "styles",
            "bounds",
            "attribution",
            "access_constraints",
            "metadata",
        ],
    }


def require_reviewed_ign_ortho_delivery_allowed(
    *,
    catalog_endpoint_url: str,
    catalog_layer: str,
    source_definition: Any,
    validation_json: Any,
    content_sha256: str,
) -> dict[str, Any] | None:
    """Validate the executable, immutable gate for promotion and serving."""

    projection = reviewed_ign_ortho_source_projection(source_definition)
    reviewed = reviewed_ign_ortho_substitution(
        catalog_endpoint_url,
        catalog_layer,
    )
    if projection is None:
        if reviewed is None:
            return None
        raise ReviewedOrthoEvidenceError(
            "stored ortho bytes do not have a reviewed source identity"
        )
    if reviewed is None:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho bytes no longer have their catalog identity"
        )
    if catalog_layer == "Ortofoto_2021":
        raise ReviewedOrthoEvidenceError(
            "the 2021 ortho delivery is permanently blocked"
        )
    if (
        projection["profile"] != reviewed.profile
        or projection["equivalence_status"] != reviewed.equivalence_status
    ):
        raise ReviewedOrthoEvidenceError(
            "stored ortho bytes do not have the reviewed source identity"
        )
    if not isinstance(validation_json, dict):
        raise ReviewedOrthoEvidenceError(
            "stored ortho delivery has no validation evidence"
        )
    gate = validation_json.get("reviewed_ortho_parity_gate")
    if not isinstance(gate, dict):
        raise ReviewedOrthoEvidenceError(
            "stored ortho delivery has no executable parity gate"
        )
    evidence_sha256 = gate.get("evidence_sha256")
    unsigned = {key: value for key, value in gate.items() if key != "evidence_sha256"}
    expected = {
        "schema_version": PARITY_GATE_SCHEMA,
        "passed": True,
        "profile": reviewed.profile,
        "evidence_profile_sha256": reviewed.evidence_profile_sha256,
        "classification": reviewed.equivalence_status,
        "required_attribution": reviewed.required_attribution,
        "policy_sha256": _canonical_sha256(reviewed.parity_policy),
        "delivery_content_sha256": content_sha256,
    }
    if (
        any(unsigned.get(key) != value for key, value in expected.items())
        or evidence_sha256 != _canonical_sha256(unsigned)
        or not _valid_live_gate(
            unsigned.get("selected_capabilities"),
            reviewed=reviewed,
            layer=reviewed.selected_layer,
        )
        or not _valid_live_gate(
            unsigned.get("catalog_capabilities"),
            reviewed=reviewed,
            layer=reviewed.catalog_layer,
        )
        or not _valid_parity_measurements(unsigned, reviewed)
    ):
        raise ReviewedOrthoEvidenceError(
            "stored ortho delivery parity evidence is invalid"
        )
    return {
        **projection,
        "required_attribution": reviewed.required_attribution,
        "delivery_content_sha256": content_sha256,
        "parity_evidence_sha256": evidence_sha256,
    }


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
            raw.get("review_notes"),
            "review notes",
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
            review_notes=requirements,
            parity_policy=_parity_policy(
                profile=profile_name,
                classification=status,
            ),
            license_name=license_value["name"],
            license_url=license_value["url"],
            required_attribution=_required_product_attribution(
                selected_layer
            ),
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


def _required_product_attribution(selected_layer: str) -> str:
    if selected_layer.startswith("PNOA") and selected_layer[4:].isdigit():
        return (
            f"Obra derivada de PNOA {selected_layer[4:]} "
            "CC-BY 4.0 scne.es"
        )
    if selected_layer == "SIGPAC":
        return (
            "Obra derivada de Orto-SIGPAC 1997-2003 "
            "CC-BY 4.0 scne.es"
        )
    if selected_layer == "Interministerial_1973-1986":
        return (
            "Obra derivada de Orto-Interministerial 1976-1986 "
            "CC-BY 4.0 scne.es"
        )
    if selected_layer == "AMS_1956-1957":
        return (
            "Obra derivada de Orto-AMS 1956-1957 "
            "CC-BY 4.0 ejercito.defensa.gob.es"
        )
    raise ReviewedOrthoEvidenceError(
        "reviewed ortho product has no official attribution formula"
    )


def _parity_policy(
    *,
    profile: str,
    classification: str,
) -> dict[str, Any]:
    exact = classification == "exact"
    return {
        "schema_version": PARITY_POLICY_SCHEMA,
        "profile": profile,
        "classification": classification,
        "sample_plan": {
            "schema_version": "siur-ortho-sample-plan/v1",
            "minimum_sample_count": 12,
            "tile_width": 256,
            "tile_height": 256,
            "crs": "EPSG:3857",
            "coordinate_selection": "zoom-stratified-stable-v1",
        },
        "coverage_mask": {
            "minimum_nonempty_samples_per_source": 1,
            "minimum_mask_iou": 0.90 if exact else None,
        },
        "resolution_scale": {
            "require_identical_coordinates": True,
            "maximum_resolution_delta_metres_per_pixel": 0.0,
            "maximum_scale_denominator_delta": 0.0,
        },
        "pixel_samples": {
            "require_local_selected_digest_match": True,
            "maximum_normalized_mean_absolute_error": (
                0.12 if exact else None
            ),
            "comparison_is_classification_evidence": True,
        },
        "promotion": {
            "eligible": classification != "blocked",
            "immutable_degraded_classification": (
                classification == "substitute_degraded"
            ),
        },
    }


def build_reviewed_ign_ortho_parity_gate(
    *,
    source_definition: Any,
    content_sha256: str,
    selected_capabilities: Any,
    catalog_capabilities: Any,
    measurements: Any,
) -> dict[str, Any] | None:
    """Build and self-validate the delivery gate from measured evidence."""

    projection = require_reviewed_ign_ortho_acquisition_allowed(
        source_definition
    )
    if projection is None:
        return None
    reviewed = _reviewed_substitutions_by_profile()[projection["profile"]]
    if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho delivery content hash is invalid"
        )
    unsigned = {
        "schema_version": PARITY_GATE_SCHEMA,
        "passed": True,
        "profile": reviewed.profile,
        "evidence_profile_sha256": reviewed.evidence_profile_sha256,
        "classification": reviewed.equivalence_status,
        "required_attribution": reviewed.required_attribution,
        "policy_sha256": _canonical_sha256(reviewed.parity_policy),
        "delivery_content_sha256": content_sha256,
        "selected_capabilities": selected_capabilities,
        "catalog_capabilities": catalog_capabilities,
        "measurements": measurements,
    }
    gate = {
        **unsigned,
        "evidence_sha256": _canonical_sha256(unsigned),
    }
    require_reviewed_ign_ortho_delivery_allowed(
        catalog_endpoint_url=reviewed.catalog_endpoint_url,
        catalog_layer=reviewed.catalog_layer,
        source_definition=source_definition,
        validation_json={"reviewed_ortho_parity_gate": gate},
        content_sha256=content_sha256,
    )
    return gate


def _valid_live_gate(
    value: Any,
    *,
    reviewed: ReviewedIgnOrthoSubstitution,
    layer: str,
) -> bool:
    if not isinstance(value, dict):
        return False
    snapshot_sha256 = (
        reviewed.capabilities_sha256
        if layer == reviewed.selected_layer
        else reviewed.catalog_capabilities_sha256
    )
    resource = (
        CAPABILITIES_RESOURCE
        if layer == reviewed.selected_layer
        else CATALOG_CAPABILITIES_RESOURCE
    )
    expected_semantic = _wms_semantic_snapshot(
        _resource_bytes(resource, MAX_CAPABILITIES_BYTES),
        layer,
    )
    return (
        value.get("schema_version") == LIVE_CAPABILITIES_GATE_SCHEMA
        and value.get("passed") is True
        and value.get("profile") == reviewed.profile
        and value.get("selected_layer") == layer
        and value.get("snapshot_sha256") == snapshot_sha256
        and isinstance(value.get("live_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["live_sha256"]) is not None
        and value.get("semantic_sha256")
        == _canonical_sha256(expected_semantic)
        and value.get("phase")
        == (
            "pre_promotion"
            if layer == reviewed.selected_layer
            else "parity_catalog"
        )
    )


def _valid_parity_measurements(
    gate: dict[str, Any],
    reviewed: ReviewedIgnOrthoSubstitution,
) -> bool:
    value = gate.get("measurements")
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "sample_count",
        "coordinate_sha256",
        "selected_sample_sha256",
        "local_sample_sha256",
        "catalog_sample_sha256",
        "coverage_mask",
        "resolution_scale",
        "pixel_samples",
    }:
        return False
    count = value.get("sample_count")
    digests = (
        value.get("coordinate_sha256"),
        value.get("selected_sample_sha256"),
        value.get("local_sample_sha256"),
        value.get("catalog_sample_sha256"),
    )
    coverage = value.get("coverage_mask")
    resolution = value.get("resolution_scale")
    pixels = value.get("pixel_samples")
    policy = reviewed.parity_policy
    minimum = policy["sample_plan"]["minimum_sample_count"]
    if (
        value.get("schema_version")
        != "siur-reviewed-ortho-parity-measurements/v1"
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not minimum <= count <= 256
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in digests
        )
        or value["selected_sample_sha256"]
        != value["local_sample_sha256"]
        or not isinstance(coverage, dict)
        or not isinstance(resolution, dict)
        or not isinstance(pixels, dict)
    ):
        return False
    selected_nonempty = coverage.get("selected_nonempty_samples")
    catalog_nonempty = coverage.get("catalog_nonempty_samples")
    mask_iou = coverage.get("minimum_mask_iou")
    mae = pixels.get("normalized_mean_absolute_error")
    sampled_zooms = resolution.get("sampled_zooms")
    if (
        isinstance(selected_nonempty, bool)
        or not isinstance(selected_nonempty, int)
        or isinstance(catalog_nonempty, bool)
        or not isinstance(catalog_nonempty, int)
        or not 1 <= selected_nonempty <= count
        or not 1 <= catalog_nonempty <= count
        or not isinstance(mask_iou, (int, float))
        or isinstance(mask_iou, bool)
        or not 0 <= float(mask_iou) <= 1
        or resolution.get("tile_width") != 256
        or resolution.get("tile_height") != 256
        or resolution.get("crs") != "EPSG:3857"
        or not isinstance(sampled_zooms, list)
        or not sampled_zooms
        or any(
            isinstance(zoom, bool)
            or not isinstance(zoom, int)
            or not reviewed.min_zoom <= zoom <= reviewed.max_zoom
            for zoom in sampled_zooms
        )
        or resolution.get("maximum_resolution_delta_metres_per_pixel")
        != 0.0
        or resolution.get("maximum_scale_denominator_delta") != 0.0
        or not isinstance(mae, (int, float))
        or isinstance(mae, bool)
        or not 0 <= float(mae) <= 1
    ):
        return False
    if reviewed.equivalence_status == "exact":
        return (
            float(mask_iou)
            >= policy["coverage_mask"]["minimum_mask_iou"]
            and float(mae)
            <= policy["pixel_samples"][
                "maximum_normalized_mean_absolute_error"
            ]
        )
    return (
        reviewed.equivalence_status == "substitute_degraded"
        and policy["promotion"]["immutable_degraded_classification"] is True
    )


def _wms_semantic_snapshot(
    body: bytes,
    selected_layer: str,
) -> dict[str, Any]:
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_CAPABILITIES_BYTES:
        raise ReviewedOrthoEvidenceError(
            "live WMS capabilities size is invalid"
        )
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ReviewedOrthoEvidenceError(
            "live WMS capabilities contain forbidden declarations"
        )
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ReviewedOrthoEvidenceError(
            "live WMS capabilities XML is invalid"
        ) from error
    elements = 0
    text_bytes = 0
    stack: list[tuple[ElementTree.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        elements += 1
        if elements > 100_000 or depth > 64:
            raise ReviewedOrthoEvidenceError(
                "live WMS capabilities structure is too large"
            )
        text_bytes += len((element.text or "").encode("utf-8"))
        text_bytes += len((element.tail or "").encode("utf-8"))
        if text_bytes > 4 * 1024 * 1024:
            raise ReviewedOrthoEvidenceError(
                "live WMS capabilities text is too large"
            )
        stack.extend((child, depth + 1) for child in element)
    if root.get("version") != "1.3.0":
        raise ReviewedOrthoEvidenceError(
            "live WMS capabilities version is invalid"
        )
    service = next(
        (
            item
            for item in root
            if _local_name(item.tag) == "Service"
        ),
        None,
    )
    service_constraints = _child_text(
        service,
        "AccessConstraints",
    )
    formats: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) == "GetMap":
            formats = sorted(
                {
                    text
                    for child in element
                    if _local_name(child.tag) == "Format"
                    for text in [_element_text(child)]
                    if text
                }
            )
            break
    selected: dict[str, Any] | None = None

    def visit(
        layer: ElementTree.Element,
        *,
        inherited_crs: tuple[str, ...],
        inherited_bounds: tuple[dict[str, Any], ...],
        inherited_styles: tuple[dict[str, Any], ...],
        inherited_attribution: dict[str, str | None] | None,
        inherited_constraints: str | None,
    ) -> None:
        nonlocal selected
        direct_crs = tuple(
            token
            for child in layer
            if _local_name(child.tag) in {"CRS", "SRS"}
            for token in (_element_text(child) or "").split()
        )
        crs = tuple(sorted(set((*inherited_crs, *direct_crs))))
        direct_bounds = tuple(
            _semantic_bounds(child)
            for child in layer
            if _local_name(child.tag)
            in {"EX_GeographicBoundingBox", "BoundingBox", "LatLonBoundingBox"}
        )
        bounds = direct_bounds or inherited_bounds
        direct_styles = tuple(
            _semantic_style(child)
            for child in layer
            if _local_name(child.tag) == "Style"
        )
        styles = tuple(
            {
                json.dumps(item, ensure_ascii=False, sort_keys=True): item
                for item in (*inherited_styles, *direct_styles)
            }.values()
        )
        direct_attribution = next(
            (
                _semantic_attribution(child)
                for child in layer
                if _local_name(child.tag) == "Attribution"
            ),
            None,
        )
        attribution = direct_attribution or inherited_attribution
        constraints = (
            _child_text(layer, "AccessConstraints")
            or inherited_constraints
        )
        name = _child_text(layer, "Name")
        if name == selected_layer:
            sorted_styles = sorted(
                styles,
                key=lambda item: json.dumps(item, sort_keys=True),
            )
            metadata = sorted(
                (
                    _semantic_metadata(child)
                    for child in layer
                    if _local_name(child.tag) == "MetadataURL"
                ),
                key=lambda item: json.dumps(item, sort_keys=True),
            )
            selected = {
                "layer": name,
                "title": _child_text(layer, "Title"),
                "abstract": _child_text(layer, "Abstract"),
                "crs": list(crs),
                "formats": formats,
                "styles": sorted_styles,
                "bounds": list(bounds),
                "attribution": attribution,
                "access_constraints": {
                    "service": service_constraints,
                    "layer": constraints,
                },
                "metadata": metadata,
            }
        for child in layer:
            if _local_name(child.tag) == "Layer":
                visit(
                    child,
                    inherited_crs=crs,
                    inherited_bounds=bounds,
                    inherited_styles=styles,
                    inherited_attribution=attribution,
                    inherited_constraints=constraints,
                )

    for capability in root.iter():
        if _local_name(capability.tag) != "Capability":
            continue
        for child in capability:
            if _local_name(child.tag) == "Layer":
                visit(
                    child,
                    inherited_crs=(),
                    inherited_bounds=(),
                    inherited_styles=(),
                    inherited_attribution=None,
                    inherited_constraints=None,
                )
        break
    if selected is None:
        raise ReviewedOrthoEvidenceError(
            "live WMS omits the reviewed layer"
        )
    return selected


def _semantic_bounds(element: ElementTree.Element) -> dict[str, Any]:
    kind = _local_name(element.tag)
    if kind == "EX_GeographicBoundingBox":
        return {
            "kind": kind,
            "west": _child_text(element, "westBoundLongitude"),
            "south": _child_text(element, "southBoundLatitude"),
            "east": _child_text(element, "eastBoundLongitude"),
            "north": _child_text(element, "northBoundLatitude"),
        }
    return {
        "kind": kind,
        "crs": element.get("CRS") or element.get("SRS"),
        "minx": element.get("minx"),
        "miny": element.get("miny"),
        "maxx": element.get("maxx"),
        "maxy": element.get("maxy"),
        "resx": element.get("resx"),
        "resy": element.get("resy"),
    }


def _semantic_attribution(
    element: ElementTree.Element,
) -> dict[str, str | None]:
    return {
        "title": _child_text(element, "Title"),
        "url": _first_online_resource(element),
    }


def _semantic_style(element: ElementTree.Element) -> dict[str, Any]:
    legends: list[dict[str, str | None]] = []
    for child in element:
        if _local_name(child.tag) != "LegendURL":
            continue
        legends.append(
            {
                "width": child.get("width"),
                "height": child.get("height"),
                "format": _child_text(child, "Format"),
                "url": _first_online_resource(child),
            }
        )
    return {
        "name": _child_text(element, "Name"),
        "title": _child_text(element, "Title"),
        "abstract": _child_text(element, "Abstract"),
        "legends": legends,
    }


def _semantic_metadata(element: ElementTree.Element) -> dict[str, str | None]:
    return {
        "type": element.get("type"),
        "format": _child_text(element, "Format"),
        "url": _first_online_resource(element),
    }


def _first_online_resource(element: ElementTree.Element) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == "OnlineResource":
            return child.get(_XLINK_HREF) or child.get("href")
    return None


def _child_text(
    element: ElementTree.Element | None,
    name: str,
) -> str | None:
    if element is None:
        return None
    for child in element:
        if _local_name(child.tag) == name:
            return _element_text(child)
    return None


def _element_text(element: ElementTree.Element) -> str | None:
    value = "".join(element.itertext()).strip()
    return value or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _canonical_sha256(value: Any) -> str:
    try:
        body = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ReviewedOrthoEvidenceError(
            "reviewed ortho evidence is not canonical JSON"
        ) from error
    return hashlib.sha256(body).hexdigest()


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
        "attribution_rule": ATTRIBUTION_RULE,
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
