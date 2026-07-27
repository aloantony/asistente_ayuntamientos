"""Deterministic local SLD adaptations for reviewed SIUR dataset sources."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, cast
from xml.etree import ElementTree

from app.reference_layers.source_discovery import (
    ReviewedLocalStyleRecipe,
    SourceCandidate,
    SourceDiscoveryError,
    reviewed_local_style_profile_identity,
    reviewed_local_style_profile_reference,
    reviewed_local_style_expected_source_definition,
    reviewed_local_style_recipe,
    reviewed_local_style_recipes,
)


SLD_NAMESPACE = "http://www.opengis.net/sld"
OGC_NAMESPACE = "http://www.opengis.net/ogc"
AUTHORED_STYLE_SCHEMA = "siur-authored-local-style-adaptation/v1"
AUTHORED_STYLE_GENERATOR = "siur-sld-1.0-local-adaptation/v1"
IDECYL_AUTHORED_STYLE_SCHEMA = (
    "siur-authored-idecyl-local-style-adaptation/v1"
)
IDECYL_AUTHORED_STYLE_GENERATOR = (
    "siur-sld-1.0-idecyl-local-adaptation/v1"
)
STYLE_ARTIFACT_SCHEMA = "reference-style-sld/v1"
STYLE_PACKAGE_SCHEMA = "reference-style-package/v1"
STYLE_PACKAGE_FORMAT = "deterministic-style-zip/v1"
MAX_LOCAL_STYLE_BYTES = 4 * 1024 * 1024
MAX_VAT_ROWS = 10_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ElementTree.register_namespace("sld", SLD_NAMESPACE)
ElementTree.register_namespace("ogc", OGC_NAMESPACE)

_INES_PALETTE = (
    (1, "0 - 5", "#7b8257"),
    (2, "5 - 10", "#9bb068"),
    (3, "10 - 25", "#edd998"),
    (4, "25 - 50", "#f6ec3c"),
    (5, "50 - 100", "#ffd130"),
    (6, "100 - 200", "#cc8e5d"),
    (7, "> 200", "#ac514d"),
    (8, "Láminas de agua superficiales y humedales", "#1eaae2"),
    (9, "Superficies artificiales", "#d0d1d4"),
)
_FLOOD_COLORS = {
    "miteco-flood-q10-ogc-api-features-v1": ("#ff0000", "#c80000"),
    "miteco-flood-q50-ogc-api-features-v1": ("#df73ff", "#df41ff"),
    "miteco-flood-q100-ogc-api-features-v1": ("#e8beff", "#b68cff"),
    "miteco-flood-q500-ogc-api-features-v1": ("#ff73df", "#ff32df"),
    "miteco-flood-zfp-ogc-api-features-v1": ("#cccccc", "#e6e600"),
}


class LocalStyleAdaptationError(ValueError):
    """A reviewed local style recipe or its dataset evidence is invalid."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthoredLocalStyle:
    document: bytes
    sld_sha256: str
    metadata: dict[str, Any]
    evidence_sha256: str


def generate_reviewed_local_style(
    candidate: SourceCandidate,
    *,
    dataset_metadata: Mapping[str, Any] | None = None,
) -> AuthoredLocalStyle | None:
    """Build the one SLD defined by an existing singular style profile."""

    try:
        reviewed = reviewed_local_style_recipe(candidate)
    except SourceDiscoveryError as error:
        raise LocalStyleAdaptationError(
            "reviewed local style source evidence is invalid",
            code=error.code,
        ) from error
    if reviewed is None:
        return None
    return _generate_reviewed_local_style(
        reviewed,
        dataset_metadata=dataset_metadata,
    )


def generate_reviewed_local_styles(
    candidate: SourceCandidate,
    *,
    dataset_metadata: Mapping[str, Any] | None = None,
) -> tuple[AuthoredLocalStyle, ...]:
    """Build every deterministic SLD bound to one reviewed source."""

    try:
        reviewed = reviewed_local_style_recipes(candidate)
    except SourceDiscoveryError as error:
        raise LocalStyleAdaptationError(
            "reviewed local style source evidence is invalid",
            code=error.code,
        ) from error
    return tuple(
        _generate_reviewed_local_style(
            item,
            dataset_metadata=dataset_metadata,
        )
        for item in reviewed
    )


def _generate_reviewed_local_style(
    reviewed: ReviewedLocalStyleRecipe,
    *,
    dataset_metadata: Mapping[str, Any] | None,
) -> AuthoredLocalStyle:
    if reviewed.style_kind == "catastro_parcels":
        recipe = _catastro_recipe(reviewed.style_reference)
    elif reviewed.style_kind == "eurostat_grid":
        recipe = _eurostat_grid_recipe(
            reviewed.profile,
            reviewed.catalog_style_source_key,
            reviewed.style_reference,
        )
    elif reviewed.style_kind == "flood_polygons":
        recipe = _flood_recipe(
            reviewed.profile,
            reviewed.style_reference,
        )
    elif reviewed.style_kind == "idecyl_polygon_outline":
        recipe = _idecyl_polygon_outline_recipe(
            reviewed.profile,
            reviewed.catalog_style_source_key,
            reviewed.style_reference,
            dataset_metadata,
        )
    elif reviewed.style_kind == "idecyl_nitrate_year":
        recipe = _idecyl_nitrate_year_recipe(
            reviewed.profile,
            reviewed.catalog_style_source_key,
            reviewed.style_reference,
            dataset_metadata,
        )
    elif reviewed.style_kind == "ines_raster":
        recipe = _ines_recipe(
            reviewed.profile,
            reviewed.style_reference,
            dataset_metadata,
        )
    else:  # pragma: no cover - source discovery constrains this literal.
        raise LocalStyleAdaptationError(
            "reviewed local style kind is unsupported",
            code="local_style_recipe_invalid",
        )
    document = _render_recipe_sld(
        profile=reviewed.profile,
        style_kind=reviewed.style_kind,
        layer_name=reviewed.selected_layer_name,
        style_name=reviewed.remote_style_name,
        recipe=recipe,
    )

    if not document or len(document) > MAX_LOCAL_STYLE_BYTES:
        raise LocalStyleAdaptationError(
            "generated local SLD exceeds its byte limit",
            code="local_style_size_limit",
        )
    _validate_generated_sld(
        document,
        layer_name=reviewed.selected_layer_name,
        style_name=reviewed.remote_style_name,
        style_kind=reviewed.style_kind,
        recipe=recipe,
    )
    sld_sha256 = hashlib.sha256(document).hexdigest()
    recipe_sha256 = canonical_json_sha256(recipe)
    idecyl_adaptation = (
        reviewed.style_kind
        in {"idecyl_polygon_outline", "idecyl_nitrate_year"}
    )
    evidence = {
        "schema": (
            IDECYL_AUTHORED_STYLE_SCHEMA
            if idecyl_adaptation
            else AUTHORED_STYLE_SCHEMA
        ),
        "generator_version": (
            IDECYL_AUTHORED_STYLE_GENERATOR
            if idecyl_adaptation
            else AUTHORED_STYLE_GENERATOR
        ),
        "profile": reviewed.profile,
        "style_kind": reviewed.style_kind,
        "parity_kind": "adapted",
        "exact_style_claim": False,
        "catalog_style_source_key": reviewed.catalog_style_source_key,
        "remote_style_name": reviewed.remote_style_name,
        "catalog_layer_name": reviewed.catalog_layer_name,
        "selected_layer_name": reviewed.selected_layer_name,
        "reviewed_equivalence_sha256": (
            reviewed.reviewed_equivalence_sha256
        ),
        "recipe": recipe,
        "recipe_sha256": recipe_sha256,
        "sld_sha256": sld_sha256,
        "resource_count": 0,
    }
    if idecyl_adaptation:
        if (
            reviewed.audit_layer_id is None
            or not isinstance(reviewed.catalog_style_is_default, bool)
            or reviewed.source_definition is None
            or reviewed.source_definition_sha256 is None
        ):
            raise LocalStyleAdaptationError(
                "IDECyL local style lacks its source binding",
                code="local_style_evidence_invalid",
            )
        evidence.update(
            {
                "audit_layer_id": reviewed.audit_layer_id,
                "catalog_style_is_default": (
                    reviewed.catalog_style_is_default
                ),
                "source_definition": deepcopy(reviewed.source_definition),
                "source_definition_sha256": (
                    reviewed.source_definition_sha256
                ),
                "dataset_schema_sha256": recipe[
                    "dataset_schema_sha256"
                ],
            }
        )
    evidence_sha256 = canonical_json_sha256(evidence)
    metadata = {
        "schema": STYLE_ARTIFACT_SCHEMA,
        "catalog_style_source_key": reviewed.catalog_style_source_key,
        "remote_name": reviewed.remote_style_name,
        "style_layer_name": reviewed.selected_layer_name,
        "parity_kind": "adapted",
        "resource_bindings": [],
        "unresolved_resources": [],
        "authored_local_evidence": evidence,
        "authored_local_evidence_sha256": evidence_sha256,
    }
    return AuthoredLocalStyle(
        document=document,
        sld_sha256=sld_sha256,
        metadata=metadata,
        evidence_sha256=evidence_sha256,
    )


