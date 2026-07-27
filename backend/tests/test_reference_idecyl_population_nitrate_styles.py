from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.idecyl_population_nitrate_style_evidence import (
    AUTHORIZATION_EFFECT,
    GENERATOR_VERSION,
    IDECyLPopulationNitrateStyleEvidenceError,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    _load_evidence_package,
    _resource_body,
    canonical_json_sha256,
    idecyl_nitrate_expected_source_definition,
    idecyl_nitrate_style_inventory,
    idecyl_population_style_exclusion,
    idecyl_population_style_exclusion_config,
)
from app.reference_layers.local_style_adaptation import (
    LocalStyleAdaptationError,
    generate_reviewed_local_styles,
    local_style_package_metadata,
    validate_zero_resource_local_adaptation,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
    reviewed_local_style_recipes,
)
from app.reference_layers.style_parity import (
    _evaluate_items,
    _required_styles,
)


SLD = {"sld": "http://www.opengis.net/sld"}
OGC = {"ogc": "http://www.opengis.net/ogc"}


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _source(layer_id: int):
    return next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == layer_id
    )


def _catalog_candidate(layer_id: int):
    source = _source(layer_id)
    service = ReferenceServiceDefinition(
        source_key=f"service:{layer_id}",
        title="IDECyL reviewed archive",
        upstream_protocol="wms",
        base_url=source.catalog_endpoint_url,
        default_format="image/png",
    )
    if layer_id == 268:
        adapted = {
            item.catalog_style_source_key: item
            for item in idecyl_nitrate_style_inventory()
        }
        catalog = source.evidence["archive_style_evidence"][
            "catalog_styles"
        ]
        styles = tuple(
            ReferenceLayerStyleDefinition(
                source_key=item["catalog_style_source_key"],
                title=(
                    adapted[item["catalog_style_source_key"]].style_title
                    if item["catalog_style_source_key"] in adapted
                    else "Recintos municipales 2021 paleta color"
                ),
                remote_name=item["remote_name"],
                is_default=item["is_default"],
            )
            for item in catalog
        )
        default = "coad_cyl_nitrat_aguas_subterr_2021"
    else:
        exclusion = idecyl_population_style_exclusion()
        styles = tuple(
            ReferenceLayerStyleDefinition(
                source_key=item["catalog_style_source_key"],
                title=item["title"],
                remote_name=item["remote_name"],
                is_default=item["is_default"],
            )
            for item in exclusion["catalog_styles"]
        )
        default = "nucleos_cyl_poblaciones_txt_blanco"
    layer = ReferenceLayerDefinition(
        source_key=source.catalog_layer_source_key,
        node_type="layer",
        title="Reviewed IDECyL layer",
        service_key=service.source_key,
        remote_name=source.catalog_remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="mirror",
        bounds={
            "west": -7.6,
            "south": 39.9,
            "east": -1.3,
            "north": 43.4,
        },
        style_name=default,
        styles=styles,
    )
    selected = acquisition_candidates(service, layer)
    assert len(selected) == 1
    return selected[0], service, layer


def _dataset_metadata() -> dict:
    inspection = idecyl_nitrate_style_inventory()[0].evidence[
        "dataset_inspection"
    ]
    return {
        "input_layer": inspection["feature_layer"],
        "geopackage_inspection": {
            "schema_version": inspection["inspection_schema"],
            "archive_member": inspection["archive_member"],
            "feature_layer": inspection["feature_layer"],
            "feature_layers": inspection["feature_layers"],
            "geometry_column": inspection["geometry_column"],
            "geometry_type": inspection["geometry_type"],
            "crs": inspection["srs"],
            "data_schema": deepcopy(inspection["data_schema"]),
            "data_schema_sha256": inspection["data_schema_sha256"],
        },
    }


def test_manifest_is_canonical_hash_bound_and_non_authorizing() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    parsed = json.loads(body)

    assert hashlib.sha256(body).hexdigest() == MANIFEST_SHA256
    assert body == _canonical_bytes(parsed)
    assert parsed["capture"]["generator_version"] == GENERATOR_VERSION
    assert parsed["capture"]["authorization_effect"] == (
        AUTHORIZATION_EFFECT
    )
    assert parsed["capture"]["network_access_during_style_authorship"] is False
    assert len(idecyl_nitrate_style_inventory()) == 15


