from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
from importlib.resources import files
import json
from typing import Any

import pytest

from app.reference_layers.idecyl_wfs_snapshot_evidence import (
    CLASSIFICATION_MANIFEST_RESOURCE,
    CLASSIFICATION_MANIFEST_SHA256,
    EVIDENCE_SCHEMA,
    LEGACY_MANIFEST_RESOURCE,
    MANIFEST_RESOURCE,
    MANIFEST_SHA256,
    IDECyLWFSSnapshotEvidenceError,
    _load_evidence_package,
    idecyl_wfs_snapshot_evidence,
    idecyl_wfs_snapshot_inventory,
    idecyl_wfs_snapshot_projection,
    reviewed_idecyl_wfs_snapshot,
)


EXPECTED_COUNTS = {
    78: 9_657,
    84: 21_949,
    118: 2_354,
    142: 202,
    161: 711,
    171: 770,
    194: 192,
    243: 21_450,
    246: 2_208,
    281: 21_276,
}
EXPECTED_IDENTITY_PROPERTIES = {
    78: (),
    84: ("fid",),
    118: ("fid",),
    142: ("fid",),
    161: ("cod1x1",),
    171: ("fid",),
    194: ("fid",),
    243: ("fid",),
    246: ("c_rel_reg",),
    281: ("fid",),
}
EXPECTED_METADATA_MISMATCHES = {
    142: (
        "SPAGOBCYLMNADTSAMMPA",
        "spagobcyltemmonprocomcyl",
    ),
    171: (
        "SPAGOBCYLMNADTSAMMCR",
        "spagobcyltemmontesconrep",
    ),
}
EXPECTED_CAPABILITIES_SHA256 = {
    78: (
        "7ad4329e4766e058786fef55889e5808d"
        "c3d94dbe3a4da84a289f2c60294eb21"
    ),
    84: (
        "813aeef71a8a5e6552536c4c445180a9"
        "99279f99d977130ab864cb1eb801cf3c"
    ),
    118: (
        "4d37cfd8ce3902595a43a1121732cb05"
        "1587454a4430b5d980e1ac7848df7905"
    ),
    142: (
        "7ad4329e4766e058786fef55889e5808d"
        "c3d94dbe3a4da84a289f2c60294eb21"
    ),
    161: (
        "53c2eb0bd519a9c66a425b64faad9d6b"
        "dc87feeae2d4fee79faec732f86b7ab4"
    ),
    171: (
        "7ad4329e4766e058786fef55889e5808d"
        "c3d94dbe3a4da84a289f2c60294eb21"
    ),
    194: (
        "53c2eb0bd519a9c66a425b64faad9d6b"
        "dc87feeae2d4fee79faec732f86b7ab4"
    ),
    243: (
        "8aa0d90d1f1bac268ef4c9a00afe2442"
        "6f3fb7b47dc3fb8fe8b4909242962573"
    ),
    246: (
        "239be6c294ad095beab5413f5f1af661"
        "3b9a65ee8ea2af76143321e4c88d34ff"
    ),
    281: (
        "8aa0d90d1f1bac268ef4c9a00afe2442"
        "6f3fb7b47dc3fb8fe8b4909242962573"
    ),
}


def _resource_body(resource_path: str) -> bytes:
    resource = files("app.reference_layers")
    for component in resource_path.split("/"):
        resource = resource.joinpath(component)
    return resource.read_bytes()


def _canonical_json(value: dict[str, Any]) -> bytes:
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


def test_manifest_is_hash_bound_canonical_and_has_no_wfs_payloads() -> None:
    body = _resource_body(MANIFEST_RESOURCE)
    classification_body = _resource_body(
        CLASSIFICATION_MANIFEST_RESOURCE
    )
    parsed = json.loads(body)
    evidence_directory = (
        files("app.reference_layers")
        .joinpath("evidence")
        .joinpath("idecyl_wfs_snapshot")
    )

    assert hashlib.sha256(body).hexdigest() == MANIFEST_SHA256
    assert hashlib.sha256(classification_body).hexdigest() == (
        CLASSIFICATION_MANIFEST_SHA256
    )
    assert body == _canonical_json(parsed)
    assert parsed["capture"]["classification_manifest"] == {
        "resource": CLASSIFICATION_MANIFEST_RESOURCE,
        "sha256": CLASSIFICATION_MANIFEST_SHA256,
    }
    assert {
        item.name for item in evidence_directory.iterdir()
    } == {"manifest-v1.json"}


