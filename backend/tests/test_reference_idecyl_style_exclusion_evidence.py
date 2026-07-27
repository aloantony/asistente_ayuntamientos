from __future__ import annotations

import hashlib
import json

import pytest

from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.idecyl_style_exclusion_evidence import (
    AUTHORIZATION_EFFECT,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    IDECyLStyleExclusionEvidenceError,
    _load_evidence_package,
    _resource_body,
    canonical_json_sha256,
    idecyl_style_exclusion_inventory,
    reviewed_idecyl_style_exclusion,
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


def test_exclusions_bind_exact_sources_and_complete_catalog_style_sets() -> None:
    exclusions = {
        item.audit_layer_id: item for item in idecyl_style_exclusion_inventory()
    }

    assert tuple(exclusions) == (65, 105)
    eclipse = exclusions[65]
    assert eclipse.reason_codes == ("qml_external_raster_marker_missing",)
    assert len(eclipse.required_catalog_styles) == 1
    assert (
        eclipse.required_catalog_styles[0]["catalog_style_source_key"]
        == "eclipse_2026_puntos_recomendados_ico"
    )
    style_artifact = eclipse.evidence["local_artifact_observation"]["style_artifact"]
    assert style_artifact["renderer"] == "RasterMarker"
    assert style_artifact["external_resource_path"] == (
        "D:/Temp/eclipse/pcivil/png/observa_ico.png"
    )
    assert style_artifact["external_resource_archive_member_matches"] == 0
    assert style_artifact["self_contained"] is False

    hydro = exclusions[105]
    assert hydro.reason_codes == (
        "complete_local_archive_unavailable",
        "classified_style_categories_and_colors_unavailable",
    )
    assert [
        item["catalog_style_source_key"] for item in hydro.required_catalog_styles
    ] == ["hidro_cyl_cursos_clasif", "hidro_cyl_cursos_simple"]
    assert (
        hydro.evidence["local_artifact_observation"][
            "complete_archive_available_locally"
        ]
        is False
    )

    exact = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id in exclusions
    }
    for layer_id, exclusion in exclusions.items():
        source = exact[layer_id]
        binding = exclusion.evidence["source_binding"]
        assert source.profile == exclusion.profile == binding["profile"]
        assert binding["candidate_config_sha256"] == (
            canonical_json_sha256(source.candidate_config)
        )
        assert binding["exact_source_evidence_sha256"] == (
            canonical_json_sha256(source.evidence)
        )
        assert source.candidate_config is not None
        assert "reviewed_local_style" not in source.candidate_config
        assert "archive_styles" not in source.candidate_config


def test_manifest_is_canonical_hash_bound_and_non_authorizing() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    manifest = json.loads(body)

    assert hashlib.sha256(body).hexdigest() == MANIFEST_SHA256
    assert body == _canonical_bytes(manifest)
    assert manifest["capture"]["authorization_effect"] == (AUTHORIZATION_EFFECT)
    assert manifest["capture"]["network_access_during_authorship"] is False
    assert all(
        layer["outcome"]
        == {
            "authorization_effect": AUTHORIZATION_EFFECT,
            "complete_style_parity": False,
            "reason_codes": list(
                {
                    65: ("qml_external_raster_marker_missing",),
                    105: (
                        "complete_local_archive_unavailable",
                        "classified_style_categories_and_colors_unavailable",
                    ),
                }[layer["audit_layer_id"]]
            ),
            "status": "excluded",
        }
        for layer in manifest["layers"]
    )


def test_eclipse_geometry_is_valid_but_missing_marker_remains_fail_closed() -> None:
    [eclipse] = [
        item for item in idecyl_style_exclusion_inventory() if item.audit_layer_id == 65
    ]
    dataset = eclipse.evidence["local_artifact_observation"]["dataset_inspection"]

    assert dataset["integrity_check"] == "ok"
    assert dataset["feature_count"] == 75
    assert dataset["empty_geometry_count"] == 0
    assert dataset["geometry_type"] == "POINT"
    assert dataset["declared_bounds"] == dataset["geometry_bounds"]
    assert eclipse.evidence["outcome"]["complete_style_parity"] is False


def test_manifest_byte_tampering_fails_pinned_digest() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    tampered = body.replace(
        b'"renderer": "RasterMarker"',
        b'"renderer": "SimpleMarker"',
        1,
    )
    assert tampered != body

    with pytest.raises(
        IDECyLStyleExclusionEvidenceError,
        match="failed its local digest",
    ):
        _load_evidence_package(
            tampered,
            expected_sha256=MANIFEST_SHA256,
        )


def test_rehashed_evidence_cannot_remove_exclusion() -> None:
    manifest = json.loads(_resource_body(MANIFEST_RESOURCE))
    eclipse = manifest["layers"][0]
    eclipse["outcome"]["complete_style_parity"] = True
    unhashed = dict(eclipse)
    unhashed.pop("evidence_identity_sha256")
    eclipse["evidence_identity_sha256"] = canonical_json_sha256(unhashed)
    body = _canonical_bytes(manifest)

    with pytest.raises(
        IDECyLStyleExclusionEvidenceError,
        match="layer identity changed",
    ):
        _load_evidence_package(
            body,
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )


def test_inventory_returns_fresh_projections_and_exact_lookup() -> None:
    first = idecyl_style_exclusion_inventory()
    first[0].evidence["outcome"]["status"] = "changed"

    second = idecyl_style_exclusion_inventory()
    assert second[0].evidence["outcome"]["status"] == "excluded"
    resolved = reviewed_idecyl_style_exclusion(second[0].profile)
    assert resolved is not None
    assert resolved.audit_layer_id == 65
    assert reviewed_idecyl_style_exclusion("missing") is None