def test_population_remains_an_explicit_hash_bound_exclusion() -> None:
    exclusion = idecyl_population_style_exclusion()

    assert exclusion["audit_layer_id"] == 237
    assert exclusion["local_service_eligible"] is False
    assert len(exclusion["catalog_styles"]) == 6
    assert exclusion["embedded_sld"]["sld_style_name"] == (
        "nucleos_cyl_poblaciones"
    )
    assert {
        item["catalog_style_source_key"]
        for item in exclusion["catalog_styles"]
    }.isdisjoint({exclusion["embedded_sld"]["sld_style_name"]})
    assert exclusion["reason_codes"][-1] == (
        "complete_catalog_style_coverage_unproven"
    )

    candidate, _, _ = _catalog_candidate(237)
    assert candidate.protocol == "wms_tiles"
    assert candidate.target_kind == "tiles"
    assert candidate.sync_strategy == "tile_seed"
    assert "archive_styles" not in candidate.config
    assert "reviewed_local_style" not in candidate.config
    assert "reviewed_local_styles" not in candidate.config
    assert "reviewed_local_style_exclusion" not in candidate.config
    fallback = candidate.config["reviewed_baked_wms_fallback"]
    assert fallback["schema"] == (
        "siur-reviewed-idecyl-baked-wms-fallback/v1"
    )
    assert fallback["audit_layer_id"] == 237
    assert fallback["profile"] == (
        "idecyl-nucleos-cyl-poblaciones-archive-20260727-v3"
    )
    assert fallback["complete_vector_style_parity"] is False
    assert fallback["catalog_styles"] == [
        {
            "catalog_style_source_key": item[
                "catalog_style_source_key"
            ],
            "remote_name": item["remote_name"],
            "is_default": item["is_default"],
        }
        for item in exclusion["catalog_styles"]
    ]
    assert fallback["exclusion_evidence"] == {
        "kind": "population_style_exclusion",
        "manifest_resource": MANIFEST_RESOURCE,
        "manifest_sha256": MANIFEST_SHA256,
        "evidence_identity_sha256": (
            "2468c24e3589efc6a755c7d15202a078"
            "602a412fe6f89dba37716e8fec903466"
        ),
    }
    assert fallback["reason_codes"] == exclusion["reason_codes"]
    assert reviewed_local_style_recipes(candidate) == ()


def test_population_vector_exclusion_config_remains_hash_bound() -> None:
    exclusion = idecyl_population_style_exclusion()

    assert idecyl_population_style_exclusion_config(exclusion) == {
        "schema": "siur-reviewed-idecyl-local-style-exclusion/v1",
        "audit_layer_id": 237,
        "profile": (
            "idecyl-nucleos-cyl-poblaciones-archive-20260727-v3"
        ),
        "exclusion_identity_sha256": (
            "2468c24e3589efc6a755c7d15202a078"
            "602a412fe6f89dba37716e8fec903466"
        ),
        "local_service_eligible": False,
        "catalog_style_count": 6,
        "reason_codes": exclusion["reason_codes"],
    }


def test_nitrate_candidate_combines_one_exact_and_fifteen_adapted_styles() -> None:
    candidate, _, _ = _catalog_candidate(268)

    assert len(candidate.config["archive_styles"]) == 1
    assert candidate.config["archive_styles"][0][
        "catalog_style_source_key"
    ] == "coad_cyl_nitrat_aguas_subterr_2021"
    assert candidate.config["archive_styles"][0]["is_default"] is True
    assert len(candidate.config["reviewed_local_styles"]) == 15
    assert len(reviewed_local_style_recipes(candidate)) == 15
    assert "archive_style_archive_sha256" not in candidate.config
    combined = {
        item["catalog_style_source_key"]
        for item in candidate.config["archive_styles"]
    } | {
        item["catalog_style_source_key"]
        for item in candidate.config["reviewed_local_styles"]
    }
    assert combined == {
        f"coad_cyl_nitrat_aguas_subterr_{year}"
        for year in range(2006, 2022)
    }
    assert {
        "style_endpoint_url",
        "style_layer_name",
        "styles",
    }.isdisjoint(candidate.config)


