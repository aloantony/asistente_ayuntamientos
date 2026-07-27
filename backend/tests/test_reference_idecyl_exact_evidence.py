from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from importlib.resources import files
import json
from typing import Any

import pytest

from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.idecyl_exact_evidence import (
    AUDIT_SHA256,
    EVIDENCE_SCHEMA,
    IDECyLExactEvidenceError,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    RECORDS_RESOURCE,
    RECORDS_SHA256,
    SOURCE_SNAPSHOT_SHA256,
    _load_evidence_package,
    idecyl_exact_source_inventory,
    reviewed_idecyl_exact_source,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    acquisition_candidates,
    candidate_definition,
)


EXPECTED_REMOTE_NAMES = {
    "telefonia_movil_cyl_cobertura_carreteras",
    "eclipse_2026_puntos_recomendados",
    "eclipse_2026_zonas_no_recomendadas",
    "znie_cyl_vvpp_ejes",
    "carr_cyl_red_vias",
    "cami_cyl_cuadricula",
    "ot_cyl_instrumentos_ambito",
    "hidro_cyl_cursos",
    "pesca_cyl_cangrejo_v",
    "spac_recinto_24",
    "en_cyl_rednatura2000_zec_vw",
    "montes_cyl_propiedad_cyl_vw",
    "ot_cyl_mancomunidades_interes_general",
    "energia_cyl_renov_zonas_sens_amb_flora",
    "spac_recinto_22",
    "montes_cyl_contratados_jcyl_vw",
    "energia_cyl_renov_zonas_sens_actv_crit",
    "plau_cyl_sectores_desclasif",
    "en_cyl_znie_zhie_vw",
    "limites_esp_autonomias",
    "especies_prot_cyl_areas_criticas_vw",
    "en_cyl_znie_zhie_zpp_vw",
    "vegetacion_cyl_rednatura2000_vw",
    "limites_esp_provincias",
    "nucleos_cyl_poblaciones",
    "habitantes_cyl_2024",
    "limites_cyl_entidad_local_menor",
    "coad_cyl_nitrat_aguas_subterr",
    "lineas_limite_cyl_municipales",
    "habitantes_cyl_2015",
    "gesfor_cyl_rodal",
}


def _resource_body(resource_path: str) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    return resource.read_bytes()


def _service(
    endpoint_url: str,
) -> ReferenceServiceDefinition:
    return ReferenceServiceDefinition(
        source_key="service",
        title="IDECyL",
        upstream_protocol="wms",
        base_url=endpoint_url,
        default_format="image/png",
    )


def _layer(
    source_key: str,
    remote_name: str,
) -> ReferenceLayerDefinition:
    return ReferenceLayerDefinition(
        source_key=source_key,
        node_type="layer",
        title=remote_name,
        service_key="service",
        remote_name=remote_name,
        role="overlay",
        renderer="raster_tile",
        delivery_mode="proxy",
        bounds={
            "west": -7.1,
            "south": 40.0,
            "east": -1.7,
            "north": 43.3,
        },
        min_zoom=6,
        max_zoom=18,
        style_name="default",
    )


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _walk_keys(value: Any) -> set[str]:
    result: set[str] = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            result.update(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return result


def test_committed_evidence_covers_exactly_the_31_audited_layers() -> None:
    inventory = idecyl_exact_source_inventory()

    assert len(inventory) == 31
    assert {item.catalog_remote_name for item in inventory} == (
        EXPECTED_REMOTE_NAMES
    )
    assert len(
        {
            item.evidence["official_metadata"]["fid"].casefold()
            for item in inventory
        }
    ) == 30
    assert len(
        {item.catalog_layer_source_key for item in inventory}
    ) == 31
    assert all(item.protocol == "wfs" for item in inventory)
    assert all(item.target_kind == "vector" for item in inventory)
    assert all(
        item.evidence["schema"] == EVIDENCE_SCHEMA
        for item in inventory
    )
    assert all(
        item.evidence["official_metadata"][
            "source_snapshot_sha256"
        ]
        == SOURCE_SNAPSHOT_SHA256
        for item in inventory
    )
    assert all(
        item.evidence["official_metadata"]["local_bundle_sha256"]
        == RECORDS_SHA256
        for item in inventory
    )


def test_every_allowlisted_identity_builds_one_hash_bound_wfs_source() -> None:
    for reviewed in idecyl_exact_source_inventory():
        candidates = acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            _layer(
                reviewed.catalog_layer_source_key,
                reviewed.catalog_remote_name,
            ),
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.protocol == "wfs"
        assert candidate.target_kind == "vector"
        assert candidate.endpoint_url == reviewed.endpoint_url
        assert candidate.remote_name == reviewed.remote_name
        assert candidate.config["discovery"] == "wfs_capabilities"
        assert candidate.config["reviewed_equivalence"] == (
            reviewed.evidence
        )
        assert candidate.definition_sha256 == _canonical_sha256(
            candidate_definition(candidate)
        )


def test_exact_metadata_changes_the_existing_source_definition_hash() -> None:
    reviewed = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.catalog_remote_name
        == "telefonia_movil_cyl_cobertura_carreteras"
    )
    candidate = acquisition_candidates(
        _service(reviewed.catalog_endpoint_url),
        _layer(
            reviewed.catalog_layer_source_key,
            reviewed.catalog_remote_name,
        ),
    )[0]

    assert candidate.definition_sha256 != (
        "65962f1deea8b939840448f17e3ce418"
        "46c3af76aeb1be38dc06cfd4b1bab8d9"
    )
    changed = candidate_definition(candidate)
    changed = deepcopy(changed)
    changed["config"]["reviewed_equivalence"][
        "official_metadata"
    ]["record_raw_sha256"] = "0" * 64
    assert _canonical_sha256(changed) != candidate.definition_sha256


