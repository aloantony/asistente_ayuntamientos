from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from importlib.resources import files
from typing import Any

import pytest

from app.reference_layers.catalog import (
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
)
from app.reference_layers.idecyl_exact_evidence import (
    BASE_MANIFEST_RESOURCE,
    BASE_MANIFEST_SHA256,
    EVIDENCE_SCHEMA,
    LEGACY_MANIFEST_RESOURCE,
    LEGACY_MANIFEST_SHA256,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    PREVIOUS_MANIFEST_RESOURCE,
    PREVIOUS_MANIFEST_SHA256,
    RECORDS_RESOURCE,
    RECORDS_SHA256,
    WFS_SNAPSHOT_MANIFEST_RESOURCE,
    WFS_SNAPSHOT_MANIFEST_SHA256,
    IDECyLExactEvidenceError,
    _load_evidence_package,
    idecyl_exact_source_inventory,
    reviewed_idecyl_exact_source,
)
from app.reference_layers.idecyl_local_style_evidence import (
    idecyl_local_style_inventory,
)
from app.reference_layers.idecyl_population_nitrate_style_evidence import (
    idecyl_nitrate_style_inventory,
    idecyl_population_style_exclusion,
)
from app.reference_layers.reviewed_archive_integrity import (
    configured_reviewed_archive_integrity,
)
from app.reference_layers.source_content_parity import (
    configured_parity_spec,
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
WFS_CANDIDATE_IDS = {
    78,
    84,
    118,
    142,
    161,
    171,
    194,
    243,
    246,
    281,
}
COMPLETE_ARCHIVE_STYLE_IDS = {98, 137, 151, 197, 213, 225, 230}
MIXED_EXACT_ADAPTED_STYLE_IDS = {268}


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
    with_cami_style: bool = False,
    boundary_layer_id: int | None = None,
) -> ReferenceLayerDefinition:
    if with_telecom_styles:
        styles = tuple(
            ReferenceLayerStyleDefinition(
                source_key=name.casefold(),
                title=name,
                remote_name=name,
                sort_order=index,
                is_default=index == 0,
            )
            for index, name in enumerate(TELECOM_STYLES)
        )
        style_name = TELECOM_STYLES[0]
    elif with_cami_style:
        style_name = "cami_cyl_cuadricula_default"
        styles = (
            ReferenceLayerStyleDefinition(
                source_key=style_name,
                title="Borde celdas negro",
                remote_name=style_name,
                is_default=True,
            ),
        )
    elif boundary_layer_id is not None:
        reviewed_styles = [
            item
            for item in idecyl_local_style_inventory()
            if item.audit_layer_id == boundary_layer_id
        ]
        styles = tuple(
            ReferenceLayerStyleDefinition(
                source_key=item.catalog_style_source_key,
                title=item.style_title,
                remote_name=item.remote_style_name,
                is_default=item.is_default,
            )
            for item in reviewed_styles
        )
        style_name = next(
            item.catalog_style_source_key for item in reviewed_styles if item.is_default
        )
    else:
        styles = ()
        style_name = TELECOM_STYLES[0]
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
        style_name=style_name,
        styles=styles,
    )


def _layer_with_reviewed_archive_styles(
    reviewed,
) -> ReferenceLayerDefinition:
    base = _layer(
        reviewed.catalog_layer_source_key,
        reviewed.catalog_remote_name,
        with_cami_style=reviewed.audit_layer_id == 86,
        boundary_layer_id=(
            reviewed.audit_layer_id if reviewed.audit_layer_id in {223, 234} else None
        ),
    )
    style_evidence = reviewed.evidence.get("archive_style_evidence")
    if reviewed.audit_layer_id == 237:
        catalog_styles = idecyl_population_style_exclusion()["catalog_styles"]
    elif isinstance(style_evidence, dict):
        catalog_styles = style_evidence["catalog_styles"]
    else:
        return base
    nitrate_titles = {
        item.catalog_style_source_key: item.style_title
        for item in idecyl_nitrate_style_inventory()
    }
    nitrate_titles["coad_cyl_nitrat_aguas_subterr_2021"] = (
        "Recintos municipales 2021 paleta color"
    )
    default = next(item for item in catalog_styles if item["is_default"] is True)
    return replace(
        base,
        style_name=default["catalog_style_source_key"],
        styles=tuple(
            ReferenceLayerStyleDefinition(
                source_key=item["catalog_style_source_key"],
                title=item.get(
                    "title",
                    nitrate_titles.get(
                        item["catalog_style_source_key"],
                        item["remote_name"],
                    ),
                ),
                remote_name=item["remote_name"],
                sort_order=index,
                is_default=item["is_default"],
            )
            for index, item in enumerate(catalog_styles)
        ),
    )


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
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
    assert {item.catalog_remote_name for item in inventory} == (EXPECTED_REMOTE_NAMES)
    assert sum(item.local_service_status == "candidate" for item in inventory) == 29
    assert sum(item.local_service_status == "restricted" for item in inventory) == 2
    assert (
        sum(item.local_service_status == "permission_pending" for item in inventory)
        == 0
    )
    assert {
        item.audit_layer_id
        for item in inventory
        if item.local_service_status == "candidate"
    } == {39, *REVIEWABLE_ARCHIVE_IDS, *WFS_CANDIDATE_IDS}
    assert all(item.evidence["schema"] == EVIDENCE_SCHEMA for item in inventory)