def test_inventory_contains_the_ten_exact_observed_wfs_identities() -> None:
    inventory = idecyl_wfs_snapshot_inventory()

    assert [item.audit_layer_id for item in inventory] == sorted(
        EXPECTED_COUNTS
    )
    assert {
        item.audit_layer_id: item.observed_feature_count
        for item in inventory
    } == EXPECTED_COUNTS
    assert {
        item.audit_layer_id: item.identity_properties
        for item in inventory
    } == EXPECTED_IDENTITY_PROPERTIES
    assert {
        item.audit_layer_id: item.capabilities_sha256
        for item in inventory
    } == EXPECTED_CAPABILITIES_SHA256
    assert all(item.crs == "EPSG:25830" for item in inventory)
    assert all(item.wfs_version == "2.0.0" for item in inventory)
    assert all(item.endpoint_url.startswith("https://") for item in inventory)
    assert all(":" in item.type_name for item in inventory)
    assert all(
        item.required_matching_passes == 2
        for item in inventory
    )
    assert all(
        item.capabilities
        == {
            "implements_result_paging": True,
            "implements_sorting": True,
            "paging_is_transaction_safe": False,
            "supports_result_type_hits": True,
        }
        for item in inventory
    )


def test_population_layers_record_the_observed_cap_not_count_default() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_wfs_snapshot_inventory()
    }

    for layer_id, feature_count in ((243, 21_450), (281, 21_276)):
        observed = inventory[layer_id]
        evidence = idecyl_wfs_snapshot_evidence(observed)
        projection = idecyl_wfs_snapshot_projection(evidence)

        assert projection is not None
        assert evidence["classification_manifest"] == {
            "resource": CLASSIFICATION_MANIFEST_RESOURCE,
            "sha256": CLASSIFICATION_MANIFEST_SHA256,
        }
        assert (
            projection["capabilities_sha256"]
            == observed.capabilities_sha256
        )
        assert (
            projection["classification_manifest_sha256"]
            == CLASSIFICATION_MANIFEST_SHA256
        )
        assert observed.count_default == 25_000
        assert observed.observed_response_cap == 11_000
        assert observed.hits_without_count_observed == 11_000
        assert observed.hits_with_count_1_observed == feature_count
        assert observed.observed_feature_count == feature_count
        assert observed.safe_snapshot_method == "paged"
        assert observed.page_size == 11_000
        assert observed.sort_by == ("fid",)
        assert observed.identity_properties_role == "paging_sort_key"
        assert projection["observed_response_cap"] == 11_000
        assert projection["hits_without_count_observed"] == 11_000
        assert (
            projection["hits_with_count_1_observed"]
            == feature_count
        )


def test_single_response_identity_properties_are_diagnostic_only() -> None:
    inventory = {
        item.audit_layer_id: item
        for item in idecyl_wfs_snapshot_inventory()
    }
    content_multiset = inventory[78]

    assert content_multiset.safe_snapshot_method == "single_response"
    assert content_multiset.identity_mode == "content_multiset"
    assert content_multiset.identity_properties == ()
    assert content_multiset.identity_properties_role == (
        "content_multiset_comparison"
    )
    for layer_id in {84, 118, 142, 161, 171, 194, 246}:
        observed = inventory[layer_id]
        assert observed.safe_snapshot_method == "single_response"
        assert observed.identity_mode == "properties"
        assert observed.identity_properties_role == "diagnostic_only"
        assert observed.sort_by == ()
        assert observed.page_size is None
        assert observed.observed_response_cap is None
        projection = idecyl_wfs_snapshot_projection(
            idecyl_wfs_snapshot_evidence(observed)
        )
        assert projection is not None
        assert projection["identity_properties_role"] == (
            "diagnostic_only"
        )
        assert "observed_response_cap" not in projection