def local_style_package_metadata(
    authored: AuthoredLocalStyle,
) -> dict[str, Any]:
    """Return hash-bound metadata for a deterministic zero-resource package."""

    metadata = authored.metadata
    return {
        "schema": STYLE_PACKAGE_SCHEMA,
        "package_format": STYLE_PACKAGE_FORMAT,
        "catalog_style_source_key": metadata[
            "catalog_style_source_key"
        ],
        "remote_name": metadata["remote_name"],
        "style_layer_name": metadata["style_layer_name"],
        "sld_sha256": authored.sld_sha256,
        "resource_bindings": [],
        "package_members": [
            {
                "path": "style.sld",
                "sha256": authored.sld_sha256,
            }
        ],
        "authored_local_evidence_sha256": authored.evidence_sha256,
    }


def validate_zero_resource_local_adaptation(
    *,
    style_metadata: Mapping[str, Any],
    package_metadata: Mapping[str, Any],
    sld_sha256: str,
) -> None:
    """Verify strict authored evidence before accepting adapted/no-resource."""

    expected_style_keys = {
        "schema",
        "catalog_style_source_key",
        "remote_name",
        "style_layer_name",
        "parity_kind",
        "resource_bindings",
        "unresolved_resources",
        "authored_local_evidence",
        "authored_local_evidence_sha256",
    }
    if (
        set(style_metadata) != expected_style_keys
        or style_metadata.get("schema") != STYLE_ARTIFACT_SCHEMA
        or style_metadata.get("parity_kind") != "adapted"
        or style_metadata.get("resource_bindings") != []
        or style_metadata.get("unresolved_resources") != []
        or _SHA256_RE.fullmatch(sld_sha256) is None
    ):
        raise LocalStyleAdaptationError(
            "authored local style metadata is invalid",
            code="local_style_evidence_invalid",
        )
    evidence = style_metadata.get("authored_local_evidence")
    evidence_sha256 = style_metadata.get(
        "authored_local_evidence_sha256"
    )
    if (
        not isinstance(evidence, Mapping)
        or not isinstance(evidence_sha256, str)
        or canonical_json_sha256(evidence) != evidence_sha256
    ):
        raise LocalStyleAdaptationError(
            "authored local style evidence hash is invalid",
            code="local_style_evidence_invalid",
        )
    base_evidence_keys = {
        "schema",
        "generator_version",
        "profile",
        "style_kind",
        "parity_kind",
        "exact_style_claim",
        "catalog_style_source_key",
        "remote_style_name",
        "catalog_layer_name",
        "selected_layer_name",
        "reviewed_equivalence_sha256",
        "recipe",
        "recipe_sha256",
        "sld_sha256",
        "resource_count",
    }
    idecyl_evidence = (
        evidence.get("schema") == IDECYL_AUTHORED_STYLE_SCHEMA
    )
    expected_evidence_keys = (
        base_evidence_keys
        | {
            "audit_layer_id",
            "catalog_style_is_default",
            "source_definition",
            "source_definition_sha256",
            "dataset_schema_sha256",
        }
        if idecyl_evidence
        else base_evidence_keys
    )
    profile = evidence.get("profile")
    catalog_style_source_key = evidence.get(
        "catalog_style_source_key"
    )
    identity = (
        reviewed_local_style_profile_identity(
            profile,
            catalog_style_source_key,
        )
        if isinstance(profile, str)
        and isinstance(catalog_style_source_key, str)
        else None
    )
    if (
        set(evidence) != expected_evidence_keys
        or evidence.get("schema")
        != (
            IDECYL_AUTHORED_STYLE_SCHEMA
            if idecyl_evidence
            else AUTHORED_STYLE_SCHEMA
        )
        or evidence.get("generator_version")
        != (
            IDECYL_AUTHORED_STYLE_GENERATOR
            if idecyl_evidence
            else AUTHORED_STYLE_GENERATOR
        )
        or evidence.get("parity_kind") != "adapted"
        or evidence.get("exact_style_claim") is not False
        or evidence.get("resource_count") != 0
        or evidence.get("sld_sha256") != sld_sha256
        or identity is None
        or evidence.get("style_kind") != identity[0]
        or evidence.get("catalog_style_source_key") != identity[1]
        or evidence.get("remote_style_name") != identity[2]
        or evidence.get("catalog_layer_name") != identity[3]
        or evidence.get("selected_layer_name") != identity[4]
        or evidence.get("reviewed_equivalence_sha256") != identity[5]
        or style_metadata.get("catalog_style_source_key") != identity[1]
        or style_metadata.get("remote_name") != identity[2]
        or style_metadata.get("style_layer_name")
        != evidence.get("selected_layer_name")
        or _SHA256_RE.fullmatch(
            str(evidence.get("reviewed_equivalence_sha256", ""))
        )
        is None
    ):
        raise LocalStyleAdaptationError(
            "authored local style evidence is not allowlisted",
            code="local_style_evidence_invalid",
        )
    recipe = evidence.get("recipe")
    if (
        not isinstance(recipe, Mapping)
        or evidence.get("recipe_sha256") != canonical_json_sha256(recipe)
        or recipe.get("source_reference")
        != reviewed_local_style_profile_reference(
            profile,
            catalog_style_source_key,
        )
    ):
        raise LocalStyleAdaptationError(
            "authored local style recipe hash is invalid",
            code="local_style_evidence_invalid",
        )
    if idecyl_evidence:
        source_identity = (
            reviewed_local_style_expected_source_definition(
                profile,
                catalog_style_source_key,
            )
        )
        source_reference = recipe.get("source_reference")
        source_definition = evidence.get("source_definition")
        source_definition_sha256 = evidence.get(
            "source_definition_sha256"
        )
        if (
            source_identity is None
            or not isinstance(source_definition, Mapping)
            or dict(source_definition) != source_identity[0]
            or source_definition_sha256 != source_identity[1]
            or canonical_json_sha256(source_definition)
            != source_definition_sha256
            or not isinstance(source_reference, Mapping)
            or evidence.get("audit_layer_id")
            != source_reference.get("audit_layer_id")
            or evidence.get("catalog_style_is_default")
            is not source_reference.get("catalog_style", {}).get(
                "is_default"
            )
            or evidence.get("dataset_schema_sha256")
            != recipe.get("dataset_schema_sha256")
            or _SHA256_RE.fullmatch(
                str(evidence.get("dataset_schema_sha256", ""))
            )
            is None
        ):
            raise LocalStyleAdaptationError(
                "authored IDECyL source or schema binding is invalid",
                code="local_style_evidence_invalid",
            )
    expected_sld = _render_recipe_sld(
        profile=profile,
        style_kind=identity[0],
        layer_name=identity[4],
        style_name=identity[2],
        recipe=recipe,
    )
    if hashlib.sha256(expected_sld).hexdigest() != sld_sha256:
        raise LocalStyleAdaptationError(
            "authored local SLD does not match its deterministic recipe",
            code="local_style_evidence_invalid",
        )

    expected_package_keys = {
        "schema",
        "package_format",
        "catalog_style_source_key",
        "remote_name",
        "style_layer_name",
        "sld_sha256",
        "resource_bindings",
        "package_members",
        "authored_local_evidence_sha256",
    }
    if (
        set(package_metadata) != expected_package_keys
        or package_metadata.get("schema") != STYLE_PACKAGE_SCHEMA
        or package_metadata.get("package_format") != STYLE_PACKAGE_FORMAT
        or package_metadata.get("catalog_style_source_key") != identity[1]
        or package_metadata.get("remote_name") != identity[2]
        or package_metadata.get("style_layer_name")
        != evidence.get("selected_layer_name")
        or package_metadata.get("sld_sha256") != sld_sha256
        or package_metadata.get("resource_bindings") != []
        or package_metadata.get("package_members")
        != [{"path": "style.sld", "sha256": sld_sha256}]
        or package_metadata.get("authored_local_evidence_sha256")
        != evidence_sha256
    ):
        raise LocalStyleAdaptationError(
            "authored local style package evidence is invalid",
            code="local_style_package_evidence_invalid",
        )