def test_similar_or_changed_layers_never_receive_reviewed_evidence() -> None:
    reviewed = idecyl_exact_source_inventory()[0]
    wrong_source_key = "layer:siur:" + "0" * 64
    assert wrong_source_key != reviewed.catalog_layer_source_key

    candidates = acquisition_candidates(
        _service(reviewed.catalog_endpoint_url),
        _layer(wrong_source_key, reviewed.catalog_remote_name),
    )

    assert [item.protocol for item in candidates] == [
        "wfs",
        "wcs",
        "wms_tiles",
    ]
    assert all(
        "reviewed_equivalence" not in item.config
        for item in candidates
    )
    assert (
        reviewed_idecyl_exact_source(
            catalog_layer_source_key=wrong_source_key,
            catalog_endpoint_url=reviewed.catalog_endpoint_url,
            catalog_remote_name=reviewed.catalog_remote_name,
        )
        is None
    )
    assert (
        reviewed_idecyl_exact_source(
            catalog_layer_source_key=(
                reviewed.catalog_layer_source_key
            ),
            catalog_endpoint_url=reviewed.catalog_endpoint_url,
            catalog_remote_name=reviewed.catalog_remote_name + "_copy",
        )
        is None
    )


def test_matching_allowlist_identity_with_changed_role_fails_closed() -> None:
    reviewed = idecyl_exact_source_inventory()[0]
    changed_layer = replace(
        _layer(
            reviewed.catalog_layer_source_key,
            reviewed.catalog_remote_name,
        ),
        role="base",
    )

    with pytest.raises(SourceDiscoveryError) as captured:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            changed_layer,
        )

    assert captured.value.code == "reviewed_idecyl_identity_invalid"


@pytest.mark.parametrize("resource_path", [MANIFEST_RESOURCE, RECORDS_RESOURCE])
def test_committed_evidence_bytes_are_exact(resource_path: str) -> None:
    expected = {
        MANIFEST_RESOURCE: MANIFEST_SHA256,
        RECORDS_RESOURCE: RECORDS_SHA256,
    }[resource_path]

    assert hashlib.sha256(_resource_body(resource_path)).hexdigest() == (
        expected
    )


def test_tampered_bundle_is_rejected_before_it_can_build_a_source() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    records = _resource_body(RECORDS_RESOURCE)
    tampered = records.replace(
        b"Junta de Castilla y Le",
        b"Xunta de Castilla y Le",
        1,
    )
    assert tampered != records

    with pytest.raises(
        IDECyLExactEvidenceError,
        match="bundle failed its local digest",
    ):
        _load_evidence_package(
            manifest,
            tampered,
            expected_manifest_sha256=MANIFEST_SHA256,
        )


def test_tampered_manifest_is_rejected_before_xml_is_parsed() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    records = _resource_body(RECORDS_RESOURCE)
    tampered = manifest.replace(
        AUDIT_SHA256.encode("ascii"),
        ("0" * 64).encode("ascii"),
        1,
    )
    assert tampered != manifest

    with pytest.raises(
        IDECyLExactEvidenceError,
        match="manifest failed its local digest",
    ):
        _load_evidence_package(
            tampered,
            records,
            expected_manifest_sha256=MANIFEST_SHA256,
        )


def test_metadata_preserves_all_official_attribution_statements() -> None:
    inventory = {
        item.catalog_remote_name: item
        for item in idecyl_exact_source_inventory()
    }
    hydro = inventory["hidro_cyl_cursos"].evidence[
        "official_metadata"
    ]
    provinces = inventory["limites_esp_provincias"].evidence[
        "official_metadata"
    ]

    assert hydro["attribution_labels"] == [
        "Instituto Geográfico Nacional / BDLJE"
    ]
    assert hydro["terms_urls"] == [
        "http://www.ign.es/resources/licencia/"
        "Condiciones_licenciaUso_IGN.pdf"
    ]
    assert provinces["attribution_labels"] == [
        "Junta de Castilla y León",
        "Instituto Geográfico Nacional / BDLJE",
    ]
    assert len(provinces["attribution_statements"]) == 2


def test_evidence_does_not_fabricate_a_human_authorization() -> None:
    forbidden = {
        "reviewer",
        "reviewed_at",
        "decision",
        "allow_download",
        "allow_cache",
        "allow_redistribution",
    }
    inventory = idecyl_exact_source_inventory()

    assert all(
        item.evidence["authorization_effect"]
        == "none_without_persisted_human_mirror_review"
        for item in inventory
    )
    assert all(
        not (_walk_keys(item.evidence) & forbidden)
        for item in inventory
    )
    unlinked = [
        item.catalog_remote_name
        for item in inventory
        if not item.evidence["audit_proposed_distribution"][
            "metadata_link_present"
        ]
    ]
    assert unlinked == ["limites_cyl_entidad_local_menor"]