def test_sigpac_uses_exact_https_directories_but_remains_license_restricted() -> None:
    inventory = {item.audit_layer_id: item for item in idecyl_exact_source_inventory()}

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
            "https://ftp.itacyl.es/cartografia/05_SIGPAC/" f"{year}_ETRS89/"
        )
        assert distribution["province_directory_url"] == (
            distribution["root_directory_url"] + "Parcelario_SIGPAC_CyL_Provincias/"
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
        item for item in idecyl_exact_source_inventory() if item.audit_layer_id == 39
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
    assert {item["remote_name"] for item in candidate.config["archive_styles"]} == set(
        TELECOM_STYLES
    )
    assert all(len(item["sha256"]) == 64 for item in candidate.config["archive_styles"])
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
    inventory = {item.audit_layer_id: item for item in idecyl_exact_source_inventory()}
    definitions: set[str] = set()

    for layer_id in sorted(REVIEWABLE_ARCHIVE_IDS):
        reviewed = inventory[layer_id]
        candidates = acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            _layer_with_reviewed_archive_styles(reviewed),
        )

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.protocol == "download"
        assert candidate.target_kind == "vector"
        assert candidate.sync_strategy == "conditional_get"
        assert candidate.endpoint_url.startswith("https://opendata.jcyl.es/")
        assert candidate.remote_name == reviewed.catalog_remote_name
        assert candidate.config["archive_member"]
        assert candidate.config["input_layer"]
        if layer_id in MIXED_EXACT_ADAPTED_STYLE_IDS:
            assert len(candidate.config["archive_styles"]) == 1
            assert len(candidate.config["reviewed_local_styles"]) == 15
            assert len(candidate.config["archive_style_catalog"]) == 16
            assert "archive_style_archive_sha256" not in candidate.config
        elif layer_id in COMPLETE_ARCHIVE_STYLE_IDS:
            assert len(candidate.config["archive_styles"]) == 1
            assert len(candidate.config["archive_style_archive_sha256"]) == 64
            assert "archive_style_catalog" not in candidate.config
        else:
            assert "archive_styles" not in candidate.config
            assert "archive_style_archive_sha256" not in candidate.config
        assert "source_content_parity" not in candidate.config
        if layer_id == 86:
            assert candidate.config["reviewed_local_style"] == {
                "schema": "siur-reviewed-idecyl-local-style/v1",
                "audit_layer_id": 86,
                "profile": reviewed.profile,
                "recipe_identity_sha256": (
                    "9cdde3badf9b6bd3af2425431680685"
                    "b75a6a97fd552b4be1e292a8e7cfef79d"
                ),
                "catalog_style_source_key": ("cami_cyl_cuadricula_default"),
                "remote_name": "cami_cyl_cuadricula_default",
                "is_default": True,
            }
        elif layer_id in {223, 234}:
            assert len(candidate.config["reviewed_local_styles"]) == 6
            assert (
                sum(
                    style["is_default"]
                    for style in candidate.config["reviewed_local_styles"]
                )
                == 1
            )
        elif layer_id in MIXED_EXACT_ADAPTED_STYLE_IDS:
            assert "reviewed_local_style" not in candidate.config
            assert len(candidate.config["reviewed_local_styles"]) == 15
        else:
            assert "reviewed_local_style" not in candidate.config
            assert "reviewed_local_styles" not in candidate.config
        if layer_id == 237:
            exclusion = candidate.config["reviewed_local_style_exclusion"]
            assert exclusion["local_service_eligible"] is False
            assert exclusion["catalog_style_count"] == 6
        else:
            assert "reviewed_local_style_exclusion" not in candidate.config
        integrity = configured_reviewed_archive_integrity(candidate.config)
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
    assert inventory[105].candidate_config["data_format"] == ("shapefile-zip")
    assert all(
        inventory[layer_id].candidate_config["data_format"] == "geopackage-zip"
        for layer_id in REVIEWABLE_ARCHIVE_IDS - {105}
    )
    assert (
        inventory[268].evidence["archive_style_evidence"]["style_coverage"]["complete"]
        is False
    )
    assert (
        len(
            inventory[268].evidence["archive_style_evidence"]["style_coverage"][
                "missing_catalog_style_source_keys"
            ]
        )
        == 15
    )


