from __future__ import annotations

import json
import warnings
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Session

import app.reference_layers.zip_metadata_probe_source as probe_source_admin
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceLayerDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
)
from app.reference_layers.delivery_builder import canonical_json_sha256
from app.reference_layers.idecyl_exact_evidence import (
    idecyl_exact_source_inventory,
)
from app.reference_layers.models import (
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerSource,
    ReferenceService,
)
from app.reference_layers.zip_metadata_probe import (
    PROBE_SOURCE_CONFIG_SCHEMA,
    build_sigpac_zip_metadata_probe_plan,
    metadata_probe_source_definition,
)
from app.reference_layers.zip_metadata_probe_source import (
    MetadataProbeSourceError,
    apply_metadata_probe_source,
    plan_metadata_probe_source,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 27, 18, tzinfo=timezone.utc)


def _reviewed(audit_layer_id: int):
    return next(
        item
        for item in idecyl_exact_source_inventory()
        if item.audit_layer_id == audit_layer_id
    )


def _indexes(audit_layer_id: int) -> tuple[bytes, bytes]:
    year = "2024" if audit_layer_id == 123 else "2022"
    return (
        (FIXTURES / f"sigpac-{year}-root.html").read_bytes(),
        (FIXTURES / f"sigpac-{year}-provinces.html").read_bytes(),
    )


def _probe_source_count(db) -> int:
    return db.scalar(
        select(func.count())
        .select_from(ReferenceLayerSource)
        .where(ReferenceLayerSource.source_key.like("probe:%"))
    )


def _seed_catalog(db, *, audit_layer_id: int, provider_key: str = "siur"):
    reviewed = _reviewed(audit_layer_id)
    definition = ReferenceCatalogDefinition(
        provider_key=provider_key,
        source_url="https://catalog.example.test/siur.json",
        raw_catalog={"revision": 1},
        services=(
            ReferenceServiceDefinition(
                source_key="service",
                title="IDECyL",
                upstream_protocol="wms",
                base_url=reviewed.catalog_endpoint_url,
                attribution=None,
                license_status="pending",
            ),
        ),
        layers=(
            ReferenceLayerDefinition(
                source_key=reviewed.catalog_layer_source_key,
                node_type="layer",
                title=f"SIGPAC {audit_layer_id}",
                service_key="service",
                remote_name=reviewed.catalog_remote_name,
                role="overlay",
                renderer="raster_tile",
                delivery_mode="mirror",
            ),
        ),
        retrieved_at=NOW,
    )
    apply_catalog_definition(db, definition)
    return db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == provider_key,
            ReferenceLayer.node_type == "layer",
        )
    )


@pytest.mark.parametrize("audit_layer_id", [123, 166])
def test_plan_binds_current_catalog_to_disabled_exact_source(
    db,
    audit_layer_id,
) -> None:
    layer = _seed_catalog(db, audit_layer_id=audit_layer_id)
    root_index, province_index = _indexes(audit_layer_id)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        plan = plan_metadata_probe_source(
            db,
            audit_layer_id=audit_layer_id,
            root_index=root_index,
            province_index=province_index,
        )

    assert plan.action == "create"
    assert plan.layer_id == layer.id
    assert plan.layer_source_key == layer.source_key
    assert plan.source_key == f"probe:{plan.probe_plan_sha256[:32]}"
    assert plan.source_definition["sync_strategy"] == "manual"
    assert plan.source_definition["config"]["metadata_probe_only"] is True
    assert plan.source_state == {
        "enabled": False,
        "is_primary": False,
        "source_format": None,
        "check_interval_seconds": 86_400,
        "full_refresh_interval_seconds": 2_592_000,
    }
    assert plan.source_definition_sha256 == canonical_json_sha256(
        plan.source_definition
    )
    assert len(plan.plan_sha256) == 64
    changed_state = {
        **plan.source_state,
        "enabled": True,
    }
    assert replace(plan, source_state=changed_state).plan_sha256 != plan.plan_sha256
    assert _probe_source_count(db) == 0