def test_only_layers_142_and_171_have_metadata_mismatches() -> None:
    inventory = idecyl_wfs_snapshot_inventory()
    mismatches = {
        item.audit_layer_id: (
            item.metadata_binding["expected_metadata_fid"],
            item.metadata_binding["published_metadata_fid"],
        )
        for item in inventory
        if item.metadata_binding["status"] == "mismatch"
    }

    assert mismatches == EXPECTED_METADATA_MISMATCHES
    assert all(
        item.metadata_binding["status"] in {"exact", "mismatch"}
        for item in inventory
    )


def test_projection_is_explicitly_non_authorizing_for_igcyl_nc() -> None:
    for observed in idecyl_wfs_snapshot_inventory():
        evidence = idecyl_wfs_snapshot_evidence(observed)
        projection = idecyl_wfs_snapshot_projection(evidence)

        assert projection is not None
        assert evidence["schema"] == EVIDENCE_SCHEMA
        assert evidence["license_gate"] == {
            "license_name": "LICENCIA-IGCYL-NC",
            "license_url": (
                "https://ftp.itacyl.es/cartografia/"
                "LICENCIA-IGCYL-NC-2012.pdf"
            ),
            "authorization_effect": (
                "none_without_persisted_human_mirror_review"
            ),
            "authorization_granted": False,
            "local_download_authorized": False,
            "local_service_authorized": False,
        }
        assert projection["authorization_granted"] is False
        assert projection["local_download_authorized"] is False
        assert projection["local_service_authorized"] is False


def test_known_identity_drift_fails_closed() -> None:
    observed = idecyl_wfs_snapshot_inventory()[0]

    exact = reviewed_idecyl_wfs_snapshot(
        audit_layer_id=observed.audit_layer_id,
        endpoint_url=observed.endpoint_url,
        type_name=observed.type_name,
        crs=observed.crs,
    )

    assert exact == observed
    assert exact is not observed
    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="identity changed",
    ):
        reviewed_idecyl_wfs_snapshot(
            audit_layer_id=observed.audit_layer_id,
            endpoint_url=observed.endpoint_url + "?changed=true",
            type_name=observed.type_name,
            crs=observed.crs,
        )
    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="identity changed",
    ):
        reviewed_idecyl_wfs_snapshot(
            audit_layer_id=999,
            endpoint_url=observed.endpoint_url,
            type_name=observed.type_name,
            crs=observed.crs,
        )
    assert (
        reviewed_idecyl_wfs_snapshot(
            audit_layer_id=999,
            endpoint_url=(
                "https://idecyl.jcyl.es/geoserver/unknown/wfs"
            ),
            type_name="unknown:unknown",
            crs="EPSG:25830",
        )
        is None
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("observation", "observed_feature_count"), 1),
        (("observation", "observed_response_cap"), 10_999),
        (("safe_snapshot", "page_size"), 10_999),
        (("safe_snapshot", "identity_properties_role"), "diagnostic_only"),
        (("license_gate", "authorization_granted"), True),
        (("license_gate", "local_download_authorized"), True),
        (("license_gate", "local_service_authorized"), True),
    ],
)
def test_projection_rejects_forged_evidence(
    path: tuple[str, str],
    value: Any,
) -> None:
    observed = next(
        item
        for item in idecyl_wfs_snapshot_inventory()
        if item.audit_layer_id == 243
    )
    evidence = idecyl_wfs_snapshot_evidence(observed)
    forged = deepcopy(evidence)
    forged[path[0]][path[1]] = value

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="no longer matches",
    ):
        idecyl_wfs_snapshot_projection(forged)

    assert idecyl_wfs_snapshot_projection(None) is None
    assert (
        idecyl_wfs_snapshot_projection(
            {"schema": "some-other-evidence/v1"}
        )
        is None
    )