@pytest.mark.parametrize(
    "mutation",
    ["source_key", "remote_name", "title", "default"],
)
def test_layer_86_local_style_catalog_identity_fails_closed(
    mutation: str,
) -> None:
    reviewed = next(
        item for item in idecyl_exact_source_inventory() if item.audit_layer_id == 86
    )
    valid = _layer(
        reviewed.catalog_layer_source_key,
        reviewed.catalog_remote_name,
        with_cami_style=True,
    )
    style = valid.styles[0]
    if mutation == "source_key":
        changed = replace(style, source_key="changed")
        layer = replace(valid, styles=(changed,))
    elif mutation == "remote_name":
        changed = replace(style, remote_name="changed")
        layer = replace(valid, styles=(changed,))
    elif mutation == "title":
        changed = replace(style, title="changed")
        layer = replace(valid, styles=(changed,))
    else:
        changed = replace(style, is_default=False)
        layer = replace(valid, styles=(changed,))

    with pytest.raises(SourceDiscoveryError) as captured:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            layer,
        )

    assert captured.value.code == ("reviewed_idecyl_local_style_identity_invalid")


def test_all_10_wfs_sources_build_exact_convergent_candidates() -> None:
    inventory = {item.audit_layer_id: item for item in idecyl_exact_source_inventory()}
    definitions: set[str] = set()

    for layer_id in sorted(WFS_CANDIDATE_IDS):
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
        assert candidate.protocol == "wfs"
        assert candidate.target_kind == "vector"
        assert candidate.sync_strategy == "full_snapshot"
        assert candidate.endpoint_url.startswith("https://idecyl.jcyl.es/geoserver/")
        assert candidate.endpoint_url.endswith("/wfs")
        assert ":" in candidate.remote_name
        assert candidate.config["discovery"] == "wfs_capabilities"
        assert candidate.config["reviewed_equivalence"] == reviewed.evidence
        assert candidate.config["style_endpoint_url"] == (reviewed.catalog_endpoint_url)
        assert candidate.config["style_layer_name"] == (reviewed.catalog_remote_name)
        assert candidate.config["styles"] == []
        assert candidate.definition_sha256 == _canonical_sha256(
            candidate_definition(candidate)
        )
        definitions.add(candidate.definition_sha256)

        snapshot = candidate.config["wfs_snapshot"]
        projection = reviewed.evidence["wfs_snapshot_projection"]
        assert projection["required_matching_passes"] == 2
        assert projection["authorization_granted"] is False
        assert projection["local_download_authorized"] is False
        assert projection["local_service_authorized"] is False
        if layer_id in {243, 281}:
            assert candidate.config["page_size"] == 11_000
            assert snapshot == {
                "identity_properties": ["fid"],
                "mode": "paged",
            }
        else:
            assert "page_size" not in candidate.config
            assert snapshot == {"mode": "single_response"}

    assert len(definitions) == 10


