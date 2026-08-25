from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import func, select

from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.mirror_lifecycle import (
    apply_mirror_bootstrap_plan,
    build_mirror_bootstrap_plan,
)
from app.reference_layers.mirror_authorization import (
    MirrorAuthorizationDocumentError,
    parse_mirror_authorization,
)
import app.reference_layers.mirror_review_packet as review_packet
from app.reference_layers.mirror_review_packet import (
    MirrorReviewPacketError,
    build_mirror_review_packet,
    require_expected_mirror_review_packet,
)
from app.reference_layers.mirror_strategy import canonical_sha256
from app.reference_layers.models import (
    ReferenceLayerSource,
    ReferenceMirrorAuthorizationReview,
    ReferenceSyncRun,
)
from support_reference_mirror_authorization import authorize_mirror_source


NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)


def _reviewable_definition(
    provider_key: str,
) -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://example.test/catalog.json",
        raw_catalog={"revision": "review-packet-v1"},
        services=(
            ReferenceServiceDefinition(
                source_key="service:planning",
                title="Planning service",
                upstream_protocol="wms",
                base_url="https://example.test/geoserver/planning/wms",
                default_format="image/png",
                attribution="Catalog attribution is not an authorization",
                license_name="Catalog license hint",
                license_url="https://example.test/license",
                license_status="approved",
                cache_policy="mirror",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:zones",
                node_type="layer",
                title="Planning zones",
                service_key="service:planning",
                remote_name="planning:zones",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                image_format="image/png",
                bounds={
                    "west": -7.1,
                    "south": 40.0,
                    "east": -1.7,
                    "north": 43.3,
                },
                min_zoom=6,
                max_zoom=18,
                style_name="style:default",
                styles=(
                    ReferenceLayerStyleDefinition(
                        source_key="style:default",
                        title="Default",
                        remote_name="planning:default",
                        is_default=True,
                    ),
                ),
            ),
        ),
        retrieved_at=NOW,
    )


def _blocked_definition(provider_key: str) -> ReferenceCatalogDefinition:
    return ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://example.test/catalog.json",
        raw_catalog={"revision": "blocked-review-packet-v1"},
        services=(
            ReferenceServiceDefinition(
                source_key="service:catastro",
                title="Catastro WMS",
                upstream_protocol="wms",
                base_url=(
                    "https://ovc.catastro.meh.es/Cartografia/WMS/"
                    "ServidorWMS.aspx"
                ),
                default_format="image/png",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key="layer:siur:catastro:unsupported",
                node_type="layer",
                title="Unsupported bulk WMS layer",
                service_key="service:catastro",
                remote_name="NotTheReviewedCatastroLayer",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
                image_format="image/png",
            ),
        ),
        retrieved_at=NOW,
    )


def _seed_reconciled(db, definition: ReferenceCatalogDefinition) -> None:
    apply_catalog_definition(db, definition)
    plan = build_mirror_bootstrap_plan(
        db,
        provider_key=definition.provider_key,
    )
    apply_mirror_bootstrap_plan(db, plan)


def test_packet_is_deterministic_and_keeps_human_fields_undecided(
    db,
) -> None:
    provider_key = "review-packet-deterministic"
    _seed_reconciled(db, _reviewable_definition(provider_key))

    first = build_mirror_review_packet(db, provider_key=provider_key)
    second = build_mirror_review_packet(db, provider_key=provider_key)
    document = first.public_document()

    assert first.packet_sha256 == second.packet_sha256
    assert first.packet_sha256 == canonical_sha256(
        first.semantic_document
    )
    assert document["schema_version"] == (
        "siur-mirror-review-packet-v1"
    )
    assert document["applicable_as_authorization"] is False
    assert document["summary"] == {
        "leaf_layer_count": 1,
        "primary_source_count": 1,
        "fallback_source_count": 2,
        "blocked_layer_count": 0,
        "composition_layer_count": 0,
        "pending_primary_review_count": 1,
        "pending_fallback_review_count": 2,
    }
    assert document["safety"] == {
        "database_writes_performed": False,
        "network_performed": False,
        "authorization_created": False,
        "runs_enqueued": False,
        "downloads_started": False,
        "human_fields_are_undecided": True,
    }

    entries = (
        document["primary_sources"] + document["fallback_sources"]
    )
    for entry in entries:
        human = entry["human_review"]
        assert human["state"] == "undecided"
        assert human["decision"] is None
        assert human["reviewer"] is None
        assert human["reviewed_at"] is None
        assert human["allowed_origins"] is None
        assert set(human["license"].values()) == {None}
        assert set(human["permissions"].values()) == {None}
        assert entry["current_authorization"]["blocker"] == (
            "mirror_authorization_missing"
        )
        source = entry["source"]
        assert canonical_sha256(source["definition"]) == (
            source["source_definition_sha256"]
        )
        assert source["required_source_origins"] == [
            "https://example.test"
        ]
        assert entry["service"]["catalog_license"][
            "grants_mirror_permission"
        ] is False

    assert document["primary_sources"][0]["needed_for"] == (
        "initial_delivery"
    )
    assert all(
        item["needed_for"] == "automatic_fallback"
        for item in document["fallback_sources"]
    )
    assert not db.new
    assert not db.dirty
    assert not db.deleted
    assert db.scalar(
        select(func.count(ReferenceMirrorAuthorizationReview.id))
    ) == 0
    with pytest.raises(MirrorAuthorizationDocumentError):
        parse_mirror_authorization(
            json.dumps(document, ensure_ascii=False).encode()
        )


