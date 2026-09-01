from dataclasses import replace
import hashlib
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from app.reference_layers import siur_delivery_import
from app.reference_layers.catalog import apply_catalog_definition
from app.reference_layers.delivery_evidence import (
    DeliveryEvidenceError,
    LicenseReviewError,
    apply_delivery_evidence,
    build_delivery_evidence_plan,
    parse_license_review,
)
from app.reference_layers.models import (
    ReferenceDeliveryAttestation,
    ReferenceLayer,
    ReferenceLicenseReview,
    ReferenceWMSCapabilitiesSnapshot,
)
from app.reference_layers.wms_capabilities import (
    MAX_WMS_CRS_PER_LAYER,
    WMSCapabilitiesError,
    canonical_capabilities_sha256,
    parse_wms_capabilities,
)
from test_reference_wms_proxy import make_wms_definition
from wms_evidence_fixtures import (
    make_capabilities_xml,
    make_license_review_document,
)


def seed_catalog_layer(db, **definition_options) -> ReferenceLayer:
    apply_catalog_definition(db, make_wms_definition(**definition_options))
    return db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.source_key == "layer:classification"
        )
    )


def parsed_evidence(layer, **capabilities_options):
    capabilities = parse_wms_capabilities(
        make_capabilities_xml(**capabilities_options)
    )
    review = parse_license_review(
        make_license_review_document(layer.service.source_key)
    )
    return capabilities, review


def test_capabilities_parser_preserves_exact_bounded_delivery_manifest() -> None:
    document = make_capabilities_xml()

    evidence = parse_wms_capabilities(document)

    assert evidence.raw_xml == document
    assert evidence.raw_sha256 == hashlib.sha256(document).hexdigest()
    assert evidence.version == "1.3.0"
    assert evidence.get_map_endpoint == (
        "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
    )
    assert evidence.get_legend_endpoint == evidence.get_map_endpoint
    assert evidence.get_feature_info_endpoint == evidence.get_map_endpoint
    assert evidence.get_map_formats == ("image/png",)
    assert evidence.get_legend_formats == ("image/png",)
    assert evidence.get_feature_info_formats == ("application/json",)
    assert len(evidence.layers) == 1
    layer = evidence.layers[0]
    assert layer.name == "plau_cyl_clasificacion"
    assert layer.crs == ("EPSG:25830", "EPSG:3857")
    assert layer.queryable is True
    assert layer.styles == (
        "",
        "plau_cyl_clasificacion_color",
        "plau_cyl_clasificacion_trama",
    )
    assert canonical_capabilities_sha256(evidence.normalized()) == (
        evidence.normalized_sha256
    )


@pytest.mark.parametrize("version", ["1.1.1", "1.3.0"])
def test_capabilities_parser_supports_only_the_two_attested_versions(
    version,
) -> None:
    evidence = parse_wms_capabilities(make_capabilities_xml(version=version))
    assert evidence.version == version


def test_capabilities_parser_drops_the_geoserver_service_selector() -> None:
    # Every real IDECyL workspace advertises its OnlineResource with the
    # service already selected, so the prefix has to be tolerated and stripped.
    # The fixture already ends every href with "?", so the selector replaces
    # it. "&amp;" is how the ampersand reaches the XML; the parser sees "&".
    document = make_capabilities_xml(
        endpoint="https://idecyl.jcyl.es/geoserver/urbanismo/ows"
    )
    for selector in (b"?SERVICE=WMS&amp;", b"?service=wms", b"?"):
        evidence = parse_wms_capabilities(
            document.replace(b'/ows?"', b"/ows" + selector + b'"')
        )
        assert (
            evidence.get_map_endpoint
            == "https://idecyl.jcyl.es/geoserver/urbanismo/ows"
        )

    for rejected in (
        b"?token=secret",
        b"?SERVICE=WFS",
        b"?SERVICE=WMS&amp;bbox=0",
    ):
        with pytest.raises(WMSCapabilitiesError):
            parse_wms_capabilities(
                document.replace(b'/ows?"', b"/ows" + rejected + b'"')
            )