def test_mutated_public_values_do_not_change_committed_evidence() -> None:
    observed = idecyl_wfs_snapshot_inventory()[0]
    observed.capabilities["paging_is_transaction_safe"] = True

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="no longer matches",
    ):
        idecyl_wfs_snapshot_evidence(observed)
    fresh = idecyl_wfs_snapshot_inventory()[0]
    assert fresh.capabilities["paging_is_transaction_safe"] is False

    changed = replace(fresh, observed_feature_count=1)
    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="no longer matches",
    ):
        idecyl_wfs_snapshot_evidence(changed)


def test_tampered_manifest_fails_digest_and_semantic_validation() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    classification = _resource_body(
        CLASSIFICATION_MANIFEST_RESOURCE
    )
    parsed = json.loads(manifest)
    source = next(
        item
        for item in parsed["sources"]
        if item["audit_layer_id"] == 243
    )
    source["observed_feature_count"] = 21_451
    tampered = _canonical_json(parsed)
    digest = hashlib.sha256(tampered).hexdigest()

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="failed its local digest",
    ):
        _load_evidence_package(
            tampered,
            legacy,
            classification,
            expected_manifest_sha256=MANIFEST_SHA256,
        )
    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="observation 243 changed",
    ):
        _load_evidence_package(
            tampered,
            legacy,
            classification,
            expected_manifest_sha256=digest,
        )


def test_capabilities_digest_must_match_bound_v2_workspace() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    classification = _resource_body(
        CLASSIFICATION_MANIFEST_RESOURCE
    )
    parsed = json.loads(manifest)
    source = next(
        item
        for item in parsed["sources"]
        if item["audit_layer_id"] == 78
    )
    source["capabilities_sha256"] = "0" * 64
    tampered = _canonical_json(parsed)

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="observation 78 changed",
    ):
        _load_evidence_package(
            tampered,
            legacy,
            classification,
            expected_manifest_sha256=hashlib.sha256(
                tampered
            ).hexdigest(),
        )


def test_recomputed_digest_cannot_authorize_or_uncanonicalize_manifest() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    classification = _resource_body(
        CLASSIFICATION_MANIFEST_RESOURCE
    )
    parsed = json.loads(manifest)
    parsed["capture"]["authorization_granted"] = True
    authorized = _canonical_json(parsed)

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="capture changed",
    ):
        _load_evidence_package(
            authorized,
            legacy,
            classification,
            expected_manifest_sha256=hashlib.sha256(
                authorized
            ).hexdigest(),
        )

    noncanonical = manifest.replace(b"{\n", b"{  \n", 1)
    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="not canonical JSON",
    ):
        _load_evidence_package(
            noncanonical,
            legacy,
            classification,
            expected_manifest_sha256=hashlib.sha256(
                noncanonical
            ).hexdigest(),
        )


def test_tampered_legacy_identity_inventory_is_rejected() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    classification = _resource_body(
        CLASSIFICATION_MANIFEST_RESOURCE
    )
    tampered_legacy = legacy.replace(
        b"znie_cyl_vvpp_ejes",
        b"znie_cyl_vvpp_Xjes",
        1,
    )
    assert tampered_legacy != legacy

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="legacy identity inventory failed its local digest",
    ):
        _load_evidence_package(
            manifest,
            tampered_legacy,
            classification,
            expected_manifest_sha256=MANIFEST_SHA256,
        )


def test_tampered_v2_capability_binding_is_rejected() -> None:
    manifest = _resource_body(MANIFEST_RESOURCE)
    legacy = _resource_body(LEGACY_MANIFEST_RESOURCE)
    classification = _resource_body(
        CLASSIFICATION_MANIFEST_RESOURCE
    )
    tampered = classification.replace(
        b'"paging_is_transaction_safe": false',
        b'"paging_is_transaction_safe": true ',
        1,
    )
    assert tampered != classification

    with pytest.raises(
        IDECyLWFSSnapshotEvidenceError,
        match="classification manifest failed its local digest",
    ):
        _load_evidence_package(
            manifest,
            legacy,
            tampered,
            expected_manifest_sha256=MANIFEST_SHA256,
        )