def test_wfs_candidates_project_hash_bound_non_authorizing_license() -> None:
    for reviewed in idecyl_exact_source_inventory():
        if reviewed.audit_layer_id not in WFS_CANDIDATE_IDS:
            continue

        assert reviewed.reason_codes == (
            "igcyl_nc_recipient_acceptance_requires_review",
            "igcyl_nc_visible_attribution_requires_review",
            "igcyl_nc_commercial_license_required_if_commercial",
            "local_service_requires_persisted_human_review",
        )
        assert reviewed.profile == (
            f"idecyl-{reviewed.catalog_remote_name}" "-wfs-snapshot-20260727-v4"
        )
        license_evidence = reviewed.evidence["license_evidence"]
        assert license_evidence == {
            "authorization_effect": ("none_without_persisted_human_mirror_review"),
            "authorization_granted": False,
            "commercial_license_required_if_commercial": True,
            "license_name": "LICENCIA-IGCYL-NC",
            "license_url": (
                "https://ftp.itacyl.es/cartografia/" "LICENCIA-IGCYL-NC-2012.pdf"
            ),
            "local_download_authorized": False,
            "local_service_authorized": False,
            "recipient_acceptance_required": True,
            "required_attribution": "© Junta de Castilla y León",
        }
        assert reviewed.evidence["authorization_effect"] == (
            "none_without_persisted_human_mirror_review"
        )
        assert (
            reviewed.evidence["wfs_snapshot_evidence"]["license_gate"][
                "authorization_granted"
            ]
            is False
        )


def test_reviewable_archives_require_acceptance_attribution_and_conditional_license() -> (
    None
):
    inventory = {item.audit_layer_id: item for item in idecyl_exact_source_inventory()}

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
        assert license_evidence["commercial_license_required_if_commercial"] is True
        assert license_evidence["required_attribution"] == (
            "© Junta de Castilla y León"
        )
        assert capture["baseline_response"]["etag"]
        assert capture["baseline_entries"]
        assert reviewed.evidence["authorization_effect"] == (
            "none_without_persisted_human_mirror_review"
        )