def is_verified_zero_resource_local_adaptation(
    *,
    style_metadata: Mapping[str, Any],
    package_metadata: Mapping[str, Any],
    sld_sha256: str,
) -> bool:
    try:
        validate_zero_resource_local_adaptation(
            style_metadata=style_metadata,
            package_metadata=package_metadata,
            sld_sha256=sld_sha256,
        )
    except LocalStyleAdaptationError:
        return False
    return True


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
        raise LocalStyleAdaptationError(
            "local style evidence is not canonical JSON",
            code="local_style_evidence_invalid",
        ) from error
    if len(encoded) > MAX_LOCAL_STYLE_BYTES:
        raise LocalStyleAdaptationError(
            "local style evidence exceeds its byte limit",
            code="local_style_evidence_oversized",
        )
    return hashlib.sha256(encoded).hexdigest()


def _catastro_recipe(reference: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "source_kind": "reviewed-local-cartographic-reproduction",
        "adaptation_status": "adaptation_required",
        "parity_claim": "reviewed_local_adaptation_not_exact",
        "polygon_fill_opacity": 0,
        "outline_color": "#000000",
        "outline_width": 1,
        "label_field": "label",
        "font_family": "DejaVu Sans",
        "font_size": 10,
        "label_color": "#000000",
    }
    if dict(reference) != expected:
        raise LocalStyleAdaptationError(
            "Catastro local style reference is invalid",
            code="local_style_recipe_invalid",
        )
    return {
        "schema": "siur-local-style-symbolizer/v1",
        "symbolizer": "polygon-and-label",
        "fill_color": "#ffffff",
        "fill_opacity": 0,
        "outline_color": "#000000",
        "outline_width": 1,
        "label_field": "label",
        "font_family": "DejaVu Sans",
        "font_size": 10,
        "label_color": "#000000",
        "adaptation_status": "adapted",
        "exact_style_claim": False,
        "source_reference": expected,
    }


def _eurostat_grid_recipe(
    profile: str,
    catalog_style_source_key: str,
    reference: Mapping[str, Any],
) -> dict[str, Any]:
    expected = reviewed_local_style_profile_reference(
        profile,
        catalog_style_source_key,
    )
    if (
        expected is None
        or dict(reference) != expected
        or reference.get("source_kind")
        != "siur-owned-deterministic-style"
        or reference.get("adaptation_status")
        != "adaptation_required"
        or reference.get("parity_claim")
        != "siur_local_adaptation_not_exact"
        or reference.get("fill_opacity") != 0
        or reference.get("outline_color")
        not in {"#6d28d9", "#ffffff", "#e6007e"}
        or reference.get("outline_width") != 1
        or reference.get("max_scale_denominator") != 4_000_000
        or not isinstance(reference.get("style_identity_sha256"), str)
        or _SHA256_RE.fullmatch(reference["style_identity_sha256"])
        is None
    ):
        raise LocalStyleAdaptationError(
            "Eurostat grid local style reference is invalid",
            code="local_style_recipe_invalid",
        )
    return {
        "schema": "siur-local-style-symbolizer/v1",
        "symbolizer": "polygon",
        "catalog_style_source_key": catalog_style_source_key,
        "fill_color": "#ffffff",
        "fill_opacity": 0,
        "outline_color": reference["outline_color"],
        "outline_width": 1,
        "max_scale_denominator": 4_000_000,
        "adaptation_status": "adapted",
        "exact_style_claim": False,
        "source_reference": dict(expected),
    }