def test_apply_requires_exact_plan_and_is_idempotent(db) -> None:
    _seed_catalog(db, audit_layer_id=123)
    root_index, province_index = _indexes(123)
    plan = plan_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
    )

    with pytest.raises(MetadataProbeSourceError, match="plan changed") as error:
        apply_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
            expected_plan_sha256="0" * 64,
        )
    assert error.value.code == "plan_changed"
    assert _probe_source_count(db) == 0

    applied, source = apply_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
        expected_plan_sha256=plan.plan_sha256,
    )
    db.commit()

    assert applied.action == "create"
    assert source.enabled is False
    assert source.is_primary is False
    assert source.sync_strategy == "manual"
    assert source.source_format is None
    assert source.definition_sha256 == applied.source_definition_sha256
    assert source.config_json["schema"] == PROBE_SOURCE_CONFIG_SCHEMA

    repeated, same_source = apply_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
        expected_plan_sha256=plan.plan_sha256,
    )

    assert repeated.action == "already_exists"
    assert repeated.plan_sha256 == plan.plan_sha256
    assert same_source.id == source.id
    assert _probe_source_count(db) == 1


def test_existing_probe_source_is_never_repaired_in_place(db) -> None:
    _seed_catalog(db, audit_layer_id=123)
    root_index, province_index = _indexes(123)
    plan = plan_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
    )
    _, source = apply_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
        expected_plan_sha256=plan.plan_sha256,
    )
    source.enabled = True
    db.commit()

    with pytest.raises(
        MetadataProbeSourceError,
        match="differs from the reviewed plan",
    ) as error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
        )

    assert error.value.code == "source_changed"
    assert source.enabled is True


def test_existing_probe_operational_intervals_are_hash_bound(db) -> None:
    _seed_catalog(db, audit_layer_id=123)
    root_index, province_index = _indexes(123)
    plan = plan_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
    )
    _, source = apply_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
        expected_plan_sha256=plan.plan_sha256,
    )
    source.check_interval_seconds = 900
    db.commit()

    with pytest.raises(MetadataProbeSourceError) as error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
        )

    assert error.value.code == "source_changed"


def test_stale_probe_source_blocks_append_instead_of_being_replaced(
    db,
) -> None:
    layer = _seed_catalog(db, audit_layer_id=123)
    root_index, province_index = _indexes(123)
    reviewed = _reviewed(123)
    probe_plan = build_sigpac_zip_metadata_probe_plan(
        reviewed,
        root_index=root_index,
        province_index=province_index,
    )
    definition = metadata_probe_source_definition(probe_plan)
    stale = ReferenceLayerSource(
        provider_key="siur",
        layer_id=layer.id,
        source_key="probe:obsolete",
        protocol=definition["protocol"],
        target_kind=definition["target_kind"],
        endpoint_url=definition["endpoint_url"],
        remote_name=definition["remote_name"],
        sync_strategy=definition["sync_strategy"],
        config_json=definition["config"],
        definition_sha256=canonical_json_sha256(definition),
        enabled=False,
        is_primary=False,
        priority=definition["priority"],
    )
    db.add(stale)
    db.commit()

    with pytest.raises(
        MetadataProbeSourceError,
        match="different metadata-probe source",
    ) as error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
        )

    assert error.value.code == "stale_probe_source_exists"
    assert _probe_source_count(db) == 1


def test_insert_collision_rolls_back_savepoint_and_keeps_session_usable(
    db,
) -> None:
    layer = _seed_catalog(db, audit_layer_id=123)
    root_index, province_index = _indexes(123)
    plan = plan_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
    )
    collision = ReferenceLayerSource(
        provider_key="siur",
        layer_id=layer.id,
        source_key=plan.source_key,
        protocol="download",
        target_kind="vector",
        endpoint_url="https://example.test/archive.zip",
        remote_name="unrelated",
        sync_strategy="manual",
        config_json={},
        definition_sha256="0" * 64,
        enabled=False,
        is_primary=False,
        priority=127,
    )
    db.add(collision)
    db.commit()

    with pytest.raises(MetadataProbeSourceError) as error:
        apply_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
            expected_plan_sha256=plan.plan_sha256,
        )

    assert error.value.code == "source_conflict"
    assert _probe_source_count(db) == 1


