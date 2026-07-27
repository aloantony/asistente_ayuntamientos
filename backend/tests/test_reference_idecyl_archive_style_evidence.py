from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from types import SimpleNamespace

import pytest

from app.reference_layers.idecyl_archive_style_evidence import (
    IDECyLArchiveStyleEvidenceError,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    load_idecyl_archive_style_evidence,
)
from app.reference_layers.idecyl_exact_evidence import (
    PREVIOUS_MANIFEST_RESOURCE,
    PREVIOUS_MANIFEST_SHA256,
    idecyl_exact_source_inventory,
)
from app.reference_layers.style_parity import (
    StyleParityError,
    StyleParityPlanResult,
    _evaluate_items,
    _required_styles,
    require_complete_style_parity,
)


EXPECTED_STYLES = {
    98: (
        "ot_cyl_instrumentos_ambito_trama",
        "urbanismo:ot_cyl_instrumentos_ambito_trama",
        "ot_cyl_instrumentos_ambito.sld",
        "ot_cyl_instrumentos_ambito",
        "ot_cyl_instrumentos_ambito",
        "d50c1025dc6851d822cab37970d8cd06818bc88bd630064cdbe482df8f472849",
    ),
    137: (
        "en_cyl_rednatura2000_zec_violeta",
        "en_cyl_rednatura2000_zec_violeta",
        "en_cyl_rednatura2000_zec.sld",
        "en_cyl_rednatura2000_zec",
        "en_cyl_rednatura2000_zec",
        "108ba168863153c9ba3e1c19679bfb302500aad2cc14498784f973a31443776c",
    ),
    151: (
        "ot_cyl_mancomunidades_interes_general_azul",
        "ot_cyl_mancomunidades_interes_general_azul",
        "ot_cyl_mancomunidades_interes_general.sld",
        "ot_cyl_mancomunidades_interes_general",
        "ot_cyl_mancomunidades_interes_general",
        "2588fed0d218f22ced1298e8053bf33fb84ff17ac79412792e7cb4d8c0d0736c",
    ),
    197: (
        "plau_cyl_sectores_desclasif",
        "urbanismo:plau_cyl_sectores_desclasif",
        "plau_cyl_sectores_desclasif.sld",
        "plau_cyl_sectores_desclasif",
        "plau_cyl_sectores_desclasif",
        "f751131fafd3905412a90ba91b3eaa903358a6913512eb63712f13ea8e77b35a",
    ),
    213: (
        "en_cyl_znie_zhie_azul",
        "en_cyl_znie_zhie_azul",
        "en_cy_znie_zhie.sld",
        "en_cyl_znie_zhie",
        "en_cyl_znie_zhie",
        "9acf565a48714e54d27266faa124bd75512c919ddbb3f3fbebf69cfc7408050b",
    ),
    225: (
        "especies_prot_cyl_areas_criticas_rosa",
        "especies_prot_cyl_areas_criticas_rosa",
        "especies_prot_cyl_areas_criticas.sld",
        "especies_prot_cyl_areas_criticas",
        "especies_prot_cyl_areas_criticas",
        "1737604dbe4c0e34bba5314ab1a20191e4c2bff868925a3f819544b279ac6731",
    ),
    230: (
        "en_cyl_znie_zhie_zpp_trama",
        "en_cyl_znie_zhie_zpp_trama",
        "en_cy_znie_zhie_zpp.sld",
        "en_cyl_znie_zhie_zpp",
        "en_cyl_znie_zhie_zpp",
        "a0479155faf7354d0da967f2ca4d0500e678b8d687834375e6b9fc22c656f63a",
    ),
    268: (
        "coad_cyl_nitrat_aguas_subterr_2021",
        "coad_cyl_nitrat_aguas_subterr_2021",
        "coad_cyl_nitrat_aguas_subterr.sld",
        "coad_cyl_nitrat_aguas_subterr",
        "coad_cyl_nitrat_aguas_subterr",
        "ed4dff6410973ffd0e672a1a4216a7d11dab2118bc3b0a6056b2d1cfc2b87c38",
    ),
}


def _resource_body(resource_path: str) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    return resource.read_bytes()


def _source_inputs() -> tuple[dict[int, dict], dict[int, dict]]:
    v3 = json.loads(_resource_body(PREVIOUS_MANIFEST_RESOURCE))
    legacy = json.loads(
        _resource_body("evidence/idecyl_exact/manifest-v1.json")
    )
    return (
        {
            item["audit_layer_id"]: item
            for item in v3["reviewable_archive_sources"]
        },
        {
            item["audit_layer_id"]: item
            for item in legacy["sources"]
        },
    )


def _loaded(
    body: bytes | None = None,
    *,
    expected_sha256: str = MANIFEST_SHA256,
) -> dict[int, dict]:
    archive_sources, catalog_sources = _source_inputs()
    return load_idecyl_archive_style_evidence(
        body if body is not None else _resource_body(MANIFEST_RESOURCE),
        archive_sources=archive_sources,
        catalog_sources=catalog_sources,
        expected_manifest_sha256=expected_sha256,
    )


def _canonical_manifest_body(manifest: dict) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def test_manifest_binds_exact_catalog_archive_member_and_sld_identities() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    manifest = json.loads(body)
    evidence = _loaded(body)

    assert hashlib.sha256(body).hexdigest() == MANIFEST_SHA256
    assert sorted(evidence) == sorted(EXPECTED_STYLES)
    assert hashlib.sha256(
        _resource_body(PREVIOUS_MANIFEST_RESOURCE)
    ).hexdigest() == PREVIOUS_MANIFEST_SHA256

    raw_by_id = {
        item["audit_layer_id"]: item for item in manifest["layers"]
    }
    for layer_id, expected in EXPECTED_STYLES.items():
        [style] = evidence[layer_id]["archive_styles"]
        assert (
            style["catalog_style_source_key"],
            style["remote_name"],
            style["archive_member"],
            style["sld_layer_name"],
            style["sld_style_name"],
            style["sha256"],
        ) == expected
        assert style["is_default"] is True
        assert raw_by_id[layer_id]["styles"][0]["sld"][
            "self_contained"
        ] is True
        assert len(
            evidence[layer_id]["archive_capture"]["archive_sha256"]
        ) == 64

    assert (
        evidence[213]["archive_capture"]
        == evidence[230]["archive_capture"]
    )


def test_only_complete_style_evidence_reaches_candidate_configuration() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_exact_source_inventory()
    }

    for layer_id in {98, 137, 151, 197, 213, 225, 230}:
        config = inventory[layer_id].candidate_config
        assert config is not None
        assert len(config["archive_styles"]) == 1
        assert config["archive_style_archive_sha256"] == (
            inventory[layer_id]
            .evidence["archive_style_evidence"]["archive_capture"][
                "archive_sha256"
            ]
        )

    partial = inventory[268]
    assert partial.candidate_config is not None
    assert "archive_styles" not in partial.candidate_config
    assert "archive_style_archive_sha256" not in partial.candidate_config
    coverage = partial.evidence["archive_style_evidence"][
        "style_coverage"
    ]
    assert coverage["complete"] is False
    assert len(coverage["missing_catalog_style_source_keys"]) == 15


def test_manifest_byte_tampering_fails_its_pinned_digest() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    tampered = body.replace(
        b'"archive_sha256": "b4a48',
        b'"archive_sha256": "c4a48',
        1,
    )
    assert tampered != body

    with pytest.raises(
        IDECyLArchiveStyleEvidenceError,
        match="failed its local digest",
    ):
        _loaded(tampered)


def test_rehashed_archive_capture_tampering_fails_the_code_allowlist() -> None:
    manifest = json.loads(_resource_body(MANIFEST_RESOURCE))
    manifest["layers"][0]["archive_capture"]["archive_sha256"] = "f" * 64
    tampered = _canonical_manifest_body(manifest)

    with pytest.raises(
        IDECyLArchiveStyleEvidenceError,
        match="capture identity changed",
    ):
        _loaded(
            tampered,
            expected_sha256=hashlib.sha256(tampered).hexdigest(),
        )


def test_rehashed_weak_etag_or_source_drift_fails_closed() -> None:
    original = json.loads(_resource_body(MANIFEST_RESOURCE))
    variants = []
    weak = deepcopy(original)
    weak["layers"][0]["archive_capture"]["etag"] = 'W/"unsafe"'
    variants.append(weak)
    changed_source = deepcopy(original)
    changed_source["layers"][0]["source_identity"][
        "selected_endpoint_url"
    ] = "https://opendata.jcyl.es/ficheros/carto/other.zip"
    variants.append(changed_source)

    for manifest in variants:
        tampered = _canonical_manifest_body(manifest)
        with pytest.raises(IDECyLArchiveStyleEvidenceError):
            _loaded(
                tampered,
                expected_sha256=hashlib.sha256(tampered).hexdigest(),
            )


def test_partial_2021_style_still_leaves_style_parity_incomplete() -> None:
    evidence = _loaded()[268]
    catalog_styles = evidence["catalog_styles"]
    required = _required_styles(
        [
            SimpleNamespace(
                id=index,
                source_key=item["catalog_style_source_key"],
                remote_name=item["remote_name"],
                is_default=item["is_default"],
                status="active",
            )
            for index, item in enumerate(catalog_styles, start=1)
        ]
    )
    [reviewed_style] = evidence["archive_styles"]
    artifact = SimpleNamespace(
        artifact_id=1,
        artifact_kind="style",
        roles=frozenset({"style"}),
        media_type="application/vnd.ogc.sld+xml",
        storage_backend="filesystem",
        storage_key="sha256/reviewed.sld",
        size_bytes=reviewed_style["size_bytes"],
        sha256=reviewed_style["sha256"],
        metadata_json={
            "catalog_style_source_key": reviewed_style[
                "catalog_style_source_key"
            ],
            "parity_kind": "exact",
            "resource_bindings": [],
            "unresolved_resources": [],
        },
    )

    items = _evaluate_items(
        required,
        delivery_kind="vector",
        artifacts=[artifact],
        probe=None,
    )

    assert len(items) == 16
    assert sum(item.parity_kind == "missing" for item in items) == 15
    with pytest.raises(StyleParityError) as captured:
        require_complete_style_parity(
            StyleParityPlanResult(
                plan_id=1,
                complete=False,
                required_style_count=16,
                missing_style_count=15,
                missing_reason_codes=("style_artifact_missing",),
            )
        )
    assert captured.value.code == "style_parity_incomplete"
