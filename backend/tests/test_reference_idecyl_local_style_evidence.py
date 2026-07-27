from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.idecyl_local_style_evidence import (
    AUTHORIZATION_EFFECT,
    GENERATOR_VERSION,
    IDECyLLocalStyleEvidenceError,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    PREVIOUS_MANIFEST_RESOURCE,
    PREVIOUS_MANIFEST_SHA256,
    canonical_json_sha256,
    idecyl_local_style_expected_source_definition,
    idecyl_local_style_inventory,
    reviewed_idecyl_local_style_exclusion_for_source,
    reviewed_idecyl_local_styles,
    _load_evidence_package,
    _resource_body,
)
from app.reference_layers.siur_wmc import parse_wmc_evidence


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


def test_inventory_keeps_layer_86_and_adds_two_complete_boundaries() -> None:
    inventory = idecyl_local_style_inventory()

    assert len(inventory) == 13
    by_layer = {
        layer_id: [
            item
            for item in inventory
            if item.audit_layer_id == layer_id
        ]
        for layer_id in {86, 223, 234}
    }
    assert [item.audit_layer_id for item in by_layer[86]] == [86]
    reviewed = by_layer[86][0]
    assert reviewed.profile == (
        "idecyl-cami-cyl-cuadricula-archive-20260727-v3"
    )
    assert reviewed.catalog_style_source_key == (
        "cami_cyl_cuadricula_default"
    )
    assert reviewed.style_title == "Borde celdas negro"
    assert reviewed.is_default is True
    assert reviewed.selected_layer_name == "cuadricula"
    assert reviewed.evidence["dataset_inspection"][
        "data_schema_sha256"
    ] == (
        "0d0bf9bf6263c2e843e978df26d42b6"
        "875ddcdaf7d3d03b231080756b5a0475b"
    )

    expected = {
        223: (
            "idecyl-limites-esp-autonomias-archive-20260727-v3",
            "n_auton",
            "limites_autonomias_negro_etiquetado",
            "6b83f9c9a3bc270281c32b0653f2acd40c6d1c567227d26d220f25ff91ab1555",
        ),
        234: (
            "idecyl-limites-esp-provincias-archive-20260727-v3",
            "n_prov",
            "limites_provincias_negro",
            "fbcb56c94b112bfadeda2933deb92fdbf3d207dffb62ad9fe63ad87f26269aee",
        ),
    }
    for layer_id, (
        profile,
        label_field,
        default_style,
        schema_sha256,
    ) in expected.items():
        styles = by_layer[layer_id]
        assert len(styles) == 6
        assert styles == list(reviewed_idecyl_local_styles(profile))
        assert [item.catalog_style_source_key for item in styles] == sorted(
            item.catalog_style_source_key for item in styles
        )
        assert [
            item.catalog_style_source_key
            for item in styles
            if item.is_default
        ] == [default_style]
        assert {
            item.evidence["parity_kind"] for item in styles
        } == {"adapted"}
        assert {
            item.evidence["exact_style_claim"] for item in styles
        } == {False}
        assert {
            item.evidence["dataset_inspection"][
                "data_schema_sha256"
            ]
            for item in styles
        } == {schema_sha256}
        labelled = [
            item
            for item in styles
            if item.evidence["visual_recipe"]["symbolizer"]
            == "polygon-and-label"
        ]
        assert len(labelled) == 2
        assert {
            item.evidence["visual_recipe"]["label_field"]
            for item in labelled
        } == {label_field}


def test_province_styles_match_versioned_wmc_catalog_evidence() -> None:
    document = (
        Path(__file__).parent / "fixtures" / "siur_context.xml"
    ).read_bytes().rstrip(b"\r\n")
    wmc = parse_wmc_evidence(document)
    province = next(
        layer
        for layer in wmc.layers
        if layer.remote_name == "limites_esp_provincias"
    )
    reviewed = tuple(
        item
        for item in idecyl_local_style_inventory()
        if item.audit_layer_id == 234
    )

    assert {
        (
            style.source_key,
            style.remote_name,
            style.title,
            style.selected,
        )
        for style in province.styles
    } == {
        (
            style.catalog_style_source_key,
            style.remote_style_name,
            style.style_title,
            style.is_default,
        )
        for style in reviewed
    }


def test_source_definitions_bind_every_catalog_style_and_default() -> None:
    inventory = idecyl_local_style_inventory()

    layer_86 = next(item for item in inventory if item.audit_layer_id == 86)
    definition = idecyl_local_style_expected_source_definition(layer_86)
    assert definition["config"]["reviewed_local_style"] == {
        "schema": "siur-reviewed-idecyl-local-style/v1",
        "audit_layer_id": 86,
        "profile": layer_86.profile,
        "recipe_identity_sha256": layer_86.recipe_identity_sha256,
        "catalog_style_source_key": "cami_cyl_cuadricula_default",
        "remote_name": "cami_cyl_cuadricula_default",
        "is_default": True,
    }
    assert "reviewed_local_styles" not in definition["config"]
    assert canonical_json_sha256(definition) == (
        "1d000ad61b43edf1a37d80bf92bf025"
        "3e09efc4c0bb430daa5f814f39fe2c4d0"
    )

    for layer_id in (223, 234):
        item = next(
            item
            for item in inventory
            if item.audit_layer_id == layer_id
        )
        definition = idecyl_local_style_expected_source_definition(item)
        configs = definition["config"]["reviewed_local_styles"]
        assert len(configs) == 6
        assert configs == sorted(
            configs,
            key=lambda value: value["catalog_style_source_key"],
        )
        assert {
            value["catalog_style_source_key"] for value in configs
        } == {
            candidate.catalog_style_source_key
            for candidate in inventory
            if candidate.audit_layer_id == layer_id
        }
        assert sum(value["is_default"] for value in configs) == 1