def test_only_the_two_sigpac_identities_cannot_generate_a_candidate() -> None:
    blocked = [
        item
        for item in idecyl_exact_source_inventory()
        if item.local_service_status != "candidate"
    ]
    assert {item.audit_layer_id for item in blocked} == {123, 166}

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
        item for item in idecyl_exact_source_inventory() if item.audit_layer_id == 39
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
    assert changed_styles.value.code == "reviewed_idecyl_style_identity_invalid"

    changed_default = tuple(
        replace(
            style,
            is_default=(
                style.remote_name == "telefonia_movil_cyl_cobertura_carreteras_4g_cnmc"
            ),
        )
        for style in valid.styles
    )
    with pytest.raises(SourceDiscoveryError) as default_drift:
        acquisition_candidates(
            _service(reviewed.catalog_endpoint_url),
            replace(valid, styles=changed_default),
        )
    assert default_drift.value.code == "reviewed_idecyl_style_identity_invalid"


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

    assert (
        _idecyl_archive_style_config(
            _layer_with_exact_archive_styles(),
            raw_styles,
        )
        == raw_styles
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda styles: styles.pop(),
        lambda styles: styles[0].update({"catalog_style_source_key": "wrong"}),
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

    assert captured.value.code == "reviewed_idecyl_style_identity_invalid"


@pytest.mark.parametrize(
    ("resource_path", "expected"),
    [
        (MANIFEST_RESOURCE, MANIFEST_SHA256),
        (PREVIOUS_MANIFEST_RESOURCE, PREVIOUS_MANIFEST_SHA256),
        (BASE_MANIFEST_RESOURCE, BASE_MANIFEST_SHA256),
        (
            WFS_SNAPSHOT_MANIFEST_RESOURCE,
            WFS_SNAPSHOT_MANIFEST_SHA256,
        ),
        (LEGACY_MANIFEST_RESOURCE, LEGACY_MANIFEST_SHA256),
        (RECORDS_RESOURCE, RECORDS_SHA256),
    ],
)
def test_committed_evidence_bytes_are_exact(
    resource_path: str,
    expected: str,
) -> None:
    assert hashlib.sha256(_resource_body(resource_path)).hexdigest() == expected


def test_v4_manifest_is_canonical_and_explicitly_non_authorizing() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    parsed = json.loads(body)

    assert body == _canonical_json_bytes(parsed)
    assert parsed["schema"] == ("siur-idecyl-local-service-classification/v4")
    assert len(parsed["wfs_candidate_sources"]) == 10
    assert parsed["capture"]["authorization_granted"] is False
    assert parsed["capture"]["local_download_authorized"] is False
    assert parsed["capture"]["local_service_authorized"] is False
    assert parsed["capture"]["resulting_classification"] == {
        "candidate_count": 29,
        "restricted_count": 2,
        "restricted_layer_ids": [123, 166],
    }
    assert (
        parsed["capture"]["license_evidence"]["required_attribution"]
        == "© Junta de Castilla y León"
    )


@pytest.mark.parametrize("mutation", ["list", "config", "authorization"])
def test_recomputed_v4_digest_rejects_wfs_candidate_drift(
    mutation: str,
) -> None:
    parsed = json.loads(_resource_body(MANIFEST_RESOURCE))
    if mutation == "list":
        parsed["wfs_candidate_sources"].pop()
        match = "candidate list or configuration changed"
    elif mutation == "config":
        parsed["wfs_candidate_sources"][0]["candidate_config"]["wfs_snapshot"][
            "mode"
        ] = "paged"
        match = "candidate list or configuration changed"
    else:
        parsed["capture"]["authorization_granted"] = True
        match = "successor classification capture changed"
    tampered = _canonical_json_bytes(parsed)

    with pytest.raises(IDECyLExactEvidenceError, match=match):
        _load_evidence_package(
            tampered,
            _resource_body(PREVIOUS_MANIFEST_RESOURCE),
            _resource_body(BASE_MANIFEST_RESOURCE),
            _resource_body(WFS_SNAPSHOT_MANIFEST_RESOURCE),
            _resource_body(LEGACY_MANIFEST_RESOURCE),
            _resource_body(RECORDS_RESOURCE),
            expected_manifest_sha256=hashlib.sha256(tampered).hexdigest(),
        )


def test_tampered_manifest_is_rejected_before_it_can_build_a_source() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    previous = _resource_body(PREVIOUS_MANIFEST_RESOURCE)
    base = _resource_body(BASE_MANIFEST_RESOURCE)
    wfs_snapshot = _resource_body(WFS_SNAPSHOT_MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    records = _resource_body(RECORDS_RESOURCE)
    tampered = manifest.replace(
        b'"mode": "single_response"',
        b'"mode": "paged"',
        1,
    )
    assert tampered != manifest

    with pytest.raises(
        IDECyLExactEvidenceError,
        match="failed its local digest",
    ):
        _load_evidence_package(
            tampered,
            previous,
            base,
            wfs_snapshot,
            legacy,
            records,
            expected_manifest_sha256=MANIFEST_SHA256,
        )

    with pytest.raises(
        IDECyLExactEvidenceError,
        match="candidate list or configuration changed",
    ):
        _load_evidence_package(
            tampered,
            previous,
            base,
            wfs_snapshot,
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
        ("base", "base classification manifest failed its local digest"),
        (
            "wfs_snapshot",
            "WFS snapshot observation manifest failed its local digest",
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
    base = _resource_body(BASE_MANIFEST_RESOURCE)
    wfs_snapshot = _resource_body(WFS_SNAPSHOT_MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    records = _resource_body(RECORDS_RESOURCE)
    if resource_name == "previous":
        previous = previous.replace(b'"restricted"', b'"candidate"', 1)
    elif resource_name == "base":
        base = base.replace(b'"restricted"', b'"candidate"', 1)
    elif resource_name == "wfs_snapshot":
        wfs_snapshot = wfs_snapshot.replace(
            b'"observed_feature_count": 9657',
            b'"observed_feature_count": 9658',
            1,
        )
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
            base,
            wfs_snapshot,
            legacy,
            records,
            expected_manifest_sha256=MANIFEST_SHA256,
        )


def test_candidate_exposes_review_questions_without_authorizing_itself() -> None:
    inventory = {item.audit_layer_id: item for item in idecyl_exact_source_inventory()}
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
    assert "BDLJE" not in " ".join(_walk_strings(inventory[105].evidence))


def test_candidate_has_no_http_or_wfs_effective_configuration() -> None:
    reviewed = next(
        item for item in idecyl_exact_source_inventory() if item.audit_layer_id == 39
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
        item for item in idecyl_exact_source_inventory() if item.audit_layer_id == 39
    )
    configured = configured_parity_spec(candidate.candidate_config or {})

    assert configured is not None
    expected, digest = configured
    assert digest == (
        "fb86dd729e3f679cc8899bf47f340f35" "ced01ba6dea82daa6d12cb57ea6f56e8"
    )
    assert expected["archive_sha256"] == (
        "a2ef017ba261e9acf35836a6110b019d" "14529c5c8b893b176367be8d4bd2d80f"
    )
    assert expected["feature_count"] == 11_791
    assert expected["crs"] == "EPSG:25830"
    assert expected["geometry_type"] == "MULTICURVE"
    assert expected["declared_bounds"] == expected["geometry_bounds"]
    assert expected["sample_sha256"]
    assert expected["data_schema_sha256"]
    assert expected["content_identity_sha256"]
