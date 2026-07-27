from importlib.resources import files
import hashlib

import pytest

from app.reference_layers.reviewed_ortho_evidence import (
    CAPABILITIES_RESOURCE,
    CAPABILITIES_SHA256,
    CATALOG_CAPABILITIES_RESOURCE,
    CATALOG_CAPABILITIES_SHA256,
    CATALOG_ENDPOINT_URL,
    PROFILE_RESOURCE,
    PROFILE_SHA256,
    ReviewedOrthoEvidenceError,
    build_reviewed_ign_ortho_parity_gate,
    require_reviewed_ign_ortho_delivery_allowed,
    require_reviewed_ign_ortho_acquisition_allowed,
    reviewed_ign_ortho_catalog_capabilities_gate,
    reviewed_ign_ortho_equivalence,
    reviewed_ign_ortho_expected_source_definition,
    reviewed_ign_ortho_live_capabilities_gate,
    reviewed_ign_ortho_source_projection,
    reviewed_ign_ortho_substitution,
)
from app.reference_layers.source_discovery import SourceCandidate
from app.reference_layers.source_probes import probe_candidate_document


EXPECTED_MAPPINGS = {
    "Ortofoto_2023": (
        "PNOA2023",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_2021": (
        "PNOA2020",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_2020": ("PNOA2020", "exact", "full", True),
    "Ortofoto_2017": ("PNOA2017", "exact", "full", True),
    "Ortofoto_2014": ("PNOA2014", "exact", "full", True),
    "Ortofoto_2011": (
        "PNOA2011",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_2010": (
        "PNOA2010",
        "exact",
        "partial",
        True,
    ),
    "Ortofoto_2009": (
        "PNOA2009",
        "substitute_degraded",
        "partial",
        True,
    ),
    "Ortofoto_2008": (
        "PNOA2008",
        "substitute_degraded",
        "partial",
        True,
    ),
    "Ortofoto_2007": (
        "PNOA2007",
        "substitute_degraded",
        "partial",
        True,
    ),
    "Ortofoto_2006": (
        "PNOA2006",
        "exact",
        "partial",
        True,
    ),
    "Ortofoto_2005": (
        "PNOA2005",
        "exact",
        "partial",
        True,
    ),
    "Ortofoto_2004": (
        "PNOA2004",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_2002": (
        "SIGPAC",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_2001": (
        "SIGPAC",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_2000": (
        "SIGPAC",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_1999": (
        "SIGPAC",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_1997": (
        "SIGPAC",
        "substitute_degraded",
        "full",
        True,
    ),
    "Ortofoto_1973-83": (
        "Interministerial_1973-1986",
        "substitute_degraded",
        "unknown",
        True,
    ),
    "Ortofoto_1956": (
        "AMS_1956-1957",
        "substitute_degraded",
        "unknown",
        True,
    ),
}


def test_committed_profile_and_capabilities_are_hash_bound() -> None:
    package = files("app.reference_layers")
    capabilities = package.joinpath(CAPABILITIES_RESOURCE).read_bytes()
    catalog_capabilities = package.joinpath(
        CATALOG_CAPABILITIES_RESOURCE
    ).read_bytes()
    profile = package.joinpath(PROFILE_RESOURCE).read_bytes()

    assert hashlib.sha256(capabilities).hexdigest() == CAPABILITIES_SHA256
    assert hashlib.sha256(catalog_capabilities).hexdigest() == (
        CATALOG_CAPABILITIES_SHA256
    )
    assert hashlib.sha256(profile).hexdigest() == PROFILE_SHA256


def test_reviewed_profile_maps_exactly_twenty_layers_and_probes_snapshot() -> None:
    capabilities = (
        files("app.reference_layers")
        .joinpath(CAPABILITIES_RESOURCE)
        .read_bytes()
    )
    statuses = []
    source_keys = set()
    for catalog_layer, (
        selected_layer,
        status,
        declared_coverage,
        promotion_eligible,
    ) in EXPECTED_MAPPINGS.items():
        reviewed = reviewed_ign_ortho_substitution(
            CATALOG_ENDPOINT_URL,
            catalog_layer,
        )

        assert reviewed is not None
        assert reviewed.selected_layer == selected_layer
        assert reviewed.equivalence_status == status
        assert reviewed.declared_coverage == declared_coverage
        assert reviewed.promotion_eligible is promotion_eligible
        statuses.append(status)
        candidate = SourceCandidate(
            protocol="wms_tiles",
            target_kind="tiles",
            endpoint_url=reviewed.selected_endpoint_url,
            remote_name=reviewed.selected_layer,
            sync_strategy="tile_seed",
            priority=5,
            config={
                "format": reviewed.image_format,
                "reviewed_equivalence": reviewed_ign_ortho_equivalence(
                    reviewed
                ),
            },
            source_key=f"auto:test:{catalog_layer}",
            definition_sha256="a" * 64,
        )
        probe = probe_candidate_document(candidate, capabilities)
        assert probe.available is True
        assert probe.canonical_name == selected_layer
        assert "EPSG:3857" in probe.metadata["crs"]
        assert "image/jpeg" in probe.metadata["formats"]
        source_keys.add(
            (
                candidate.endpoint_url,
                candidate.remote_name,
                reviewed.profile,
            )
        )

    assert statuses.count("exact") == 6
    assert statuses.count("substitute_degraded") == 14
    assert statuses.count("blocked") == 0
    assert len(source_keys) == 20


def test_degraded_projection_is_bound_and_remains_acquisition_eligible() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2002",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    projection = reviewed_ign_ortho_source_projection(definition)

    assert projection is not None
    assert projection["equivalence_status"] == "substitute_degraded"
    assert projection["promotion_eligible"] is True
    assert "1997-2003" in projection["public_notice"]
    assert not any("url" in key for key in projection)
    with pytest.raises(ReviewedOrthoEvidenceError):
        reviewed_ign_ortho_source_projection(
            {**definition, "remote_name": "PNOA2002"}
        )
    forged_equivalence = {
        **definition["config"]["reviewed_equivalence"],
        "equivalence_status": "exact",
    }
    with pytest.raises(ReviewedOrthoEvidenceError):
        reviewed_ign_ortho_source_projection(
            {
                **definition,
                "config": {
                    **definition["config"],
                    "reviewed_equivalence": forged_equivalence,
                },
            }
        )
    assert require_reviewed_ign_ortho_acquisition_allowed(definition) == (
        projection
    )


def test_controlled_ign_source_cannot_omit_or_disguise_reviewed_evidence() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2002",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    config_without_evidence = dict(definition["config"])
    config_without_evidence.pop("reviewed_equivalence")
    with pytest.raises(
        ReviewedOrthoEvidenceError,
        match="missing committed evidence",
    ):
        require_reviewed_ign_ortho_acquisition_allowed(
            {**definition, "config": config_without_evidence}
        )

    with pytest.raises(
        ReviewedOrthoEvidenceError,
        match="missing committed evidence",
    ):
        require_reviewed_ign_ortho_acquisition_allowed(
            {
                **definition,
                "config": {
                    **definition["config"],
                    "reviewed_equivalence": {
                        "schema": "unreviewed-substitution/v1"
                    },
                },
            }
        )


def test_full_product_year_mapping_is_bound_to_complete_promotable_source() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2020",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    projection = require_reviewed_ign_ortho_acquisition_allowed(definition)

    assert projection is not None
    assert projection["declared_coverage"] == "full"
    assert projection["declared_resolutions_metres"] == [0.15, 0.25]
    assert projection["catalog_declared_resolutions_metres"] == [0.25]
    assert projection["comparison_basis"] == (
        "catalog_and_selected_capabilities_match"
    )
    assert projection["promotion_eligible"] is True
    with pytest.raises(ReviewedOrthoEvidenceError):
        reviewed_ign_ortho_source_projection(
            {
                **definition,
                "config": {**definition["config"], "max_zoom": 16},
            }
        )


def test_2021_uses_visible_pnoa2020_degraded_mapping() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2021",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    projection = require_reviewed_ign_ortho_acquisition_allowed(definition)

    assert projection is not None
    assert projection["selected_layer"] == "PNOA2020"
    assert projection["equivalence_status"] == "substitute_degraded"
    assert projection["promotion_eligible"] is True
    assert "anualidad distinta" in projection["public_notice"]
    assert projection["parity_policy"]["promotion"][
        "immutable_degraded_classification"
    ] is True


def test_both_capabilities_drive_quadrant_classification() -> None:
    exact = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2010",
    )
    mismatch = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2008",
    )
    ambiguous = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2007",
    )
    assert exact is not None and mismatch is not None and ambiguous is not None

    assert "Cuadrante NW" in exact.catalog_abstract
    assert "NW de Castilla y León" in exact.selected_abstract
    assert exact.catalog_declared_resolutions_metres == (0.25, 0.5)
    assert exact.declared_resolutions_metres == (0.25, 0.5)
    assert exact.equivalence_status == "exact"
    assert "Cuadrante NE" in mismatch.catalog_abstract
    assert "NW de Castilla y León" in mismatch.selected_abstract
    assert mismatch.equivalence_status == "substitute_degraded"
    assert "Cuadrante NE" in ambiguous.catalog_abstract
    assert "Norte de Castilla y León" in ambiguous.selected_abstract
    assert ambiguous.equivalence_status == "substitute_degraded"