def test_manifest_is_canonical_hash_bound_non_authorizing_and_excludes_279() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    parsed = json.loads(body)
    previous = _resource_body(PREVIOUS_MANIFEST_RESOURCE)

    assert hashlib.sha256(body).hexdigest() == MANIFEST_SHA256
    assert body == _canonical_bytes(parsed)
    assert hashlib.sha256(previous).hexdigest() == (
        PREVIOUS_MANIFEST_SHA256
    )
    assert parsed["capture"]["generator_version"] == GENERATOR_VERSION
    assert parsed["capture"]["authorization_effect"] == (
        AUTHORIZATION_EFFECT
    )
    assert (
        parsed["capture"]["network_access_during_style_authorship"]
        is False
    )
    assert parsed["capture"]["previous_manifest"] == {
        "resource": PREVIOUS_MANIFEST_RESOURCE,
        "sha256": PREVIOUS_MANIFEST_SHA256,
    }
    assert parsed["excluded_layers"] == [
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
                "catalog_thematic_palette_evidence_"
                "incomplete_for_full_parity"
            ),
            "unresolved_style_source_keys": [
                "lineas_limite_municipales_ambito",
                "lineas_limite_municipales_estadolegal",
                "lineas_limite_municipales_precision",
            ],
        }
    ]


def test_incomplete_style_sources_expose_hash_bound_fallback_evidence() -> None:
    sources = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
    }

    for layer_id in (66, 232, 296):
        exclusion = reviewed_idecyl_local_style_exclusion_for_source(
            sources[layer_id]
        )
        assert exclusion is not None
        assert exclusion["audit_layer_id"] == layer_id
        assert exclusion["complete_vector_style_parity"] is False
        assert exclusion["reason_codes"] == [
            "complete_local_archive_unavailable"
        ]
        assert exclusion["catalog_style_source_keys"] == []
        assert exclusion["evidence_binding"] == {
            "manifest_resource": PREVIOUS_MANIFEST_RESOURCE,
            "manifest_sha256": PREVIOUS_MANIFEST_SHA256,
        }

    boundary = reviewed_idecyl_local_style_exclusion_for_source(
        sources[279]
    )
    assert boundary is not None
    assert boundary["reason_codes"] == [
        "catalog_thematic_palette_evidence_incomplete_for_full_parity"
    ]
    assert boundary["catalog_style_source_keys"] == [
        "lineas_limite_municipales_ambito",
        "lineas_limite_municipales_azul",
        "lineas_limite_municipales_estadolegal",
        "lineas_limite_municipales_precision",
    ]
    assert boundary["unresolved_style_source_keys"] == [
        "lineas_limite_municipales_ambito",
        "lineas_limite_municipales_estadolegal",
        "lineas_limite_municipales_precision",
    ]
    assert boundary["evidence_binding"] == {
        "manifest_resource": MANIFEST_RESOURCE,
        "manifest_sha256": MANIFEST_SHA256,
    }
    assert (
        reviewed_idecyl_local_style_exclusion_for_source(sources[86])
        is None
    )


def _rehash_source(source: dict[str, object]) -> None:
    source_unhashed = {
        key: value
        for key, value in source.items()
        if key not in {"source_evidence_sha256", "styles"}
    }
    source_sha256 = canonical_json_sha256(source_unhashed)
    source["source_evidence_sha256"] = source_sha256
    for style in source["styles"]:
        style_unhashed = {
            key: value
            for key, value in style.items()
            if key != "recipe_identity_sha256"
        }
        style["recipe_identity_sha256"] = canonical_json_sha256(
            {
                "source_evidence_sha256": source_sha256,
                **style_unhashed,
            }
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "catalog_identity",
        "style_name",
        "default",
        "archive_hash",
        "schema",
        "visual",
        "label_field",
    ],
)
def test_rehashed_manifest_rejects_identity_schema_or_recipe_drift(
    mutation: str,
) -> None:
    parsed = json.loads(_resource_body(MANIFEST_RESOURCE))
    source = parsed["sources"][0]
    style = source["styles"][0]
    if mutation == "catalog_identity":
        source["catalog_identity"]["catalog_remote_name"] = "changed"
    elif mutation == "style_name":
        style["catalog_style"]["remote_name"] = "changed"
    elif mutation == "default":
        style["catalog_style"]["is_default"] = True
    elif mutation == "archive_hash":
        source["dataset_inspection"]["archive_snapshot_sha256"] = (
            "0" * 64
        )
    elif mutation == "schema":
        source["dataset_inspection"]["data_schema"][4]["name"] = (
            "changed"
        )
        source["dataset_inspection"]["data_schema_sha256"] = (
            canonical_json_sha256(
                source["dataset_inspection"]["data_schema"]
            )
        )
    elif mutation == "visual":
        style["visual_recipe"]["outline_color"] = "#123456"
    else:
        labelled = next(
            item
            for item in source["styles"]
            if item["visual_recipe"]["symbolizer"]
            == "polygon-and-label"
        )
        labelled["visual_recipe"]["label_field"] = "changed"
        labelled["visual_recipe"]["evidence_basis"][
            "label_field"
        ] = "changed"
    _rehash_source(source)
    body = _canonical_bytes(parsed)

    with pytest.raises(IDECyLLocalStyleEvidenceError):
        _load_evidence_package(
            body,
            previous_body=_resource_body(PREVIOUS_MANIFEST_RESOURCE),
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )
