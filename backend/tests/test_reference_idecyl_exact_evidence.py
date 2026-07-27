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
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.idecyl_exact_evidence import (
    EVIDENCE_SCHEMA,
    IDECyLExactEvidenceError,
    LEGACY_MANIFEST_RESOURCE,
    LEGACY_MANIFEST_SHA256,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    PREVIOUS_MANIFEST_RESOURCE,
    PREVIOUS_MANIFEST_SHA256,
    RECORDS_RESOURCE,
    RECORDS_SHA256,
    _load_evidence_package,
    idecyl_exact_source_inventory,
    reviewed_idecyl_exact_source,
)
from app.reference_layers.source_content_parity import (
    configured_parity_spec,
)
from app.reference_layers.reviewed_archive_integrity import (
    configured_reviewed_archive_integrity,
)
from app.reference_layers.source_discovery import (
    SourceDiscoveryError,
    _idecyl_archive_style_config,
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
TELECOM_STYLES = (
    "telefonia_movil_cyl_cobertura_carreteras_tc_cnmc_mj",
    "telefonia_movil_cyl_cobertura_carreteras_4g_cnmc",
    "telefonia_movil_cyl_cobertura_carreteras_5g_cnmc_mj",
)
REVIEWABLE_ARCHIVE_IDS = {
    65,
    66,
    86,
    98,
    105,
    137,
    151,
    197,
    213,
    223,
    225,
    230,
    232,
    234,
    237,
    268,
    279,
    296,
}


def _resource_body(resource_path: str) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    return resource.read_bytes()


def _service(endpoint_url: str) -> ReferenceServiceDefinition:
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
    *,
    with_telecom_styles: bool = False,
) -> ReferenceLayerDefinition:
    styles = (
        tuple(
            ReferenceLayerStyleDefinition(
                source_key=name.casefold(),
                title=name,
                remote_name=name,
                sort_order=index,
                is_default=index == 0,
            )
            for index, name in enumerate(TELECOM_STYLES)
        )
        if with_telecom_styles
        else ()
    )
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
        style_name=TELECOM_STYLES[0],
        styles=styles,
    )


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _walk_strings(value: Any) -> list[str]:
    result: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            result.append(current)
    return result


def test_classification_covers_all_31_layers_fail_closed() -> None:
    inventory = idecyl_exact_source_inventory()

    assert len(inventory) == 31
    assert {item.catalog_remote_name for item in inventory} == (
        EXPECTED_REMOTE_NAMES
    )
    assert sum(
        item.local_service_status == "candidate"
        for item in inventory
    ) == 19
    assert sum(
        item.local_service_status == "restricted"
        for item in inventory
    ) == 12
    assert sum(
        item.local_service_status == "permission_pending"
        for item in inventory
    ) == 0
    assert {
        item.audit_layer_id
        for item in inventory
        if item.local_service_status == "candidate"
    } == {39, *REVIEWABLE_ARCHIVE_IDS}
    assert all(item.evidence["schema"] == EVIDENCE_SCHEMA for item in inventory)


def test_sigpac_uses_exact_https_directories_but_remains_license_restricted() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
    }

    for layer_id, year in ((123, "2024"), (166, "2022")):
        reviewed = inventory[layer_id]
        distribution = reviewed.evidence["official_https_distribution"]

        assert reviewed.local_service_status == "restricted"
        assert reviewed.protocol is None
        assert reviewed.endpoint_url is None
        assert reviewed.reason_codes == (
            "https_directory_igcyl_nc_requires_recipient_acceptance",
            "local_service_requires_persisted_human_review",
        )
        assert distribution["root_directory_url"] == (
            "https://ftp.itacyl.es/cartografia/05_SIGPAC/"
            f"{year}_ETRS89/"
        )
        assert distribution["province_directory_url"] == (
            distribution["root_directory_url"]
            + "Parcelario_SIGPAC_CyL_Provincias/"
        )
        assert distribution["archive_names"] == [
            "AVILA.zip",
            "BURGOS.zip",
            "LEON.zip",
            "PALENCIA.zip",
            "SALAMANCA.zip",
            "SEGOVIA.zip",
            "SORIA.zip",
            "VALLADOLID.zip",
            "ZAMORA.zip",
        ]
        assert distribution["license_name"] == "LICENCIA-IGCYL-NC"
        assert distribution["license_url"].startswith("https://")
        assert distribution["authorization_effect"] == (
            "none_without_persisted_human_mirror_review"
        )