def test_packet_reports_existing_head_but_never_reuses_human_fields(
    db,
) -> None:
    provider_key = "review-packet-existing-head"
    _seed_reconciled(db, _reviewable_definition(provider_key))
    source = db.scalar(
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.is_primary.is_(True),
        )
    )
    assert source is not None
    review = authorize_mirror_source(db, source, reviewed_at=NOW)

    document = build_mirror_review_packet(
        db,
        provider_key=provider_key,
    ).public_document()

    [primary] = document["primary_sources"]
    assert primary["current_authorization"]["blocker"] is None
    assert primary["current_authorization"]["head"][
        "review_sha256"
    ] == review.review_sha256
    assert primary["current_authorization"][
        "required_supersedes_review_sha256"
    ] == review.review_sha256
    assert primary["human_review"]["reviewer"] is None
    assert primary["human_review"]["decision"] is None
    assert document["summary"]["pending_primary_review_count"] == 0


def test_packet_lists_technical_blocker_without_fabricating_review(
    db,
) -> None:
    provider_key = "review-packet-blocked"
    _seed_reconciled(db, _blocked_definition(provider_key))

    document = build_mirror_review_packet(
        db,
        provider_key=provider_key,
    ).public_document()

    assert document["summary"]["primary_source_count"] == 0
    assert document["summary"]["fallback_source_count"] == 0
    assert document["summary"]["blocked_layer_count"] == 1
    [blocked] = document["blocked_layers"]
    assert blocked["reason_code"] == "bulk_wms_prohibited"
    assert blocked["evidence"]["wms_tiles_eligible"] is False
    assert "human_review" not in blocked


def test_packet_detects_drift_between_its_two_observations(
    db,
    monkeypatch,
) -> None:
    provider_key = "review-packet-observed-drift"
    _seed_reconciled(db, _reviewable_definition(provider_key))
    original = review_packet._load_packet_state
    calls = 0

    def drifting_state(*args, **kwargs):
        nonlocal calls
        calls += 1
        state = original(*args, **kwargs)
        if calls == 2:
            return replace(
                state,
                fence={
                    **state.fence,
                    "strategy_generation": (
                        state.fence["strategy_generation"] + 1
                    ),
                },
            )
        return state

    monkeypatch.setattr(
        review_packet,
        "_load_packet_state",
        drifting_state,
    )

    with pytest.raises(MirrorReviewPacketError) as captured:
        build_mirror_review_packet(db, provider_key=provider_key)

    assert captured.value.code == "mirror_review_packet_drift"


def test_expected_packet_hash_fails_closed_after_source_drift(db) -> None:
    provider_key = "review-packet-source-drift"
    _seed_reconciled(db, _reviewable_definition(provider_key))
    packet = build_mirror_review_packet(db, provider_key=provider_key)
    same = require_expected_mirror_review_packet(
        db,
        provider_key=provider_key,
        expected_packet_sha256=packet.packet_sha256,
    )
    assert same.packet_sha256 == packet.packet_sha256

    source = db.scalar(
        select(ReferenceLayerSource)
        .where(
            ReferenceLayerSource.provider_key == provider_key,
            ReferenceLayerSource.is_primary.is_(True),
        )
    )
    assert source is not None
    source.priority += 1
    db.commit()

    with pytest.raises(MirrorReviewPacketError) as captured:
        require_expected_mirror_review_packet(
            db,
            provider_key=provider_key,
            expected_packet_sha256=packet.packet_sha256,
        )
    assert captured.value.code == "mirror_review_packet_not_ready"


def test_scheduler_timestamp_does_not_invalidate_review_packet(db) -> None:
    provider_key = "review-packet-scheduler-timestamp"
    _seed_reconciled(db, _reviewable_definition(provider_key))
    first = build_mirror_review_packet(db, provider_key=provider_key)
    sources = list(
        db.scalars(
            select(ReferenceLayerSource).where(
                ReferenceLayerSource.provider_key == provider_key
            )
        )
    )
    for source in sources:
        source.next_check_at += timedelta(days=1)
    db.commit()

    second = build_mirror_review_packet(db, provider_key=provider_key)

    assert second.packet_sha256 == first.packet_sha256


def test_cli_is_read_only_and_can_recheck_an_exact_hash(
    db,
    monkeypatch,
    capsys,
) -> None:
    provider_key = "review-packet-cli"
    _seed_reconciled(db, _reviewable_definition(provider_key))
    before_reviews = db.scalar(
        select(func.count(ReferenceMirrorAuthorizationReview.id))
    )
    before_runs = db.scalar(select(func.count(ReferenceSyncRun.id)))
    monkeypatch.setattr(review_packet, "register_all_models", lambda: None)
    monkeypatch.setattr(
        review_packet,
        "SessionLocal",
        lambda: nullcontext(db),
    )

    assert review_packet.main(
        ["--provider-key", provider_key]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["applicable_as_authorization"] is False

    assert review_packet.main(
        [
            "--provider-key",
            provider_key,
            "--expected-packet-sha256",
            first["packet_sha256"],
        ]
    ) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["packet_sha256"] == first["packet_sha256"]
    assert db.scalar(
        select(func.count(ReferenceMirrorAuthorizationReview.id))
    ) == before_reviews
    assert db.scalar(select(func.count(ReferenceSyncRun.id))) == before_runs
    assert not db.new
    assert not db.dirty
    assert not db.deleted

    assert review_packet.main(
        [
            "--provider-key",
            provider_key,
            "--expected-packet-sha256",
            "f" * 64,
        ]
    ) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["error_code"] == "mirror_review_packet_changed"