def test_live_capabilities_are_compared_semantically_not_only_by_hash() -> None:
    package = files("app.reference_layers")
    body = package.joinpath(CAPABILITIES_RESOURCE).read_bytes()
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2020",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    gate = reviewed_ign_ortho_live_capabilities_gate(
        definition,
        body,
        phase="pre_download",
    )

    assert gate is not None
    assert gate["passed"] is True
    assert gate["live_sha256"] == CAPABILITIES_SHA256
    changed = body.replace(
        b"Ortoimagen PNOA correspondiente al a\xc3\xb1o 2020.",
        b"Ortoimagen PNOA correspondiente al a\xc3\xb1o 2020 cambiada.",
        1,
    )
    with pytest.raises(
        ReviewedOrthoEvidenceError,
        match="semantics changed",
    ):
        reviewed_ign_ortho_live_capabilities_gate(
            definition,
            changed,
            phase="pre_download",
        )


def test_exact_parity_gate_is_structured_hash_bound_and_executable() -> None:
    package = files("app.reference_layers")
    selected_body = package.joinpath(CAPABILITIES_RESOURCE).read_bytes()
    catalog_body = package.joinpath(
        CATALOG_CAPABILITIES_RESOURCE
    ).read_bytes()
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2020",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)
    selected_gate = reviewed_ign_ortho_live_capabilities_gate(
        definition,
        selected_body,
        phase="pre_promotion",
    )
    catalog_gate = reviewed_ign_ortho_catalog_capabilities_gate(
        definition,
        catalog_body,
    )
    content_sha256 = "a" * 64
    measurements = {
        "schema_version": "siur-reviewed-ortho-parity-measurements/v1",
        "sample_count": 12,
        "coordinate_sha256": "1" * 64,
        "selected_sample_sha256": "2" * 64,
        "local_sample_sha256": "2" * 64,
        "catalog_sample_sha256": "3" * 64,
        "coverage_mask": {
            "selected_nonempty_samples": 12,
            "catalog_nonempty_samples": 12,
            "minimum_mask_iou": 0.98,
        },
        "resolution_scale": {
            "tile_width": 256,
            "tile_height": 256,
            "crs": "EPSG:3857",
            "sampled_zooms": [12, 14, 15],
            "maximum_resolution_delta_metres_per_pixel": 0.0,
            "maximum_scale_denominator_delta": 0.0,
        },
        "pixel_samples": {
            "normalized_mean_absolute_error": 0.02,
        },
    }

    gate = build_reviewed_ign_ortho_parity_gate(
        source_definition=definition,
        content_sha256=content_sha256,
        selected_capabilities=selected_gate,
        catalog_capabilities=catalog_gate,
        measurements=measurements,
    )
    stale_selected_gate = reviewed_ign_ortho_live_capabilities_gate(
        definition,
        selected_body,
        phase="pre_download",
    )
    with pytest.raises(ReviewedOrthoEvidenceError):
        build_reviewed_ign_ortho_parity_gate(
            source_definition=definition,
            content_sha256=content_sha256,
            selected_capabilities=stale_selected_gate,
            catalog_capabilities=catalog_gate,
            measurements=measurements,
        )

    assert gate is not None
    projection = require_reviewed_ign_ortho_delivery_allowed(
        catalog_endpoint_url=CATALOG_ENDPOINT_URL,
        catalog_layer="Ortofoto_2020",
        source_definition=definition,
        validation_json={"reviewed_ortho_parity_gate": gate},
        content_sha256=content_sha256,
    )
    assert projection is not None
    assert projection["equivalence_status"] == "exact"
    assert projection["required_attribution"] == (
        "Obra derivada de PNOA 2020 CC-BY 4.0 scne.es"
    )
    with pytest.raises(
        ReviewedOrthoEvidenceError,
        match="catalog identity",
    ):
        require_reviewed_ign_ortho_delivery_allowed(
            catalog_endpoint_url="https://changed.example.test/wms",
            catalog_layer="Ortofoto_2020",
            source_definition=definition,
            validation_json={"reviewed_ortho_parity_gate": gate},
            content_sha256=content_sha256,
        )
    forged = {**gate, "classification": "substitute_degraded"}
    with pytest.raises(ReviewedOrthoEvidenceError):
        require_reviewed_ign_ortho_delivery_allowed(
            catalog_endpoint_url=CATALOG_ENDPOINT_URL,
            catalog_layer="Ortofoto_2020",
            source_definition=definition,
            validation_json={"reviewed_ortho_parity_gate": forged},
            content_sha256=content_sha256,
        )
    with pytest.raises(ReviewedOrthoEvidenceError):
        require_reviewed_ign_ortho_delivery_allowed(
            catalog_endpoint_url=CATALOG_ENDPOINT_URL,
            catalog_layer="Ortofoto_2020",
            source_definition=definition,
            validation_json={},
            content_sha256=content_sha256,
        )


def test_2021_delivery_still_requires_executable_parity_evidence() -> None:
    reviewed = reviewed_ign_ortho_substitution(
        CATALOG_ENDPOINT_URL,
        "Ortofoto_2021",
    )
    assert reviewed is not None
    definition = reviewed_ign_ortho_expected_source_definition(reviewed)

    with pytest.raises(
        ReviewedOrthoEvidenceError,
        match="no executable parity gate",
    ):
        require_reviewed_ign_ortho_delivery_allowed(
            catalog_endpoint_url=CATALOG_ENDPOINT_URL,
            catalog_layer="Ortofoto_2021",
            source_definition=definition,
            validation_json={"passed": True},
            content_sha256="a" * 64,
        )


def test_attribution_and_parity_policy_are_product_specific() -> None:
    expectations = {
        "Ortofoto_2023": (
            "Obra derivada de PNOA 2023 CC-BY 4.0 scne.es"
        ),
        "Ortofoto_2002": (
            "Obra derivada de Orto-SIGPAC 1997-2003 "
            "CC-BY 4.0 scne.es"
        ),
        "Ortofoto_1973-83": (
            "Obra derivada de Orto-Interministerial 1976-1986 "
            "CC-BY 4.0 scne.es"
        ),
        "Ortofoto_1956": (
            "Obra derivada de Orto-AMS 1956-1957 "
            "CC-BY 4.0 ejercito.defensa.gob.es"
        ),
    }
    for catalog_layer, attribution in expectations.items():
        reviewed = reviewed_ign_ortho_substitution(
            CATALOG_ENDPOINT_URL,
            catalog_layer,
        )
        assert reviewed is not None
        projection = reviewed_ign_ortho_source_projection(
            reviewed_ign_ortho_expected_source_definition(reviewed)
        )
        assert projection is not None
        assert projection["required_attribution"] == attribution
        assert projection["parity_policy"]["schema_version"] == (
            "siur-reviewed-ortho-parity-policy/v1"
        )
        assert "parity_requirements" not in projection