def test_wms_metadata_bindings_preserve_exact_mismatch_and_missing() -> None:
    inventory = idecyl_exact_source_inventory()
    by_status: dict[str, set[int]] = {}
    for item in inventory:
        by_status.setdefault(
            item.evidence["metadata_binding"]["status"],
            set(),
        ).add(item.audit_layer_id)

    assert len(by_status["exact"]) == 22
    assert by_status["mismatch"] == {137, 142, 171, 213, 225, 230, 268}
    assert by_status["missing"] == {65, 66}


def test_layer_39_keeps_its_exact_https_geopackage_candidate() -> None:
    reviewed = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == 39
    )

    candidates = acquisition_candidates(
        _service(reviewed.catalog_endpoint_url),
        _layer(
            reviewed.catalog_layer_source_key,
            reviewed.catalog_remote_name,
            with_telecom_styles=True,
        ),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.protocol == "download"
    assert candidate.target_kind == "vector"
    assert candidate.sync_strategy == "conditional_get"
    assert candidate.endpoint_url.startswith("https://opendata.jcyl.es/")
    assert candidate.config["data_format"] == "geopackage-zip"
    assert candidate.config["media_type"] == "application/x-zip-compressed"
    assert candidate.config["archive_member"].endswith(".gpkg")
    assert candidate.config["input_layer"] == reviewed.catalog_remote_name
    assert len(candidate.config["archive_styles"]) == 3
    assert {
        item["remote_name"] for item in candidate.config["archive_styles"]
    } == set(TELECOM_STYLES)
    assert all(
        len(item["sha256"]) == 64
        for item in candidate.config["archive_styles"]
    )
    assert all(
        set(item)
        == {
            "catalog_style_source_key",
            "remote_name",
            "archive_member",
            "sha256",
        }
        for item in candidate.config["archive_styles"]
    )
    assert candidate.config["reviewed_equivalence"] == reviewed.evidence
    assert candidate.definition_sha256 == _canonical_sha256(
        candidate_definition(candidate)
    )


def test_all_18_reviewable_archives_build_hash_bound_candidates() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
    }
    definitions: set[str] = set()

    for layer_id in sorted(REVIEWABLE_ARCHIVE_IDS):
        reviewed = inventory[layer_id]
        candidates = acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            _layer(
                reviewed.catalog_layer_source_key,
                reviewed.catalog_remote_name,
            ),
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.protocol == "download"
        assert candidate.target_kind == "vector"
        assert candidate.sync_strategy == "conditional_get"
        assert candidate.endpoint_url.startswith(
            "https://opendata.jcyl.es/"
        )
        assert candidate.remote_name == reviewed.catalog_remote_name
        assert candidate.config["archive_member"]
        assert candidate.config["input_layer"]
        assert "archive_styles" not in candidate.config
        assert "source_content_parity" not in candidate.config
        integrity = configured_reviewed_archive_integrity(
            candidate.config
        )
        assert integrity is not None
        semantic, spec_sha256 = integrity
        assert len(spec_sha256) == 64
        assert (
            semantic["archive_constraints"]["max_uncompressed_bytes"]
            == candidate.config["archive_max_uncompressed_bytes"]
        )
        assert candidate.config["reviewed_equivalence"] == reviewed.evidence
        assert candidate.definition_sha256 == _canonical_sha256(
            candidate_definition(candidate)
        )
        definitions.add(candidate.definition_sha256)

    assert len(definitions) == 18
    assert inventory[105].candidate_config["data_format"] == (
        "shapefile-zip"
    )
    assert all(
        inventory[layer_id].candidate_config["data_format"]
        == "geopackage-zip"
        for layer_id in REVIEWABLE_ARCHIVE_IDS - {105}
    )