def test_local_index_reader_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "index.html"
    target.write_bytes(_indexes(123)[0])
    link = tmp_path / "linked-index.html"
    link.symlink_to(target)

    with pytest.raises(MetadataProbeSourceError) as error:
        probe_source_admin._read_index(str(link))

    assert error.value.code == "operator_input_invalid"


def test_apply_expires_dry_run_identity_map_before_locked_revalidation(db) -> None:
    layer = _seed_catalog(db, audit_layer_id=123)
    root_index, province_index = _indexes(123)
    plan = plan_metadata_probe_source(
        db,
        audit_layer_id=123,
        root_index=root_index,
        province_index=province_index,
    )

    with Session(bind=db.get_bind()) as concurrent:
        changed = concurrent.get(ReferenceLayer, layer.id)
        changed.remote_name = "catalog:changed"
        concurrent.commit()

    assert layer.remote_name == _reviewed(123).catalog_remote_name
    with pytest.raises(MetadataProbeSourceError) as error:
        apply_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
            expected_plan_sha256=plan.plan_sha256,
        )

    assert error.value.code == "catalog_identity_changed"


def test_cli_dry_run_reads_exact_files_without_mutating_sources(
    db,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _seed_catalog(db, audit_layer_id=123)
    root = tmp_path / "root.html"
    provinces = tmp_path / "provinces.html"
    root_index, province_index = _indexes(123)
    root.write_bytes(root_index)
    provinces.write_bytes(province_index)
    monkeypatch.setattr(
        probe_source_admin,
        "SessionLocal",
        lambda: nullcontext(db),
    )
    monkeypatch.setattr(
        probe_source_admin,
        "register_all_models",
        lambda: None,
    )

    result = probe_source_admin.main(
        [
            "--audit-layer-id",
            "123",
            "--root-index",
            str(root),
            "--province-index",
            str(provinces),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert payload["mode"] == "dry-run"
    assert payload["safety"]["network_performed"] is False
    assert _probe_source_count(db) == 0


def test_changed_catalog_identity_is_rejected(db) -> None:
    layer = _seed_catalog(db, audit_layer_id=123)
    layer.remote_name = "catalog:changed"
    db.commit()
    root_index, province_index = _indexes(123)

    with pytest.raises(MetadataProbeSourceError) as error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
        )

    assert error.value.code == "catalog_identity_changed"


@pytest.mark.parametrize("tamper", ["hash", "normalized"])
def test_corrupt_current_catalog_snapshot_is_rejected(db, tamper) -> None:
    _seed_catalog(db, audit_layer_id=123)
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == "siur",
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )
    if tamper == "hash":
        snapshot.definition_sha256 = "0" * 64
    else:
        snapshot.normalized_definition_json = {
            **snapshot.normalized_definition_json,
            "provider_key": "tampered",
        }
    db.commit()
    root_index, province_index = _indexes(123)

    with pytest.raises(MetadataProbeSourceError) as error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
        )

    assert error.value.code == "catalog_integrity_invalid"


def test_changed_current_catalog_service_is_rejected(db) -> None:
    _seed_catalog(db, audit_layer_id=123)
    service = db.scalar(
        select(ReferenceService).where(
            ReferenceService.provider_key == "siur",
        )
    )
    service.base_url = "https://example.test/changed"
    db.commit()
    root_index, province_index = _indexes(123)

    with pytest.raises(MetadataProbeSourceError) as error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
        )

    assert error.value.code == "catalog_identity_changed"


def test_wrong_catalog_identity_and_layer_are_rejected(db) -> None:
    _seed_catalog(db, audit_layer_id=123, provider_key="other")
    root_index, province_index = _indexes(123)

    with pytest.raises(MetadataProbeSourceError) as provider_error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=123,
            root_index=root_index,
            province_index=province_index,
            provider_key="other",
        )
    assert provider_error.value.code == "provider_invalid"

    with pytest.raises(MetadataProbeSourceError) as layer_error:
        plan_metadata_probe_source(
            db,
            audit_layer_id=999,
            root_index=root_index,
            province_index=province_index,
        )
    assert layer_error.value.code == "layer_invalid"