def test_nitrate_authorship_changes_only_the_hash_bound_year_field() -> None:
    candidate, _, _ = _catalog_candidate(268)
    first = generate_reviewed_local_styles(
        candidate,
        dataset_metadata=_dataset_metadata(),
    )
    second = generate_reviewed_local_styles(
        candidate,
        dataset_metadata=_dataset_metadata(),
    )

    assert first == second
    assert len(first) == 15
    expected_palette = [
        "#cccccc",
        "#08bd08",
        "#ffff00",
        "#ff9900",
        "#ff0000",
    ]
    for year, authored in zip(range(2006, 2021), first, strict=True):
        root = ElementTree.fromstring(authored.document)
        properties = {
            (element.text or "").strip()
            for element in root.findall(".//ogc:PropertyName", OGC)
        }
        fills = [
            (element.text or "").strip()
            for element in root.findall(".//sld:CssParameter", SLD)
            if element.attrib.get("name") == "fill"
        ]
        evidence = authored.metadata["authored_local_evidence"]
        assert properties == {f"v_nitr{year}"}
        assert fills == expected_palette
        assert len(root.findall(".//sld:PolygonSymbolizer", SLD)) == 5
        assert authored.metadata["catalog_style_source_key"].endswith(
            str(year)
        )
        assert authored.metadata["parity_kind"] == "adapted"
        assert evidence["exact_style_claim"] is False
        assert evidence["catalog_style_is_default"] is False
        assert evidence["recipe"]["template_sld_sha256"] == (
            "ed4dff6410973ffd0e672a1a4216a7d"
            "11dab2118bc3b0a6056b2d1cfc2b87c38"
        )
        assert evidence["recipe"]["semantic_adaptation"] == {
            "schema": (
                "siur-idecyl-temporal-rule-semantic-adaptation/v1"
            ),
            "source_property_name": "v_nitr2021",
            "target_property_name": f"v_nitr{year}",
            "classification_rule_semantics_preserved": True,
            "property_name_is_only_rule_semantic_change": True,
            "runtime_sld_version": "1.0.0",
            "catalog_style_identity_applied": True,
            "source_descriptions_omitted": True,
            "year": year,
        }
        validate_zero_resource_local_adaptation(
            style_metadata=authored.metadata,
            package_metadata=local_style_package_metadata(authored),
            sld_sha256=authored.sld_sha256,
        )


def test_nitrate_mixed_exact_and_adapted_style_parity_is_complete() -> None:
    candidate, _, layer = _catalog_candidate(268)
    authored_styles = generate_reviewed_local_styles(
        candidate,
        dataset_metadata=_dataset_metadata(),
    )
    required = _required_styles(
        [
            SimpleNamespace(
                id=index,
                source_key=style.source_key,
                remote_name=style.remote_name,
                is_default=style.is_default,
                status=style.status,
            )
            for index, style in enumerate(layer.styles, start=1)
        ]
    )
    exact = candidate.config["archive_styles"][0]
    artifacts = [
        SimpleNamespace(
            artifact_id=1,
            artifact_kind="style",
            roles=frozenset({"style"}),
            sha256=exact["sha256"],
            metadata_json={
                "catalog_style_source_key": exact[
                    "catalog_style_source_key"
                ],
                "parity_kind": "exact",
                "resource_bindings": [],
                "unresolved_resources": [],
            },
        )
    ]
    for index, authored in enumerate(authored_styles, start=2):
        artifacts.extend(
            [
                SimpleNamespace(
                    artifact_id=index,
                    artifact_kind="style",
                    roles=frozenset({"style"}),
                    sha256=authored.sld_sha256,
                    metadata_json=authored.metadata,
                ),
                SimpleNamespace(
                    artifact_id=index + 100,
                    artifact_kind="style_package",
                    roles=frozenset({"style_package"}),
                    sha256="f" * 64,
                    metadata_json=local_style_package_metadata(authored),
                ),
            ]
        )

    items = _evaluate_items(
        required,
        delivery_kind="vector",
        artifacts=artifacts,
        probe=None,
    )

    assert len(items) == 16
    assert all(item.verified for item in items)
    assert sum(item.parity_kind == "exact" for item in items) == 1
    assert sum(item.parity_kind == "adapted" for item in items) == 15


@pytest.mark.parametrize("mutation", ["missing_field", "schema", "crs"])
def test_nitrate_rejects_missing_or_changed_dataset_schema(
    mutation: str,
) -> None:
    metadata = _dataset_metadata()
    if mutation == "missing_field":
        metadata["geopackage_inspection"]["data_schema"].pop(8)
    elif mutation == "schema":
        metadata["geopackage_inspection"]["data_schema"][8][
            "declared_type"
        ] = "TEXT"
    else:
        metadata["geopackage_inspection"]["crs"] = "EPSG:4326"
    metadata["geopackage_inspection"]["data_schema_sha256"] = (
        canonical_json_sha256(
            metadata["geopackage_inspection"]["data_schema"]
        )
    )

    with pytest.raises(LocalStyleAdaptationError) as captured:
        generate_reviewed_local_styles(
            _catalog_candidate(268)[0],
            dataset_metadata=metadata,
        )

    assert captured.value.code == "local_style_dataset_schema_changed"