def test_reviewable_archives_require_acceptance_attribution_and_conditional_license() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
    }

    for layer_id in REVIEWABLE_ARCHIVE_IDS:
        reviewed = inventory[layer_id]
        license_evidence = reviewed.evidence["license_evidence"]
        capture = reviewed.evidence["audit_capture"]

        assert reviewed.reason_codes == (
            "igcyl_nc_recipient_acceptance_requires_review",
            "igcyl_nc_visible_attribution_requires_review",
            "igcyl_nc_commercial_license_required_if_commercial",
            "local_service_requires_persisted_human_review",
        )
        assert license_evidence["license_name"] == "IGCYL-NC"
        assert license_evidence["recipient_acceptance_required"] is True
        assert (
            license_evidence[
                "commercial_license_required_if_commercial"
            ]
            is True
        )
        assert license_evidence["required_attribution"] == (
            "© Junta de Castilla y León"
        )
        assert capture["baseline_response"]["etag"]
        assert capture["baseline_entries"]
        assert reviewed.evidence["authorization_effect"] == (
            "none_without_persisted_human_mirror_review"
        )


def test_the_other_12_exact_identities_cannot_generate_a_candidate() -> None:
    blocked = [
        item
        for item in idecyl_exact_source_inventory()
        if item.local_service_status != "candidate"
    ]
    assert len(blocked) == 12

    for reviewed in blocked:
        with pytest.raises(SourceDiscoveryError) as captured:
            acquisition_candidates(
                _service(reviewed.catalog_endpoint_url),
                _layer(
                    reviewed.catalog_layer_source_key,
                    reviewed.catalog_remote_name,
                ),
            )

        assert captured.value.code == "idecyl_local_service_restricted"
        assert captured.value.evidence["local_service_status"] != "candidate"
        assert "endpoint_url" not in captured.value.evidence
        assert not any(
            text.startswith(("http://", "https://"))
            for text in _walk_strings(captured.value.evidence)
        )


def test_known_remote_name_cannot_bypass_classification_by_changing_key() -> None:
    reviewed = idecyl_exact_source_inventory()[0]
    wrong_source_key = "layer:siur:" + "0" * 64
    assert wrong_source_key != reviewed.catalog_layer_source_key

    with pytest.raises(SourceDiscoveryError) as captured:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            _layer(wrong_source_key, reviewed.catalog_remote_name),
        )

    assert captured.value.code == "reviewed_idecyl_evidence_invalid"
    with pytest.raises(IDECyLExactEvidenceError):
        reviewed_idecyl_exact_source(
            catalog_layer_source_key=wrong_source_key,
            catalog_endpoint_url=reviewed.catalog_endpoint_url,
            catalog_remote_name=reviewed.catalog_remote_name,
        )


def test_candidate_identity_or_style_drift_fails_closed() -> None:
    reviewed = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == 39
    )
    valid = _layer(
        reviewed.catalog_layer_source_key,
        reviewed.catalog_remote_name,
        with_telecom_styles=True,
    )

    with pytest.raises(SourceDiscoveryError) as changed_role:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            replace(valid, role="base"),
        )
    assert changed_role.value.code == "reviewed_idecyl_identity_invalid"

    with pytest.raises(SourceDiscoveryError) as changed_styles:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            replace(valid, styles=valid.styles[:-1]),
        )
    assert (
        changed_styles.value.code
        == "reviewed_idecyl_style_identity_invalid"
    )

    changed_default = tuple(
        replace(
            style,
            is_default=(
                style.remote_name
                == "telefonia_movil_cyl_cobertura_carreteras_4g_cnmc"
            ),
        )
        for style in valid.styles
    )
    with pytest.raises(SourceDiscoveryError) as default_drift:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            replace(valid, styles=changed_default),
        )
    assert (
        default_drift.value.code
        == "reviewed_idecyl_style_identity_invalid"
    )


