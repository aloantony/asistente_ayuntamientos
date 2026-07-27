from __future__ import annotations

import hashlib
import json

import pytest

from app.reference_layers.idecyl_local_style_evidence import (
    AUTHORIZATION_EFFECT,
    GENERATOR_VERSION,
    IDECyLLocalStyleEvidenceError,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    canonical_json_sha256,
    idecyl_local_style_expected_source_definition,
    idecyl_local_style_inventory,
    _load_evidence_package,
    _resource_body,
)


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


def test_layer_86_recipe_binds_exact_source_style_and_schema() -> None:
    inventory = idecyl_local_style_inventory()

    assert len(inventory) == 1
    reviewed = inventory[0]
    assert reviewed.audit_layer_id == 86
    assert reviewed.profile == (
        "idecyl-cami-cyl-cuadricula-archive-20260727-v3"
    )
    assert reviewed.catalog_style_source_key == (
        "cami_cyl_cuadricula_default"
    )
    assert reviewed.remote_style_name == "cami_cyl_cuadricula_default"
    assert reviewed.style_title == "Borde celdas negro"
    assert reviewed.is_default is True
    assert reviewed.selected_layer_name == "cuadricula"
    assert reviewed.evidence["parity_kind"] == "adapted"
    assert reviewed.evidence["exact_style_claim"] is False
    assert reviewed.evidence["dataset_inspection"]["geometry_type"] == (
        "MULTIPOLYGON"
    )
    assert reviewed.evidence["dataset_inspection"][
        "data_schema_sha256"
    ] == (
        "0d0bf9bf6263c2e843e978df26d42b6"
        "875ddcdaf7d3d03b231080756b5a0475b"
    )

    definition = idecyl_local_style_expected_source_definition(reviewed)
    assert definition["config"]["reviewed_local_style"] == {
        "schema": "siur-reviewed-idecyl-local-style/v1",
        "audit_layer_id": 86,
        "profile": reviewed.profile,
        "recipe_identity_sha256": reviewed.recipe_identity_sha256,
        "catalog_style_source_key": "cami_cyl_cuadricula_default",
        "remote_name": "cami_cyl_cuadricula_default",
        "is_default": True,
    }
    assert canonical_json_sha256(definition) == (
        "1d000ad61b43edf1a37d80bf92bf025"
        "3e09efc4c0bb430daa5f814f39fe2c4d0"
    )


def test_manifest_is_canonical_hash_bound_and_non_authorizing() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    parsed = json.loads(body)

    assert hashlib.sha256(body).hexdigest() == MANIFEST_SHA256
    assert body == _canonical_bytes(parsed)
    assert parsed["capture"]["generator_version"] == GENERATOR_VERSION
    assert parsed["capture"]["authorization_effect"] == (
        AUTHORIZATION_EFFECT
    )
    assert (
        parsed["capture"]["network_access_during_style_authorship"]
        is False
    )
    assert parsed["excluded_priority_layers"] == [
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


@pytest.mark.parametrize(
    "mutation",
    [
        "catalog_identity",
        "style_name",
        "default",
        "archive_hash",
        "schema",
        "visual",
    ],
)
def test_rehashed_manifest_rejects_identity_or_recipe_drift(
    mutation: str,
) -> None:
    parsed = json.loads(_resource_body(MANIFEST_RESOURCE))
    recipe = parsed["recipes"][0]
    if mutation == "catalog_identity":
        recipe["catalog_identity"]["catalog_remote_name"] = "changed"
    elif mutation == "style_name":
        recipe["catalog_style"]["remote_name"] = "changed"
    elif mutation == "default":
        recipe["catalog_style"]["is_default"] = False
    elif mutation == "archive_hash":
        recipe["dataset_inspection"]["archive_snapshot_sha256"] = (
            "0" * 64
        )
    elif mutation == "schema":
        recipe["dataset_inspection"]["data_schema"][2]["name"] = (
            "changed"
        )
        recipe["dataset_inspection"]["data_schema_sha256"] = (
            canonical_json_sha256(
                recipe["dataset_inspection"]["data_schema"]
            )
        )
    else:
        recipe["visual_recipe"]["outline_color"] = "#ffffff"
    unhashed = dict(recipe)
    unhashed.pop("recipe_identity_sha256")
    recipe["recipe_identity_sha256"] = canonical_json_sha256(unhashed)
    body = _canonical_bytes(parsed)

    with pytest.raises(IDECyLLocalStyleEvidenceError):
        _load_evidence_package(
            body,
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )
