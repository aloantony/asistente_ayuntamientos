from __future__ import annotations

import pytest

from app.reference_layers.reviewed_style_evidence import (
    EVIDENCE_SCHEMA,
    reviewed_miteco_mvt_style_reference,
)


@pytest.mark.parametrize(
    (
        "profile",
        "fill_color",
        "outline_color",
        "local_sha256",
        "archive_payload_sha256",
    ),
    [
        (
            "miteco-flood-q10-ogc-api-features-v1",
            "#ff0000",
            "#c80000",
            "5966ace7d1012605f2bf68264f8533fc"
            "73a0a91d4bf08b9b3fba6245d1e77c07",
            "99f7a8c01015ae5cc3983c333e2e76ca"
            "30a20502c693fcd25ff6ca878fd99f5b",
        ),
        (
            "miteco-flood-q50-ogc-api-features-v1",
            "#df73ff",
            "#df41ff",
            "e7d8abd91b0432ede69d2ac2066842d3"
            "e961907ebed6884f629cf7c5d6dca193",
            "0e753d758c49eded5da62147e8d1329f"
            "29359d2864379fa87396ac07fd042868",
        ),
        (
            "miteco-flood-q100-ogc-api-features-v1",
            "#e8beff",
            "#b68cff",
            "fb4946a17b7bae54d90ef22212586c53"
            "3fdded5265e30fc901caf2f56d9d37fd",
            "5d971fcab264052457976e82d63c8e73"
            "92e04b259f0c9944f3271c541fccb592",
        ),
        (
            "miteco-flood-q500-ogc-api-features-v1",
            "#ff73df",
            "#ff32df",
            "cd1202d3ab3cb98efd688521134a53b72"
            "b95863a4a2a792bfc32f80aa07d5744",
            "dfce3a142a2b773762e1381a89a13dcb"
            "5523b4c9f35dd5d04d4d2138435bb28f",
        ),
        (
            "miteco-flood-zfp-ogc-api-features-v1",
            "#cccccc",
            "#e6e600",
            "19dab6437a2d650505333d440cf2cc299"
            "6ba055f257c48d0d31921947fc9af8d",
            "18ec64cf71afd41ab622365b0ca67e98f"
            "a54417903d1a616e3b868f8371b3e55",
        ),
    ],
)
def test_reviewed_miteco_style_is_local_hash_bound_and_reproducible(
    profile: str,
    fill_color: str,
    outline_color: str,
    local_sha256: str,
    archive_payload_sha256: str,
) -> None:
    reference = reviewed_miteco_mvt_style_reference(profile)

    assert reference is not None
    assert reference["schema"] == EVIDENCE_SCHEMA
    assert reference["source_kind"] == "archived-official-mvt-json"
    assert reference["fill_color"] == fill_color
    assert reference["outline_color"] == outline_color
    assert reference["local_evidence_sha256"] == local_sha256
    assert reference["archive_payload_sha256"] == archive_payload_sha256
    assert reference["archive_capture_url"].startswith(
        "https://web.archive.org/web/20260511"
    )
    assert reference["url"].startswith(
        "https://wmts.mapama.gob.es/sig/www/styles/mvt/"
    )


def test_reviewed_miteco_style_returns_fresh_projection() -> None:
    profile = "miteco-flood-q10-ogc-api-features-v1"
    first = reviewed_miteco_mvt_style_reference(profile)
    second = reviewed_miteco_mvt_style_reference(profile)

    assert first is not None
    assert second is not None
    assert first == second
    assert first is not second
    first["fill_color"] = "#000000"
    assert second["fill_color"] == "#ff0000"


def test_unknown_miteco_style_profile_has_no_reviewed_evidence() -> None:
    assert reviewed_miteco_mvt_style_reference("unknown") is None