def _exact_archive_styles() -> list[dict[str, Any]]:
    return [
        {
            "catalog_style_source_key": "official_a",
            "remote_name": "catalog_a",
            "is_default": True,
            "archive_member": "styles/official-a.sld",
            "sha256": "a" * 64,
            "size_bytes": 1_234,
            "crc32": "0123abcd",
            "sld_layer_name": "archive_layer",
            "sld_style_name": "official_a",
        },
        {
            "catalog_style_source_key": "official_b",
            "remote_name": "catalog_b",
            "is_default": False,
            "archive_member": "styles/official-b.sld",
            "sha256": "b" * 64,
            "size_bytes": 2_345,
            "crc32": "89abcdef",
            "sld_layer_name": "archive_layer",
            "sld_style_name": "official_b",
        },
    ]


def _layer_with_exact_archive_styles() -> ReferenceLayerDefinition:
    return replace(
        _layer("layer:siur:" + "a" * 64, "catalog_layer"),
        style_name="catalog_a",
        styles=(
            ReferenceLayerStyleDefinition(
                source_key="official_a",
                title="A",
                remote_name="catalog_a",
                sort_order=0,
                is_default=True,
            ),
            ReferenceLayerStyleDefinition(
                source_key="official_b",
                title="B",
                remote_name="catalog_b",
                sort_order=1,
                is_default=False,
            ),
        ),
    )


def test_exact_archive_styles_bind_every_catalog_identity_and_default() -> None:
    raw_styles = _exact_archive_styles()

    assert _idecyl_archive_style_config(
        _layer_with_exact_archive_styles(),
        raw_styles,
    ) == raw_styles


@pytest.mark.parametrize(
    "mutate",
    [
        lambda styles: styles.pop(),
        lambda styles: styles[0].update(
            {"catalog_style_source_key": "wrong"}
        ),
        lambda styles: styles[0].update({"is_default": False}),
        lambda styles: styles[1].update(
            {"archive_member": styles[0]["archive_member"]}
        ),
        lambda styles: styles[1].update({"archive_member": "../style.sld"}),
        lambda styles: styles[1].update({"sha256": styles[0]["sha256"]}),
    ],
)
def test_exact_archive_styles_reject_partial_or_invented_mappings(
    mutate,
) -> None:
    raw_styles = _exact_archive_styles()
    mutate(raw_styles)

    with pytest.raises(SourceDiscoveryError) as captured:
        _idecyl_archive_style_config(
            _layer_with_exact_archive_styles(),
            raw_styles,
        )

    assert (
        captured.value.code
        == "reviewed_idecyl_style_identity_invalid"
    )


@pytest.mark.parametrize(
    ("resource_path", "expected"),
    [
        (MANIFEST_RESOURCE, MANIFEST_SHA256),
        (PREVIOUS_MANIFEST_RESOURCE, PREVIOUS_MANIFEST_SHA256),
        (LEGACY_MANIFEST_RESOURCE, LEGACY_MANIFEST_SHA256),
        (RECORDS_RESOURCE, RECORDS_SHA256),
    ],
)
def test_committed_evidence_bytes_are_exact(
    resource_path: str,
    expected: str,
) -> None:
    assert hashlib.sha256(_resource_body(resource_path)).hexdigest() == expected