def test_capabilities_parser_rejects_unsafe_xml_namespaces_and_endpoints() -> None:
    document = make_capabilities_xml()
    doctype = document.replace(
        b"<WMS_Capabilities",
        b'<!DOCTYPE WMS_Capabilities [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<WMS_Capabilities",
        1,
    )
    wrong_namespace = document.replace(
        b'xmlns="http://www.opengis.net/wms"',
        b'xmlns="https://attacker.example/wms"',
        1,
    )
    mixed_namespace = document.replace(
        b"<Capability>",
        b'<evil:Capability xmlns:evil="https://attacker.example/wms">',
        1,
    ).replace(b"</Capability>", b"</evil:Capability>", 1)
    unsafe_endpoint = document.replace(
        b"https://idecyl.jcyl.es/",
        b"https://attacker.example/",
    )

    for value in (doctype, wrong_namespace, mixed_namespace, unsafe_endpoint):
        with pytest.raises(WMSCapabilitiesError):
            parse_wms_capabilities(value)


def test_capabilities_parser_rejects_ambiguous_endpoints_and_layer_bounds() -> None:
    document = make_capabilities_xml()
    second_endpoint = (
        b"<DCPType><HTTP><Get><OnlineResource "
        b'xmlns:xlink="http://www.w3.org/1999/xlink" '
        b'xlink:href="https://idecyl.jcyl.es/geoserver/otro/wms?"/>'
        b"</Get></HTTP></DCPType>"
    )
    ambiguous = document.replace(
        b"</GetMap>",
        second_endpoint + b"</GetMap>",
        1,
    )
    excessive_crs = make_capabilities_xml(
        crs=tuple(f"EPSG:{index}" for index in range(MAX_WMS_CRS_PER_LAYER + 1))
    )
    duplicate = document.replace(
        b"</Layer>\n    </Layer>",
        b"</Layer><Layer><Name>plau_cyl_clasificacion</Name></Layer>\n"
        b"    </Layer>",
        1,
    )

    for value in (ambiguous, excessive_crs, duplicate):
        with pytest.raises(WMSCapabilitiesError):
            parse_wms_capabilities(value)


def test_capabilities_parser_requires_exact_http_get_binding_structure() -> None:
    document = make_capabilities_xml()
    misplaced = document.replace(
        b"<DCPType><HTTP><Get>",
        b"<VendorBinding><Get>",
        1,
    ).replace(
        b"</Get></HTTP></DCPType>",
        b"</Get></VendorBinding>",
        1,
    )
    post_only = document.replace(b"<Get>", b"<Post>", 1).replace(
        b"</Get>",
        b"</Post>",
        1,
    )
    plain_href = document.replace(b"xlink:href=", b"href=", 1)

    for value in (misplaced, post_only, plain_href):
        with pytest.raises(WMSCapabilitiesError):
            parse_wms_capabilities(value)


def test_license_review_is_strict_human_evidence_not_automatic_approval() -> None:
    document = make_license_review_document("service:wms:idecyl:urbanismo")
    review = parse_license_review(document)

    assert review.raw_document == document
    assert review.evidence_sha256 == hashlib.sha256(document).hexdigest()
    assert review.decision == "approved"
    assert review.allow_proxy is True
    assert review.allow_cache is True
    assert "not an approval of the real SIUR license" in review.license_terms

    duplicate = document.replace(
        b'"allow_cache":true',
        b'"allow_cache":true,"allow_cache":false',
    )
    with pytest.raises(LicenseReviewError, match="strict"):
        parse_license_review(duplicate)

    invalid_permissions = json.loads(document)
    invalid_permissions.update(
        decision="restricted",
        allow_proxy=True,
        allow_cache=False,
    )
    with pytest.raises(LicenseReviewError, match="approved"):
        parse_license_review(json.dumps(invalid_permissions).encode())


def test_import_plan_is_hash_bound_transactional_and_idempotent(db) -> None:
    layer = seed_catalog_layer(
        db,
        license_status="pending",
        supported_crs=("EPSG:25830",),
    )
    capabilities, review = parsed_evidence(layer)
    plan = build_delivery_evidence_plan(db, capabilities, review)

    assert plan.fatal_issues == ()
    assert plan.attestation_issues == ()
    assert plan.delivery_issues == ()
    assert plan.attestable is True
    assert plan.delivery_ready is True
    assert plan.attestation_sha256
    first, applied_plan = apply_delivery_evidence(
        db,
        capabilities,
        review,
        expected_plan_sha256=plan.plan_sha256,
    )

    assert applied_plan.plan_sha256 == plan.plan_sha256
    assert first.attestation_id is not None
    capability_row = db.get(
        ReferenceWMSCapabilitiesSnapshot,
        first.capabilities_snapshot_id,
    )
    review_row = db.get(ReferenceLicenseReview, first.license_review_id)
    assert capability_row.raw_xml == capabilities.raw_xml
    assert capability_row.raw_sha256 == capabilities.raw_sha256
    assert capability_row.normalized_sha256 == capabilities.normalized_sha256
    assert review_row.reviewed_document == review.raw_document
    assert review_row.evidence_sha256 == review.evidence_sha256
    assert layer.service.license_status == "pending"
    assert layer.supported_crs_json == ["EPSG:25830"]

    repeated_plan = build_delivery_evidence_plan(db, capabilities, review)
    assert repeated_plan.plan_sha256 == plan.plan_sha256
    second, _ = apply_delivery_evidence(
        db,
        capabilities,
        review,
        expected_plan_sha256=repeated_plan.plan_sha256,
    )
    assert second == first
    assert db.scalar(select(func.count(ReferenceWMSCapabilitiesSnapshot.id))) == 1
    assert db.scalar(select(func.count(ReferenceLicenseReview.id))) == 1
    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 1