@pytest.mark.parametrize(
    "mutation",
    ["catalog_identity", "default", "source", "template", "schema", "exclusion"],
)
def test_rehashed_manifest_rejects_evidence_drift(mutation: str) -> None:
    parsed = json.loads(_resource_body(MANIFEST_RESOURCE))
    if mutation == "catalog_identity":
        parsed["nitrate_series"]["catalog_identity"][
            "catalog_remote_name"
        ] = "changed"
    elif mutation == "default":
        parsed["nitrate_series"]["adapted_catalog_styles"][0][
            "is_default"
        ] = True
    elif mutation == "source":
        parsed["nitrate_series"]["source_binding"][
            "selected_endpoint_url"
        ] = "https://opendata.jcyl.es/changed.zip"
    elif mutation == "template":
        parsed["nitrate_series"]["style_template"][
            "archive_member_sha256"
        ] = "0" * 64
    elif mutation == "schema":
        parsed["nitrate_series"]["dataset_inspection"][
            "data_schema_sha256"
        ] = "0" * 64
    else:
        parsed["population_exclusion"]["local_service_eligible"] = True
    if mutation == "exclusion":
        item = parsed["population_exclusion"]
        unhashed = dict(item)
        unhashed.pop("exclusion_identity_sha256")
        item["exclusion_identity_sha256"] = canonical_json_sha256(
            unhashed
        )
    else:
        item = parsed["nitrate_series"]
        unhashed = dict(item)
        unhashed.pop("series_identity_sha256")
        item["series_identity_sha256"] = canonical_json_sha256(unhashed)
    body = _canonical_bytes(parsed)

    with pytest.raises(IDECyLPopulationNitrateStyleEvidenceError):
        _load_evidence_package(
            body,
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )


def test_nitrate_rejects_catalog_default_or_candidate_tampering() -> None:
    candidate, service, layer = _catalog_candidate(268)
    styles = list(layer.styles)
    default_index = next(
        index for index, item in enumerate(styles) if item.is_default
    )
    styles[default_index] = replace(styles[default_index], is_default=False)
    changed_layer = replace(layer, styles=tuple(styles))

    with pytest.raises(SourceDiscoveryError) as captured:
        acquisition_candidates(service, changed_layer)
    assert captured.value.code == (
        "reviewed_idecyl_local_style_identity_invalid"
    )

    title_index = next(
        index
        for index, item in enumerate(layer.styles)
        if not item.is_default
    )
    title_styles = list(layer.styles)
    title_styles[title_index] = replace(
        title_styles[title_index],
        title="Changed title",
    )
    with pytest.raises(SourceDiscoveryError) as title_error:
        acquisition_candidates(
            service,
            replace(layer, styles=tuple(title_styles)),
        )
    assert title_error.value.code == (
        "reviewed_idecyl_local_style_identity_invalid"
    )

    tampered = replace(
        candidate,
        config={
            **candidate.config,
            "reviewed_local_styles": candidate.config[
                "reviewed_local_styles"
            ][1:],
        },
    )
    with pytest.raises(SourceDiscoveryError) as source_error:
        reviewed_local_style_recipes(tampered)
    assert source_error.value.code == "reviewed_local_style_invalid"


def test_population_exclusion_rejects_catalog_tampering() -> None:
    _candidate, service, layer = _catalog_candidate(237)
    styles = list(layer.styles)
    styles[0] = replace(styles[0], title="Changed title")

    with pytest.raises(SourceDiscoveryError) as catalog_error:
        acquisition_candidates(
            service,
            replace(layer, styles=tuple(styles)),
        )
    assert catalog_error.value.code == (
        "reviewed_idecyl_local_style_identity_invalid"
    )


def test_all_nitrate_styles_share_one_full_source_definition() -> None:
    inventory = idecyl_nitrate_style_inventory()
    definitions = [
        idecyl_nitrate_expected_source_definition(item)
        for item in inventory
    ]

    assert all(item == definitions[0] for item in definitions)
    assert len(
        {
            item.recipe_identity_sha256
            for item in inventory
        }
    ) == 15