def _flood_recipe(
    profile: str,
    reference: Mapping[str, Any],
) -> dict[str, Any]:
    expected_colors = _FLOOD_COLORS.get(profile)
    if (
        expected_colors is None
        or reference.get("source_kind")
        != "archived-official-mvt-json"
        or reference.get("adaptation_status") != "adaptation_required"
        or reference.get("parity_claim")
        != "official_mvt_style_adapted_to_sld_not_exact"
        or reference.get("fill_color") != expected_colors[0]
        or reference.get("outline_color") != expected_colors[1]
        or not isinstance(reference.get("url"), str)
        or not reference["url"].startswith("https://")
    ):
        raise LocalStyleAdaptationError(
            "flood local style reference is invalid",
            code="local_style_recipe_invalid",
        )
    return {
        "schema": "siur-local-style-symbolizer/v1",
        "symbolizer": "polygon",
        "fill_color": expected_colors[0],
        "fill_opacity": 1,
        "outline_color": expected_colors[1],
        "outline_width": 1,
        "adaptation_status": "adapted",
        "exact_style_claim": False,
        "source_reference": dict(reference),
    }


def _idecyl_polygon_outline_recipe(
    profile: str,
    catalog_style_source_key: str,
    reference: Mapping[str, Any],
    dataset_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    expected = reviewed_local_style_profile_reference(
        profile,
        catalog_style_source_key,
    )
    if expected is None or dict(reference) != expected:
        raise LocalStyleAdaptationError(
            "IDECyL local-style reference is invalid",
            code="local_style_recipe_invalid",
        )
    expected_dataset = expected.get("dataset_inspection")
    visual = expected.get("visual_recipe")
    if (
        not isinstance(expected_dataset, Mapping)
        or not isinstance(visual, Mapping)
        or expected.get("audit_layer_id") != 86
        or expected.get("parity_kind") != "adapted"
        or expected.get("exact_style_claim") is not False
        or visual.get("schema")
        != "siur-idecyl-simple-vector-style/v1"
        or visual.get("symbolizer") != "polygon"
        or visual.get("fill_color") != "#ffffff"
        or visual.get("fill_opacity") != 0
        or visual.get("outline_color") != "#000000"
        or visual.get("outline_width") != 1
    ):
        raise LocalStyleAdaptationError(
            "IDECyL local visual recipe is invalid",
            code="local_style_recipe_invalid",
        )
    if not isinstance(dataset_metadata, Mapping):
        raise LocalStyleAdaptationError(
            "IDECyL local style requires inspected GeoPackage evidence",
            code="local_style_dataset_schema_missing",
        )
    inspection = dataset_metadata.get("geopackage_inspection")
    if not isinstance(inspection, Mapping):
        raise LocalStyleAdaptationError(
            "IDECyL local style requires inspected GeoPackage evidence",
            code="local_style_dataset_schema_missing",
        )
    data_schema = inspection.get("data_schema")
    if not isinstance(data_schema, list):
        raise LocalStyleAdaptationError(
            "IDECyL GeoPackage schema evidence is invalid",
            code="local_style_dataset_schema_changed",
        )
    compatibility = {
        "inspection_schema": inspection.get("schema_version"),
        "archive_member": inspection.get("archive_member"),
        "feature_layer": inspection.get("feature_layer"),
        "feature_layers": inspection.get("feature_layers"),
        "geometry_column": inspection.get("geometry_column"),
        "geometry_type": inspection.get("geometry_type"),
        "srs": inspection.get("crs"),
        "data_schema": data_schema,
        "data_schema_sha256": inspection.get("data_schema_sha256"),
    }
    expected_compatibility = {
        key: deepcopy(expected_dataset[key])
        for key in (
            "inspection_schema",
            "archive_member",
            "feature_layer",
            "feature_layers",
            "geometry_column",
            "geometry_type",
            "srs",
            "data_schema",
            "data_schema_sha256",
        )
    }
    if (
        compatibility != expected_compatibility
        or dataset_metadata.get("input_layer")
        != expected_dataset["feature_layer"]
        or canonical_json_sha256(data_schema)
        != expected_dataset["data_schema_sha256"]
    ):
        raise LocalStyleAdaptationError(
            "IDECyL GeoPackage schema or geometry changed",
            code="local_style_dataset_schema_changed",
        )
    return {
        "schema": "siur-local-style-symbolizer/v1",
        "symbolizer": "polygon",
        "fill_color": visual["fill_color"],
        "fill_opacity": visual["fill_opacity"],
        "outline_color": visual["outline_color"],
        "outline_width": visual["outline_width"],
        "dataset_inspection": compatibility,
        "dataset_schema_sha256": canonical_json_sha256(compatibility),
        "adaptation_status": "adapted",
        "exact_style_claim": False,
        "source_reference": deepcopy(expected),
    }


def _idecyl_nitrate_year_recipe(
    profile: str,
    catalog_style_source_key: str,
    reference: Mapping[str, Any],
    dataset_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    expected = reviewed_local_style_profile_reference(
        profile,
        catalog_style_source_key,
    )
    if expected is None or dict(reference) != expected:
        raise LocalStyleAdaptationError(
            "IDECyL nitrate style reference is invalid",
            code="local_style_recipe_invalid",
        )
    catalog_style = expected.get("catalog_style")
    temporal = expected.get("temporal_adaptation")
    template = expected.get("style_template")
    expected_dataset = expected.get("dataset_inspection")
    if (
        not isinstance(catalog_style, Mapping)
        or not isinstance(temporal, Mapping)
        or not isinstance(template, Mapping)
        or not isinstance(expected_dataset, Mapping)
        or expected.get("audit_layer_id") != 268
        or expected.get("parity_kind") != "adapted"
        or expected.get("exact_style_claim") is not False
        or temporal.get("schema")
        != "siur-idecyl-temporal-rule-semantic-adaptation/v1"
        or temporal.get("source_property_name") != "v_nitr2021"
        or temporal.get("target_property_name")
        != catalog_style.get("property_name")
        or temporal.get("classification_rule_semantics_preserved")
        is not True
        or temporal.get("property_name_is_only_rule_semantic_change")
        is not True
        or temporal.get("runtime_sld_version") != "1.0.0"
        or temporal.get("catalog_style_identity_applied") is not True
        or temporal.get("source_descriptions_omitted") is not True
        or template.get("adaptation_contract")
        != {
            "catalog_style_identity_applied": True,
            "classification_rule_semantics_preserved": True,
            "property_name_is_only_rule_semantic_change": True,
            "runtime_sld_version": "1.0.0",
            "source_descriptions_omitted": True,
            "source_sld_version": "1.1.0",
        }
        or template.get("template_property_name") != "v_nitr2021"
        or template.get("archive_member_sha256")
        != (
            "ed4dff6410973ffd0e672a1a4216a7d"
            "11dab2118bc3b0a6056b2d1cfc2b87c38"
        )
        or not isinstance(template.get("classification_rules"), list)
        or len(template["classification_rules"]) != 5
    ):
        raise LocalStyleAdaptationError(
            "IDECyL nitrate temporal recipe is invalid",
            code="local_style_recipe_invalid",
        )
    if not isinstance(dataset_metadata, Mapping):
        raise LocalStyleAdaptationError(
            "IDECyL nitrate style requires inspected GeoPackage evidence",
            code="local_style_dataset_schema_missing",
        )
    inspection = dataset_metadata.get("geopackage_inspection")
    if not isinstance(inspection, Mapping):
        raise LocalStyleAdaptationError(
            "IDECyL nitrate style requires inspected GeoPackage evidence",
            code="local_style_dataset_schema_missing",
        )
    data_schema = inspection.get("data_schema")
    compatibility = {
        "inspection_schema": inspection.get("schema_version"),
        "archive_member": inspection.get("archive_member"),
        "feature_layer": inspection.get("feature_layer"),
        "feature_layers": inspection.get("feature_layers"),
        "geometry_column": inspection.get("geometry_column"),
        "geometry_type": inspection.get("geometry_type"),
        "srs": inspection.get("crs"),
        "data_schema": data_schema,
        "data_schema_sha256": inspection.get("data_schema_sha256"),
    }
    expected_compatibility = {
        "inspection_schema": expected_dataset.get("inspection_schema"),
        "archive_member": expected_dataset.get("archive_member"),
        "feature_layer": expected_dataset.get("feature_layer"),
        "feature_layers": expected_dataset.get("feature_layers"),
        "geometry_column": expected_dataset.get("geometry_column"),
        "geometry_type": expected_dataset.get("geometry_type"),
        "srs": expected_dataset.get("srs"),
        "data_schema": expected_dataset.get("data_schema"),
        "data_schema_sha256": expected_dataset.get(
            "data_schema_sha256"
        ),
    }
    property_name = catalog_style.get("property_name")
    schema_names = {
        item.get("name")
        for item in data_schema
        if isinstance(item, Mapping)
    } if isinstance(data_schema, list) else set()
    if (
        compatibility != expected_compatibility
        or dataset_metadata.get("input_layer")
        != expected_dataset.get("feature_layer")
        or canonical_json_sha256(data_schema)
        != expected_dataset.get("data_schema_sha256")
        or not isinstance(property_name, str)
        or property_name not in schema_names
    ):
        raise LocalStyleAdaptationError(
            "IDECyL nitrate GeoPackage schema or year field changed",
            code="local_style_dataset_schema_changed",
        )
    return {
        "schema": "siur-local-style-polygon-classification/v1",
        "symbolizer": "polygon-classification",
        "property_name": property_name,
        "classification_rules": deepcopy(
            template["classification_rules"]
        ),
        "semantic_adaptation": deepcopy(temporal),
        "template_sld_sha256": template["archive_member_sha256"],
        "dataset_inspection": compatibility,
        "dataset_schema_sha256": canonical_json_sha256(compatibility),
        "adaptation_status": "adapted",
        "exact_style_claim": False,
        "source_reference": deepcopy(expected),
    }


def _ines_recipe(
    profile: str,
    reference: Mapping[str, Any],
    dataset_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(dataset_metadata, Mapping):
        raise LocalStyleAdaptationError(
            "INES style requires validated raster VAT evidence",
            code="local_style_vat_missing",
        )
    style_reference = reference.get("official_style_reference")
    raster_reference = reference.get("official_raster_style_evidence")
    vat = dataset_metadata.get("raster_value_attribute_table")
    if (
        not isinstance(style_reference, Mapping)
        or not isinstance(raster_reference, Mapping)
        or not isinstance(vat, Mapping)
    ):
        raise LocalStyleAdaptationError(
            "INES style evidence is incomplete",
            code="local_style_vat_invalid",
        )
    raw_palette = style_reference.get("palette")
    expected_palette = [
        {"class_value": value, "label": label, "color": color}
        for value, label, color in _INES_PALETTE
    ]
    if (
        style_reference.get("adaptation_status")
        != "adaptation_required"
        or style_reference.get("parity_claim")
        != "official_historical_adaptation_not_exact"
        or raw_palette != expected_palette
        or raster_reference.get("adaptation_status")
        != "adaptation_required"
        or raster_reference.get("tiff_color_map_present") is not False
        or raster_reference.get("embedded_style_files") != []
    ):
        raise LocalStyleAdaptationError(
            "INES reviewed palette evidence is invalid",
            code="local_style_recipe_invalid",
        )
    attribute_reference = raster_reference.get("attribute_table")
    expected_class_field = (
        "EroPot_pb"
        if profile == "miteco-ines-potential-cyl-geotiff-download-v1"
        else "EroLam_pb"
        if profile == "miteco-ines-laminar-cyl-geotiff-download-v1"
        else None
    )
    raw_mapping = vat.get("value_class_mapping")
    if (
        expected_class_field is None
        or not isinstance(attribute_reference, Mapping)
        or attribute_reference.get("classification_field")
        != expected_class_field
        or attribute_reference.get("class_values") != list(range(1, 10))
        or attribute_reference.get("color_fields") != []
        or vat.get("value_field") != "Value"
        or vat.get("class_field") != expected_class_field
        or vat.get("expected_class_values") != list(range(1, 10))
        or not isinstance(vat.get("member"), str)
        or not vat["member"]
        or not isinstance(vat.get("sha256"), str)
        or _SHA256_RE.fullmatch(vat["sha256"]) is None
        or not isinstance(raw_mapping, list)
        or not 1 <= len(raw_mapping) <= MAX_VAT_ROWS
        or vat.get("row_count") != len(raw_mapping)
    ):
        raise LocalStyleAdaptationError(
            "INES raster VAT provenance is invalid",
            code="local_style_vat_invalid",
        )
    mapping: list[dict[str, int]] = []
    seen_values: set[int] = set()
    observed_classes: set[int] = set()
    for item in raw_mapping:
        value = item.get("value") if isinstance(item, Mapping) else None
        class_value = (
            item.get("class_value") if isinstance(item, Mapping) else None
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 2**31 - 1
            or value in seen_values
            or isinstance(class_value, bool)
            or not isinstance(class_value, int)
            or class_value not in range(1, 10)
        ):
            raise LocalStyleAdaptationError(
                "INES raster VAT mapping is invalid",
                code="local_style_vat_invalid",
            )
        seen_values.add(value)
        observed_classes.add(class_value)
        mapping.append({"value": value, "class_value": class_value})
    if (
        mapping != sorted(mapping, key=lambda item: item["value"])
        or observed_classes != set(range(1, 10))
    ):
        raise LocalStyleAdaptationError(
            "INES raster VAT mapping is incomplete or non-canonical",
            code="local_style_vat_invalid",
        )
    palette = expected_palette
    return {
        "schema": "siur-local-style-raster-colormap/v1",
        "symbolizer": "raster-colormap-values",
        "color_map_type": "values",
        "palette": palette,
        "palette_sha256": canonical_json_sha256(palette),
        "vat_member": vat["member"],
        "vat_sha256": vat["sha256"],
        "vat_value_field": "Value",
        "vat_class_field": expected_class_field,
        "vat_row_count": len(mapping),
        "value_class_mapping": mapping,
        "value_class_mapping_sha256": canonical_json_sha256(mapping),
        "adaptation_status": "adapted",
        "exact_style_claim": False,
        "source_reference": {
            "official_style_reference": dict(style_reference),
            "official_raster_style_evidence": dict(raster_reference),
        },
    }


def _validate_recipe(
    profile: str,
    style_kind: str,
    recipe: Mapping[str, Any],
) -> None:
    if style_kind == "catastro_parcels":
        rebuilt = _catastro_recipe(recipe.get("source_reference", {}))
    elif style_kind == "eurostat_grid":
        catalog_style_source_key = recipe.get(
            "catalog_style_source_key"
        )
        if not isinstance(catalog_style_source_key, str):
            raise LocalStyleAdaptationError(
                "Eurostat grid recipe identity is invalid",
                code="local_style_evidence_invalid",
            )
        rebuilt = _eurostat_grid_recipe(
            profile,
            catalog_style_source_key,
            recipe.get("source_reference", {}),
        )
    elif style_kind == "flood_polygons":
        rebuilt = _flood_recipe(
            profile,
            recipe.get("source_reference", {}),
        )
    elif style_kind == "idecyl_polygon_outline":
        source_reference = recipe.get("source_reference")
        inspection = recipe.get("dataset_inspection")
        catalog_style = (
            source_reference.get("catalog_style")
            if isinstance(source_reference, Mapping)
            else None
        )
        catalog_style_source_key = (
            catalog_style.get("catalog_style_source_key")
            if isinstance(catalog_style, Mapping)
            else None
        )
        if (
            not isinstance(source_reference, Mapping)
            or not isinstance(inspection, Mapping)
            or not isinstance(catalog_style_source_key, str)
        ):
            raise LocalStyleAdaptationError(
                "persisted IDECyL style recipe is invalid",
                code="local_style_evidence_invalid",
            )
        rebuilt = _idecyl_polygon_outline_recipe(
            profile,
            catalog_style_source_key,
            source_reference,
            {
                "input_layer": inspection.get("feature_layer"),
                "geopackage_inspection": {
                    "schema_version": inspection.get(
                        "inspection_schema"
                    ),
                    "archive_member": inspection.get("archive_member"),
                    "feature_layer": inspection.get("feature_layer"),
                    "feature_layers": inspection.get("feature_layers"),
                    "geometry_column": inspection.get(
                        "geometry_column"
                    ),
                    "geometry_type": inspection.get("geometry_type"),
                    "crs": inspection.get("srs"),
                    "data_schema": inspection.get("data_schema"),
                    "data_schema_sha256": inspection.get(
                        "data_schema_sha256"
                    ),
                },
            },
        )
    elif style_kind == "idecyl_nitrate_year":
        source_reference = recipe.get("source_reference")
        inspection = recipe.get("dataset_inspection")
        catalog_style = (
            source_reference.get("catalog_style")
            if isinstance(source_reference, Mapping)
            else None
        )
        catalog_style_source_key = (
            catalog_style.get("catalog_style_source_key")
            if isinstance(catalog_style, Mapping)
            else None
        )
        if (
            not isinstance(source_reference, Mapping)
            or not isinstance(inspection, Mapping)
            or not isinstance(catalog_style_source_key, str)
        ):
            raise LocalStyleAdaptationError(
                "persisted IDECyL nitrate recipe is invalid",
                code="local_style_evidence_invalid",
            )
        rebuilt = _idecyl_nitrate_year_recipe(
            profile,
            catalog_style_source_key,
            source_reference,
            {
                "input_layer": inspection.get("feature_layer"),
                "geopackage_inspection": {
                    "schema_version": inspection.get(
                        "inspection_schema"
                    ),
                    "archive_member": inspection.get("archive_member"),
                    "feature_layer": inspection.get("feature_layer"),
                    "feature_layers": inspection.get("feature_layers"),
                    "geometry_column": inspection.get(
                        "geometry_column"
                    ),
                    "geometry_type": inspection.get("geometry_type"),
                    "crs": inspection.get("srs"),
                    "data_schema": inspection.get("data_schema"),
                    "data_schema_sha256": inspection.get(
                        "data_schema_sha256"
                    ),
                },
            },
        )
    elif style_kind == "ines_raster":
        _validate_persisted_ines_recipe(profile, recipe)
        return
    else:
        raise LocalStyleAdaptationError(
            "local style recipe kind is invalid",
            code="local_style_evidence_invalid",
        )
    if dict(recipe) != rebuilt:
        raise LocalStyleAdaptationError(
            "local style recipe semantics are invalid",
            code="local_style_evidence_invalid",
        )


def _validate_persisted_ines_recipe(
    profile: str,
    recipe: Mapping[str, Any],
) -> None:
    source_reference = recipe.get("source_reference")
    mapping = recipe.get("value_class_mapping")
    if not isinstance(source_reference, Mapping) or not isinstance(mapping, list):
        raise LocalStyleAdaptationError(
            "persisted INES recipe is invalid",
            code="local_style_evidence_invalid",
        )
    rebuilt = _ines_recipe(
        profile,
        source_reference,
        {
            "raster_value_attribute_table": {
                "member": recipe.get("vat_member"),
                "sha256": recipe.get("vat_sha256"),
                "row_count": recipe.get("vat_row_count"),
                "value_field": recipe.get("vat_value_field"),
                "class_field": recipe.get("vat_class_field"),
                "expected_class_values": list(range(1, 10)),
                "value_class_mapping": mapping,
            }
        },
    )
    if dict(recipe) != rebuilt:
        raise LocalStyleAdaptationError(
            "persisted INES recipe semantics are invalid",
            code="local_style_evidence_invalid",
        )


def _render_recipe_sld(
    *,
    profile: str,
    style_kind: str,
    layer_name: str,
    style_name: str,
    recipe: Mapping[str, Any],
) -> bytes:
    """Regenerate the only SLD bytes permitted by persisted recipe evidence."""

    _validate_recipe(profile, style_kind, recipe)
    if style_kind == "catastro_parcels":
        return _vector_sld(
            layer_name=layer_name,
            style_name=style_name,
            title="Parcelas catastrales — adaptación local",
            fill_color=cast(str, recipe["fill_color"]),
            fill_opacity=str(recipe["fill_opacity"]),
            outline_color=cast(str, recipe["outline_color"]),
            outline_width=str(recipe["outline_width"]),
            label_field=cast(str, recipe["label_field"]),
        )
    if style_kind == "eurostat_grid":
        return _vector_sld(
            layer_name=layer_name,
            style_name=style_name,
            title="Rejilla Eurostat — adaptación local SIUR",
            fill_color=cast(str, recipe["fill_color"]),
            fill_opacity=str(recipe["fill_opacity"]),
            outline_color=cast(str, recipe["outline_color"]),
            outline_width=str(recipe["outline_width"]),
            label_field=None,
            max_scale_denominator=str(
                recipe["max_scale_denominator"]
            ),
        )
    if style_kind == "flood_polygons":
        return _vector_sld(
            layer_name=layer_name,
            style_name=style_name,
            title="Inundabilidad — adaptación local",
            fill_color=cast(str, recipe["fill_color"]),
            fill_opacity=str(recipe["fill_opacity"]),
            outline_color=cast(str, recipe["outline_color"]),
            outline_width=str(recipe["outline_width"]),
            label_field=None,
        )
    if style_kind == "idecyl_polygon_outline":
        return _vector_sld(
            layer_name=layer_name,
            style_name=style_name,
            title="Cuadrícula minera — adaptación local SIUR",
            fill_color=cast(str, recipe["fill_color"]),
            fill_opacity=str(recipe["fill_opacity"]),
            outline_color=cast(str, recipe["outline_color"]),
            outline_width=str(recipe["outline_width"]),
            label_field=None,
        )
    if style_kind == "idecyl_nitrate_year":
        return _nitrate_sld(
            layer_name=layer_name,
            style_name=style_name,
            title=(
                f"Nitratos {recipe['property_name'][-4:]} "
                "— adaptación local"
            ),
            property_name=cast(str, recipe["property_name"]),
            rules=cast(list[dict[str, Any]], recipe["classification_rules"]),
        )
    if style_kind == "ines_raster":
        return _raster_sld(
            layer_name=layer_name,
            style_name=style_name,
            title="INES — adaptación local de clases históricas",
            mapping=cast(
                list[dict[str, int]],
                recipe["value_class_mapping"],
            ),
            palette=cast(
                list[dict[str, Any]],
                recipe["palette"],
            ),
        )
    raise LocalStyleAdaptationError(
        "local style recipe kind is invalid",
        code="local_style_evidence_invalid",
    )


def _vector_sld(
    *,
    layer_name: str,
    style_name: str,
    title: str,
    fill_color: str,
    fill_opacity: str,
    outline_color: str,
    outline_width: str,
    label_field: str | None,
    max_scale_denominator: str | None = None,
) -> bytes:
    root, rule = _sld_document(
        layer_name=layer_name,
        style_name=style_name,
        title=title,
    )
    if max_scale_denominator is not None:
        ElementTree.SubElement(
            rule,
            _q(SLD_NAMESPACE, "MaxScaleDenominator"),
        ).text = max_scale_denominator
    polygon = ElementTree.SubElement(
        rule,
        _q(SLD_NAMESPACE, "PolygonSymbolizer"),
    )
    fill = ElementTree.SubElement(polygon, _q(SLD_NAMESPACE, "Fill"))
    _css(fill, "fill", fill_color)
    _css(fill, "fill-opacity", fill_opacity)
    stroke = ElementTree.SubElement(polygon, _q(SLD_NAMESPACE, "Stroke"))
    _css(stroke, "stroke", outline_color)
    _css(stroke, "stroke-width", outline_width)
    if label_field is not None:
        text = ElementTree.SubElement(
            rule,
            _q(SLD_NAMESPACE, "TextSymbolizer"),
        )
        label = ElementTree.SubElement(text, _q(SLD_NAMESPACE, "Label"))
        ElementTree.SubElement(
            label,
            _q(OGC_NAMESPACE, "PropertyName"),
        ).text = label_field
        font = ElementTree.SubElement(text, _q(SLD_NAMESPACE, "Font"))
        _css(font, "font-family", "DejaVu Sans")
        _css(font, "font-size", "10")
        _css(font, "font-style", "normal")
        _css(font, "font-weight", "normal")
        label_fill = ElementTree.SubElement(
            text,
            _q(SLD_NAMESPACE, "Fill"),
        )
        _css(label_fill, "fill", "#000000")
    return _serialize(root)


def _nitrate_sld(
    *,
    layer_name: str,
    style_name: str,
    title: str,
    property_name: str,
    rules: list[dict[str, Any]],
) -> bytes:
    root = ElementTree.Element(
        _q(SLD_NAMESPACE, "StyledLayerDescriptor"),
        {"version": "1.0.0"},
    )
    named_layer = ElementTree.SubElement(
        root,
        _q(SLD_NAMESPACE, "NamedLayer"),
    )
    ElementTree.SubElement(
        named_layer,
        _q(SLD_NAMESPACE, "Name"),
    ).text = layer_name
    user_style = ElementTree.SubElement(
        named_layer,
        _q(SLD_NAMESPACE, "UserStyle"),
    )
    ElementTree.SubElement(
        user_style,
        _q(SLD_NAMESPACE, "Name"),
    ).text = style_name
    ElementTree.SubElement(
        user_style,
        _q(SLD_NAMESPACE, "Title"),
    ).text = title
    feature_type = ElementTree.SubElement(
        user_style,
        _q(SLD_NAMESPACE, "FeatureTypeStyle"),
    )
    for item in rules:
        rule = ElementTree.SubElement(
            feature_type,
            _q(SLD_NAMESPACE, "Rule"),
        )
        ElementTree.SubElement(
            rule,
            _q(SLD_NAMESPACE, "Name"),
        ).text = str(item["name"])
        filter_element = ElementTree.SubElement(
            rule,
            _q(OGC_NAMESPACE, "Filter"),
        )
        _nitrate_filter(
            filter_element,
            property_name=property_name,
            spec=cast(dict[str, Any], item["filter"]),
        )
        polygon = ElementTree.SubElement(
            rule,
            _q(SLD_NAMESPACE, "PolygonSymbolizer"),
        )
        fill = ElementTree.SubElement(
            polygon,
            _q(SLD_NAMESPACE, "Fill"),
        )
        _css(fill, "fill", str(item["fill_color"]))
        _css(fill, "fill-opacity", str(item["fill_opacity"]))
        stroke = ElementTree.SubElement(
            polygon,
            _q(SLD_NAMESPACE, "Stroke"),
        )
        _css(stroke, "stroke", str(item["stroke_color"]))
        _css(stroke, "stroke-width", str(item["stroke_width"]))
        _css(stroke, "stroke-linejoin", str(item["stroke_linejoin"]))
    return _serialize(root)


def _nitrate_filter(
    parent: ElementTree.Element,
    *,
    property_name: str,
    spec: dict[str, Any],
) -> None:
    operator = spec.get("operator")
    if operator == "is_null":
        predicate = ElementTree.SubElement(
            parent,
            _q(OGC_NAMESPACE, "PropertyIsNull"),
        )
        ElementTree.SubElement(
            predicate,
            _q(OGC_NAMESPACE, "PropertyName"),
        ).text = property_name
        return
    predicates = {
        "greater_than": "PropertyIsGreaterThan",
        "greater_than_or_equal": "PropertyIsGreaterThanOrEqualTo",
        "less_than": "PropertyIsLessThan",
    }
    if operator == "range":
        conjunction = ElementTree.SubElement(
            parent,
            _q(OGC_NAMESPACE, "And"),
        )
        _comparison(
            conjunction,
            tag=predicates[str(spec["lower_operator"])],
            property_name=property_name,
            literal=spec["lower"],
        )
        _comparison(
            conjunction,
            tag=predicates[str(spec["upper_operator"])],
            property_name=property_name,
            literal=spec["upper"],
        )
        return
    if operator == "lower_bound":
        _comparison(
            parent,
            tag=predicates[str(spec["lower_operator"])],
            property_name=property_name,
            literal=spec["lower"],
        )
        return
    raise LocalStyleAdaptationError(
        "IDECyL nitrate filter is invalid",
        code="local_style_recipe_invalid",
    )


def _comparison(
    parent: ElementTree.Element,
    *,
    tag: str,
    property_name: str,
    literal: Any,
) -> None:
    predicate = ElementTree.SubElement(
        parent,
        _q(OGC_NAMESPACE, tag),
    )
    ElementTree.SubElement(
        predicate,
        _q(OGC_NAMESPACE, "PropertyName"),
    ).text = property_name
    ElementTree.SubElement(
        predicate,
        _q(OGC_NAMESPACE, "Literal"),
    ).text = str(literal)


def _raster_sld(
    *,
    layer_name: str,
    style_name: str,
    title: str,
    mapping: list[dict[str, int]],
    palette: list[dict[str, Any]],
) -> bytes:
    root, rule = _sld_document(
        layer_name=layer_name,
        style_name=style_name,
        title=title,
    )
    raster = ElementTree.SubElement(
        rule,
        _q(SLD_NAMESPACE, "RasterSymbolizer"),
    )
    color_map = ElementTree.SubElement(
        raster,
        _q(SLD_NAMESPACE, "ColorMap"),
        {"type": "values"},
    )
    palette_by_class = {
        item["class_value"]: item for item in palette
    }
    for item in mapping:
        class_item = palette_by_class[item["class_value"]]
        ElementTree.SubElement(
            color_map,
            _q(SLD_NAMESPACE, "ColorMapEntry"),
            {
                "color": class_item["color"],
                "quantity": str(item["value"]),
                "label": (
                    f"Value {item['value']} — {class_item['label']}"
                ),
                "opacity": "1",
            },
        )
    return _serialize(root)


def _sld_document(
    *,
    layer_name: str,
    style_name: str,
    title: str,
) -> tuple[ElementTree.Element, ElementTree.Element]:
    root = ElementTree.Element(
        _q(SLD_NAMESPACE, "StyledLayerDescriptor"),
        {"version": "1.0.0"},
    )
    named_layer = ElementTree.SubElement(
        root,
        _q(SLD_NAMESPACE, "NamedLayer"),
    )
    ElementTree.SubElement(
        named_layer,
        _q(SLD_NAMESPACE, "Name"),
    ).text = layer_name
    user_style = ElementTree.SubElement(
        named_layer,
        _q(SLD_NAMESPACE, "UserStyle"),
    )
    ElementTree.SubElement(
        user_style,
        _q(SLD_NAMESPACE, "Name"),
    ).text = style_name
    ElementTree.SubElement(
        user_style,
        _q(SLD_NAMESPACE, "Title"),
    ).text = title
    feature_type = ElementTree.SubElement(
        user_style,
        _q(SLD_NAMESPACE, "FeatureTypeStyle"),
    )
    rule = ElementTree.SubElement(
        feature_type,
        _q(SLD_NAMESPACE, "Rule"),
    )
    return root, rule


def _css(parent: ElementTree.Element, name: str, value: str) -> None:
    ElementTree.SubElement(
        parent,
        _q(SLD_NAMESPACE, "CssParameter"),
        {"name": name},
    ).text = value


def _serialize(root: ElementTree.Element) -> bytes:
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )


def _validate_generated_sld(
    document: bytes,
    *,
    layer_name: str,
    style_name: str,
    style_kind: str,
    recipe: Mapping[str, Any],
) -> None:
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise LocalStyleAdaptationError(
            "generated local SLD is malformed",
            code="local_style_sld_invalid",
        ) from error
    names = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag == _q(SLD_NAMESPACE, "Name")
    ]
    symbolizers = [
        _local(element.tag)
        for element in root.iter()
        if _local(element.tag).endswith("Symbolizer")
    ]
    external_reference = any(
        re.search(r"(?:https?|ftp|file|data):", value, re.IGNORECASE)
        is not None
        for element in root.iter()
        for value in (
            *(str(item) for item in element.attrib.values()),
            element.text or "",
            element.tail or "",
        )
    )
    if (
        root.tag != _q(SLD_NAMESPACE, "StyledLayerDescriptor")
        or root.get("version") != "1.0.0"
        or names[:2] != [layer_name, style_name]
        or any(token in document.lower() for token in (b"<!doctype", b"<!entity"))
        or external_reference
    ):
        raise LocalStyleAdaptationError(
            "generated local SLD identity is invalid",
            code="local_style_sld_invalid",
        )
    if style_kind == "catastro_parcels":
        if symbolizers != ["PolygonSymbolizer", "TextSymbolizer"]:
            raise LocalStyleAdaptationError(
                "Catastro SLD symbolizers are incomplete",
                code="local_style_sld_invalid",
            )
    elif style_kind == "eurostat_grid":
        maximums = [
            (element.text or "").strip()
            for element in root.iter()
            if element.tag
            == _q(SLD_NAMESPACE, "MaxScaleDenominator")
        ]
        if (
            symbolizers != ["PolygonSymbolizer"]
            or maximums != ["4000000"]
        ):
            raise LocalStyleAdaptationError(
                "Eurostat grid SLD symbolizer is incomplete",
                code="local_style_sld_invalid",
            )
    elif style_kind == "flood_polygons":
        if symbolizers != ["PolygonSymbolizer"]:
            raise LocalStyleAdaptationError(
                "flood SLD symbolizers are incomplete",
                code="local_style_sld_invalid",
            )
    elif style_kind == "idecyl_polygon_outline":
        if symbolizers != ["PolygonSymbolizer"]:
            raise LocalStyleAdaptationError(
                "IDECyL SLD symbolizer is incomplete",
                code="local_style_sld_invalid",
            )
    elif style_kind == "idecyl_nitrate_year":
        properties = [
            (element.text or "").strip()
            for element in root.iter()
            if element.tag == _q(OGC_NAMESPACE, "PropertyName")
        ]
        if (
            symbolizers != ["PolygonSymbolizer"] * 5
            or len(properties) != 8
            or set(properties) != {recipe["property_name"]}
        ):
            raise LocalStyleAdaptationError(
                "IDECyL nitrate SLD rules are incomplete",
                code="local_style_sld_invalid",
            )
    elif style_kind == "ines_raster":
        entries = [
            element
            for element in root.iter()
            if element.tag == _q(SLD_NAMESPACE, "ColorMapEntry")
        ]
        if (
            symbolizers != ["RasterSymbolizer"]
            or len(entries) != len(recipe["value_class_mapping"])
            or [
                int(element.attrib["quantity"]) for element in entries
            ]
            != [
                item["value"]
                for item in recipe["value_class_mapping"]
            ]
        ):
            raise LocalStyleAdaptationError(
                "INES SLD ColorMap is incomplete",
                code="local_style_sld_invalid",
            )


def _q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