def test_immutable_delivery_evidence_rejects_updates_and_deletes(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, review = parsed_evidence(layer)
    plan = build_delivery_evidence_plan(db, capabilities, review)
    result, _ = apply_delivery_evidence(
        db,
        capabilities,
        review,
        expected_plan_sha256=plan.plan_sha256,
    )
    assert result.attestation_id is not None

    statements = (
        update(ReferenceWMSCapabilitiesSnapshot)
        .where(ReferenceWMSCapabilitiesSnapshot.id == result.capabilities_snapshot_id)
        .values(wms_version="1.1.1"),
        delete(ReferenceWMSCapabilitiesSnapshot).where(
            ReferenceWMSCapabilitiesSnapshot.id == result.capabilities_snapshot_id
        ),
        update(ReferenceLicenseReview)
        .where(ReferenceLicenseReview.id == result.license_review_id)
        .values(reviewer="tampered"),
        delete(ReferenceLicenseReview).where(
            ReferenceLicenseReview.id == result.license_review_id
        ),
        update(ReferenceDeliveryAttestation)
        .where(ReferenceDeliveryAttestation.id == result.attestation_id)
        .values(attestation_sha256="0" * 64),
        delete(ReferenceDeliveryAttestation).where(
            ReferenceDeliveryAttestation.id == result.attestation_id
        ),
    )
    for statement in statements:
        with pytest.raises(DBAPIError, match="immutable"):
            with db.begin_nested():
                db.execute(statement)

    assert db.get(
        ReferenceWMSCapabilitiesSnapshot,
        result.capabilities_snapshot_id,
    ) is not None
    assert db.get(ReferenceLicenseReview, result.license_review_id) is not None
    assert db.get(ReferenceDeliveryAttestation, result.attestation_id) is not None


def test_corrupt_reused_capabilities_are_rejected_before_attestation(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, review = parsed_evidence(layer)
    corrupt_xml = b"<WMS_Capabilities/>"
    db.add(
        ReferenceWMSCapabilitiesSnapshot(
            provider_key="siur",
            service_id=layer.service_id,
            raw_xml=corrupt_xml,
            raw_size_bytes=len(corrupt_xml),
            raw_sha256=capabilities.raw_sha256,
            normalized_sha256=capabilities.normalized_sha256,
            normalization_version=capabilities.normalization_version,
            wms_version=capabilities.version,
            get_map_endpoint=capabilities.get_map_endpoint,
            get_legend_endpoint=capabilities.get_legend_endpoint,
            get_feature_info_endpoint=capabilities.get_feature_info_endpoint,
            get_map_formats_json=list(capabilities.get_map_formats),
            get_legend_formats_json=list(capabilities.get_legend_formats),
            get_feature_info_formats_json=list(
                capabilities.get_feature_info_formats
            ),
            layer_manifest_json=capabilities.layer_manifest,
        )
    )
    db.commit()

    plan = build_delivery_evidence_plan(db, capabilities, review)
    assert "Stored capabilities evidence is corrupt" in plan.fatal_issues


def test_corrupt_reused_license_review_is_rejected_before_attestation(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, review = parsed_evidence(layer)
    corrupt_document = b"{}"
    db.add(
        ReferenceLicenseReview(
            provider_key="siur",
            service_id=layer.service_id,
            reviewed_document=corrupt_document,
            document_size_bytes=len(corrupt_document),
            evidence_sha256=review.evidence_sha256,
            review_sha256=review.review_sha256,
            supersedes_review_sha256=None,
            decision=review.decision,
            reviewer=review.reviewer,
            reviewed_at=review.reviewed_at,
            license_name=review.license_name,
            license_url=review.license_url,
            license_terms=review.license_terms,
            allow_proxy=review.allow_proxy,
            allow_cache=review.allow_cache,
        )
    )
    db.commit()

    plan = build_delivery_evidence_plan(db, capabilities, review)
    assert "Stored license review evidence is corrupt" in plan.fatal_issues


def test_catalog_drift_invalidates_the_reviewed_plan_without_partial_rows(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, review = parsed_evidence(layer)
    reviewed_plan = build_delivery_evidence_plan(db, capabilities, review)

    changed = replace(
        make_wms_definition(),
        raw_catalog={"fixture": "changed-after-review"},
    )
    apply_catalog_definition(db, changed)

    with pytest.raises(DeliveryEvidenceError, match="state changed"):
        apply_delivery_evidence(
            db,
            capabilities,
            review,
            expected_plan_sha256=reviewed_plan.plan_sha256,
        )
    assert db.scalar(select(func.count(ReferenceWMSCapabilitiesSnapshot.id))) == 0
    assert db.scalar(select(func.count(ReferenceLicenseReview.id))) == 0
    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 0


def test_two_plans_from_one_predecessor_cannot_fork_the_attestation_chain(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities_a, review = parsed_evidence(layer)
    capabilities_b = parse_wms_capabilities(
        make_capabilities_xml().replace(b"Synthetic parent", b"Synthetic fork B")
    )
    plan_a = build_delivery_evidence_plan(db, capabilities_a, review)
    plan_b = build_delivery_evidence_plan(db, capabilities_b, review)

    apply_delivery_evidence(
        db,
        capabilities_a,
        review,
        expected_plan_sha256=plan_a.plan_sha256,
    )
    with pytest.raises(DeliveryEvidenceError, match="state changed"):
        apply_delivery_evidence(
            db,
            capabilities_b,
            review,
            expected_plan_sha256=plan_b.plan_sha256,
        )

    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 1
    assert db.scalar(select(func.count(ReferenceWMSCapabilitiesSnapshot.id))) == 1


def test_restricted_review_appends_an_explicit_revocation_attestation(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities = parse_wms_capabilities(make_capabilities_xml())
    review = parse_license_review(
        make_license_review_document(
            layer.service.source_key,
            decision="restricted",
            allow_proxy=False,
            allow_cache=False,
        )
    )
    plan = build_delivery_evidence_plan(db, capabilities, review)

    assert plan.attestable is True
    assert plan.delivery_ready is False
    assert plan.attestation_kind == "revocation"
    assert "Human license review is not approved" in plan.attestation_issues
    result, _ = apply_delivery_evidence(
        db,
        capabilities,
        review,
        expected_plan_sha256=plan.plan_sha256,
    )

    assert result.capabilities_snapshot_id
    assert result.license_review_id
    assert result.attestation_id is not None
    attestation = db.get(ReferenceDeliveryAttestation, result.attestation_id)
    assert attestation.attestation_kind == "revocation"
    assert attestation.sequence_number == 1
    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 1


def test_failed_capabilities_do_not_replace_current_delivery_authority(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, review = parsed_evidence(layer)
    initial_plan = build_delivery_evidence_plan(db, capabilities, review)
    initial, _ = apply_delivery_evidence(
        db,
        capabilities,
        review,
        expected_plan_sha256=initial_plan.plan_sha256,
    )

    incomplete = parse_wms_capabilities(
        make_capabilities_xml(
            endpoint="https://idecyl.jcyl.es/geoserver/otro/wms"
        )
    )
    failed_plan = build_delivery_evidence_plan(db, incomplete, review)
    assert failed_plan.attestable is False
    assert (
        "GetMap endpoint does not match the current catalog"
        in failed_plan.attestation_issues
    )
    stored, _ = apply_delivery_evidence(
        db,
        incomplete,
        review,
        expected_plan_sha256=failed_plan.plan_sha256,
    )

    assert stored.attestation_id is None
    assert db.scalar(select(func.count(ReferenceWMSCapabilitiesSnapshot.id))) == 2
    current = db.scalar(
        select(ReferenceDeliveryAttestation).order_by(
            ReferenceDeliveryAttestation.sequence_number.desc()
        )
    )
    assert current.id == initial.attestation_id


def test_superseding_review_with_bad_capabilities_revokes_until_corrected(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, approved = parsed_evidence(layer)
    initial_plan = build_delivery_evidence_plan(db, capabilities, approved)
    apply_delivery_evidence(
        db,
        capabilities,
        approved,
        expected_plan_sha256=initial_plan.plan_sha256,
    )
    no_cache = parse_license_review(
        make_license_review_document(
            layer.service.source_key,
            allow_cache=False,
            reviewer="Synthetic No-Cache Reviewer",
            reviewed_at="2026-07-17T13:30:00Z",
            supersedes_review_sha256=approved.review_sha256,
        )
    )
    mismatched = parse_wms_capabilities(
        make_capabilities_xml(
            endpoint="https://idecyl.jcyl.es/geoserver/otro/wms"
        )
    )

    revocation_plan = build_delivery_evidence_plan(db, mismatched, no_cache)
    assert revocation_plan.attestable is True
    assert revocation_plan.attestation_kind == "revocation"
    assert revocation_plan.sequence_number == 2
    revoked, _ = apply_delivery_evidence(
        db,
        mismatched,
        no_cache,
        expected_plan_sha256=revocation_plan.plan_sha256,
    )

    repeated_plan = build_delivery_evidence_plan(db, mismatched, no_cache)
    assert repeated_plan.plan_sha256 == revocation_plan.plan_sha256
    repeated, _ = apply_delivery_evidence(
        db,
        mismatched,
        no_cache,
        expected_plan_sha256=repeated_plan.plan_sha256,
    )
    assert repeated == revoked
    assert db.scalar(select(func.count(ReferenceLicenseReview.id))) == 2
    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 2

    corrected_plan = build_delivery_evidence_plan(db, capabilities, no_cache)
    assert corrected_plan.attestable is True
    assert corrected_plan.attestation_kind == "delivery"
    assert corrected_plan.sequence_number == 3
    corrected, _ = apply_delivery_evidence(
        db,
        capabilities,
        no_cache,
        expected_plan_sha256=corrected_plan.plan_sha256,
    )

    attestations = list(
        db.scalars(
            select(ReferenceDeliveryAttestation).order_by(
                ReferenceDeliveryAttestation.sequence_number
            )
        )
    )
    assert [item.attestation_kind for item in attestations] == [
        "delivery",
        "revocation",
        "delivery",
    ]
    assert corrected.attestation_id == attestations[-1].id
    corrected_review = db.get(
        ReferenceLicenseReview,
        corrected.license_review_id,
    )
    assert corrected_review.allow_cache is False


def test_attestation_chain_supports_explicit_capabilities_rollback(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities_a, review = parsed_evidence(layer)
    plan_a = build_delivery_evidence_plan(db, capabilities_a, review)
    first, _ = apply_delivery_evidence(
        db,
        capabilities_a,
        review,
        expected_plan_sha256=plan_a.plan_sha256,
    )
    capabilities_b = parse_wms_capabilities(
        make_capabilities_xml().replace(b"Synthetic parent", b"Synthetic parent B")
    )
    plan_b = build_delivery_evidence_plan(db, capabilities_b, review)
    second, _ = apply_delivery_evidence(
        db,
        capabilities_b,
        review,
        expected_plan_sha256=plan_b.plan_sha256,
    )
    rollback_plan = build_delivery_evidence_plan(db, capabilities_a, review)
    rollback, _ = apply_delivery_evidence(
        db,
        capabilities_a,
        review,
        expected_plan_sha256=rollback_plan.plan_sha256,
    )

    attestations = list(
        db.scalars(
            select(ReferenceDeliveryAttestation).order_by(
                ReferenceDeliveryAttestation.sequence_number
            )
        )
    )
    assert [item.sequence_number for item in attestations] == [1, 2, 3]
    assert attestations[1].previous_attestation_id == first.attestation_id
    assert attestations[2].previous_attestation_id == second.attestation_id
    assert rollback.capabilities_snapshot_id == first.capabilities_snapshot_id
    assert rollback.attestation_id != first.attestation_id
    assert len({item.attestation_sha256 for item in attestations}) == 3


def test_review_lineage_blocks_replay_after_an_explicit_revocation(db) -> None:
    layer = seed_catalog_layer(db)
    capabilities, approved = parsed_evidence(layer)
    approved_plan = build_delivery_evidence_plan(db, capabilities, approved)
    apply_delivery_evidence(
        db,
        capabilities,
        approved,
        expected_plan_sha256=approved_plan.plan_sha256,
    )
    restricted = parse_license_review(
        make_license_review_document(
            layer.service.source_key,
            decision="restricted",
            allow_proxy=False,
            allow_cache=False,
            reviewed_at="2026-07-17T13:30:00Z",
            supersedes_review_sha256=approved.review_sha256,
        )
    )
    restricted_plan = build_delivery_evidence_plan(
        db,
        capabilities,
        restricted,
    )
    revoked, _ = apply_delivery_evidence(
        db,
        capabilities,
        restricted,
        expected_plan_sha256=restricted_plan.plan_sha256,
    )
    assert db.get(
        ReferenceDeliveryAttestation,
        revoked.attestation_id,
    ).attestation_kind == "revocation"

    replay_plan = build_delivery_evidence_plan(db, capabilities, approved)
    assert replay_plan.fatal_issues
    with pytest.raises(DeliveryEvidenceError, match="supersede|later"):
        apply_delivery_evidence(
            db,
            capabilities,
            approved,
            expected_plan_sha256=replay_plan.plan_sha256,
        )

    restored = parse_license_review(
        make_license_review_document(
            layer.service.source_key,
            reviewer="Synthetic Restoration Reviewer",
            reviewed_at="2026-07-17T14:30:00Z",
            supersedes_review_sha256=restricted.review_sha256,
        )
    )
    restored_plan = build_delivery_evidence_plan(db, capabilities, restored)
    restored_result, _ = apply_delivery_evidence(
        db,
        capabilities,
        restored,
        expected_plan_sha256=restored_plan.plan_sha256,
    )
    restored_attestation = db.get(
        ReferenceDeliveryAttestation,
        restored_result.attestation_id,
    )
    assert restored_attestation.attestation_kind == "delivery"
    assert restored_attestation.sequence_number == 3


def test_local_cli_is_dry_run_hash_approved_and_never_accepts_urls(
    db,
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    layer = seed_catalog_layer(db)
    capabilities_path = tmp_path / "GetCapabilities.xml"
    review_path = tmp_path / "license-review.json"
    capabilities_path.write_bytes(make_capabilities_xml())
    review_path.write_bytes(make_license_review_document(layer.service.source_key))

    class SessionContext:
        def __enter__(self):
            return db

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(siur_delivery_import, "SessionLocal", SessionContext)
    base_args = [
        "--capabilities",
        str(capabilities_path),
        "--license-review",
        str(review_path),
    ]
    discovery_code = siur_delivery_import.main(base_args)
    discovery = json.loads(capsys.readouterr().out)
    assert discovery_code == 3
    assert discovery["mode"] == "dry-run"
    assert discovery["applied"] is False
    assert discovery["approval_issues"]
    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 0

    reviewed_args = base_args + [
        "--approved-capabilities-sha256",
        discovery["capabilities"]["raw_sha256"],
        "--approved-normalized-capabilities-sha256",
        discovery["capabilities"]["normalized_sha256"],
        "--approved-license-evidence-sha256",
        discovery["license_review"]["evidence_sha256"],
        "--approved-license-review-sha256",
        discovery["license_review"]["review_sha256"],
        "--approved-plan-sha256",
        discovery["plan_sha256"],
    ]
    assert siur_delivery_import.main(reviewed_args) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["mode"] == "dry-run"
    assert reviewed["delivery_ready"] is True

    assert siur_delivery_import.main(reviewed_args + ["--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["stored"]["attestation_id"]
    assert db.scalar(select(func.count(ReferenceDeliveryAttestation.id))) == 1

    with pytest.raises(SystemExit):
        siur_delivery_import._build_parser().parse_args(
            base_args + ["--url", "https://attacker.example/capabilities"]
        )


def test_local_import_reads_at_most_one_byte_past_the_size_limit(
    monkeypatch,
) -> None:
    class GrowingFile:
        requested_bytes: int | None = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def fileno(self) -> int:
            return 99

        def read(self, amount: int) -> bytes:
            self.requested_bytes = amount
            return b"x" * amount

    handle = GrowingFile()
    sizes = iter(
        (
            SimpleNamespace(st_mode=stat.S_IFREG, st_size=8),
            SimpleNamespace(st_mode=stat.S_IFREG, st_size=9),
        )
    )
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: handle)
    monkeypatch.setattr(siur_delivery_import.os, "fstat", lambda fd: next(sizes))

    with pytest.raises(
        siur_delivery_import.SiurDeliveryImportError,
        match="cambió",
    ):
        siur_delivery_import._read_bounded(Path("evidence.xml"), 8, "test")

    assert handle.requested_bytes == 9