def test_tampered_manifest_is_rejected_before_it_can_build_a_source() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    previous = _resource_body(PREVIOUS_MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    records = _resource_body(RECORDS_RESOURCE)
    tampered = manifest.replace(b'"candidate"', b'"restricted"', 1)
    assert tampered != manifest

    with pytest.raises(
        IDECyLExactEvidenceError,
        match="failed its local digest",
    ):
        _load_evidence_package(
            tampered,
            previous,
            legacy,
            records,
            expected_manifest_sha256=MANIFEST_SHA256,
        )

    with pytest.raises(
        IDECyLExactEvidenceError,
        match="technical classification changed",
    ):
        _load_evidence_package(
            tampered,
            previous,
            legacy,
            records,
            expected_manifest_sha256=hashlib.sha256(tampered).hexdigest(),
        )


@pytest.mark.parametrize(
    ("resource_name", "match"),
    [
        (
            "previous",
            "previous classification manifest failed its local digest",
        ),
        ("legacy", "legacy inventory failed its local digest"),
        ("records", "metadata records failed its local digest"),
    ],
)
def test_tampered_bound_inputs_are_rejected(
    resource_name: str,
    match: str,
) -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    previous = _resource_body(PREVIOUS_MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    records = _resource_body(RECORDS_RESOURCE)
    if resource_name == "previous":
        previous = previous.replace(b'"restricted"', b'"candidate"', 1)
    elif resource_name == "legacy":
        legacy = legacy.replace(b"telefonia_movil", b"telefonia_Xovil", 1)
    else:
        records = records.replace(
            b"Junta de Castilla y Le",
            b"Xunta de Castilla y Le",
            1,
        )

    with pytest.raises(IDECyLExactEvidenceError, match=match):
        _load_evidence_package(
            manifest,
            previous,
            legacy,
            records,
            expected_manifest_sha256=MANIFEST_SHA256,
        )


def test_candidate_exposes_review_questions_without_authorizing_itself() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
    }
    candidate = inventory[39]
    metadata = candidate.evidence["official_metadata"]
    forbidden = {
        "reviewer",
        "reviewed_at",
        "allow_download",
        "allow_cache",
        "allow_redistribution",
    }

    assert metadata["attribution"] == "© Junta de Castilla y León"
    assert metadata["terms_url"].startswith("https://")
    assert "suscripción externa" in metadata["lineage_caveat"]
    assert "GeoHash" in metadata["lineage_caveat"]
    assert len(candidate.evidence["review_requirements"]) == 3
    assert (
        candidate.evidence["authorization_effect"]
        == "none_without_persisted_human_mirror_review"
    )
    assert not forbidden.intersection(candidate.evidence)
    assert "BDLJE" not in " ".join(
        _walk_strings(inventory[105].evidence)
    )


def test_candidate_has_no_http_or_wfs_effective_configuration() -> None:
    reviewed = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == 39
    )
    candidate = acquisition_candidates(
        _service(reviewed.catalog_endpoint_url),
        _layer(
            reviewed.catalog_layer_source_key,
            reviewed.catalog_remote_name,
            with_telecom_styles=True,
        ),
    )[0]
    strings = _walk_strings(candidate_definition(candidate))

    assert not any(text.startswith("http://") for text in strings)
    assert not any("/wfs" in text.casefold() for text in strings)
    assert "content_parity_required_before_promotion" not in strings


def test_candidate_parity_is_a_real_exact_spec() -> None:
    candidate = next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == 39
    )
    configured = configured_parity_spec(candidate.candidate_config or {})

    assert configured is not None
    expected, digest = configured
    assert digest == (
        "fb86dd729e3f679cc8899bf47f340f35"
        "ced01ba6dea82daa6d12cb57ea6f56e8"
    )
    assert expected["archive_sha256"] == (
        "a2ef017ba261e9acf35836a6110b019d"
        "14529c5c8b893b176367be8d4bd2d80f"
    )
    assert expected["feature_count"] == 11_791
    assert expected["crs"] == "EPSG:25830"
    assert expected["geometry_type"] == "MULTICURVE"
    assert expected["declared_bounds"] == expected["geometry_bounds"]
    assert expected["sample_sha256"]
    assert expected["data_schema_sha256"]
    assert expected["content_identity_sha256"]
