from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import threading
import time

import pytest
from conftest import headers_for, unique_suffix
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.reference_layers import catalog as reference_catalog
from app.reference_layers.catalog import (
    ReferenceCatalogDefinition,
    ReferenceCatalogValidationError,
    ReferenceLayerDefinition,
    ReferenceLayerStyleDefinition,
    ReferenceServiceDefinition,
    apply_catalog_definition,
    build_catalog_sync_plan,
)
from app.reference_layers.models import (
    OrganizationReferenceLayerSetting,
    ReferenceCatalogSnapshot,
    ReferenceLayer,
    ReferenceLayerStyle,
    ReferenceService,
)


def make_definition(
    *,
    layer_title: str = "Clasificación del suelo",
    include_overlay: bool = True,
    version: int = 1,
) -> ReferenceCatalogDefinition:
    layers = [
        ReferenceLayerDefinition(
            source_key="group:planning",
            node_type="group",
            title="Planeamiento urbanístico",
            sort_order=10,
        )
    ]
    if include_overlay:
        layers.append(
            ReferenceLayerDefinition(
                source_key="layer:classification",
                node_type="layer",
                title=layer_title,
                parent_key="group:planning",
                service_key="service:urbanismo-wms",
                remote_name="urbanismo:plau_cyl_clasificacion",
                role="overlay",
                renderer="raster_tile",
                delivery_mode="proxy",
                style_name="urbanismo:plau_cyl_clasificacion_color",
                image_format="image/png",
                supported_crs=("EPSG:25830", "EPSG:3857"),
                bounds={
                    "west": -7.1,
                    "south": 39.9,
                    "east": -1.7,
                    "north": 43.3,
                },
                sort_order=20,
                default_visible=True,
                default_opacity=Decimal("0.750"),
                queryable=True,
                downloadable=True,
                legend_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
                metadata_url="https://idecyl.jcyl.es/geonetwork/",
                styles=(
                    ReferenceLayerStyleDefinition(
                        source_key=(
                            "urbanismo:plau_cyl_clasificacion_color"
                        ),
                        title="Clasificación por color",
                        legend_url=(
                            "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
                            "?service=WMS&request=GetLegendGraphic"
                        ),
                        sort_order=10,
                        is_default=True,
                    ),
                    ReferenceLayerStyleDefinition(
                        source_key=(
                            "urbanismo:plau_cyl_clasificacion_trama"
                        ),
                        title="Clasificación por trama",
                        sort_order=20,
                    ),
                ),
            )
        )
    return ReferenceCatalogDefinition(
        provider_key="siur",
        source_url=(
            "https://idecyl.jcyl.es/siur/assets/settings/settings.json"
        ),
        raw_catalog={"fixture_version": version, "layer_title": layer_title},
        services=(
            ReferenceServiceDefinition(
                source_key="service:urbanismo-wms",
                title="Urbanismo de Castilla y León",
                upstream_protocol="wms",
                base_url="https://idecyl.jcyl.es/geoserver/urbanismo/wms",
                capabilities_url=(
                    "https://idecyl.jcyl.es/geoserver/urbanismo/wms"
                    "?service=WMS&request=GetCapabilities"
                ),
                version="1.3.0",
                default_crs="EPSG:25830",
                default_format="image/png",
                attribution="Junta de Castilla y León",
                license_name="Datos abiertos de Castilla y León",
                license_url="https://datosabiertos.jcyl.es/web/jcyl/RISP/",
                license_status="pending",
                cache_policy="on_demand",
                last_error="upstream https://internal.invalid/token=secret",
            ),
        ),
        layers=tuple(layers),
        retrieved_at=datetime(2026, 7, 17, 12, version, tzinfo=timezone.utc),
    )


def get_overlay(db) -> ReferenceLayer:
    return db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.source_key == "layer:classification"
        )
    )


def _wait_for_pending_advisory_lock(
    engine,
    backend_pid: int,
    future: Future,
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE pid = :pid AND locktype = 'advisory' "
                    "AND NOT granted)"
                ),
                {"pid": backend_pid},
            ).scalar_one()
        if waiting:
            return
        if future.done():
            future.result()
            pytest.fail("Concurrent catalog apply bypassed the provider lock")
        time.sleep(0.02)
    pytest.fail("Concurrent catalog apply did not wait for the provider lock")


def test_sync_is_dry_run_idempotent_and_preserves_stable_layer_identity(db) -> None:
    definition = make_definition()

    plan = build_catalog_sync_plan(db, definition)

    assert plan.blocking_issues == ()
    assert plan.new_services == ("service:urbanismo-wms",)
    assert plan.new_layers == ("group:planning", "layer:classification")
    assert plan.new_styles == (
        "layer:classification|urbanismo:plau_cyl_clasificacion_color",
        "layer:classification|urbanismo:plau_cyl_clasificacion_trama",
    )
    assert db.scalar(select(func.count(ReferenceCatalogSnapshot.id))) == 0

    snapshot, applied = apply_catalog_definition(
        db,
        definition,
        expected_plan=plan,
    )
    overlay = get_overlay(db)
    overlay_id = overlay.id

    assert snapshot.is_current is True
    assert snapshot.layer_count == 1
    assert applied.new_layers == plan.new_layers
    assert overlay.parent.source_key == "group:planning"
    assert overlay.service.source_key == "service:urbanismo-wms"
    assert [style.source_key for style in overlay.styles] == [
        "urbanismo:plau_cyl_clasificacion_color",
        "urbanismo:plau_cyl_clasificacion_trama",
    ]
    assert [style.is_default for style in overlay.styles] == [True, False]
    style_updated_at = {
        style.source_key: style.updated_at for style in overlay.styles
    }

    second_plan = build_catalog_sync_plan(db, definition)
    second_snapshot, second_applied = apply_catalog_definition(db, definition)

    assert second_plan.new_services == ()
    assert second_plan.updated_services == ()
    assert second_plan.new_layers == ()
    assert second_plan.updated_layers == ()
    assert second_plan.missing_layers == ()
    assert second_plan.new_styles == ()
    assert second_plan.updated_styles == ()
    assert second_plan.missing_styles == ()
    assert second_plan.unchanged_count == 5
    assert second_applied.unchanged_count == 5
    assert second_snapshot.id == snapshot.id
    assert db.scalar(select(func.count(ReferenceCatalogSnapshot.id))) == 1
    db.expire_all()
    assert {
        style.source_key: style.updated_at for style in get_overlay(db).styles
    } == style_updated_at

    with pytest.raises(
        ReferenceCatalogValidationError,
        match="state changed",
    ):
        apply_catalog_definition(db, definition, expected_plan=plan)

    renamed = make_definition(
        layer_title="Clasificación urbanística vigente",
        version=2,
    )
    renamed_plan = build_catalog_sync_plan(db, renamed)
    apply_catalog_definition(db, renamed)

    db.expire_all()
    renamed_overlay = get_overlay(db)
    assert renamed_plan.updated_layers == ("layer:classification",)
    assert renamed_overlay.id == overlay_id
    assert renamed_overlay.title == "Clasificación urbanística vigente"


def test_reviewed_catalog_plan_is_revalidated_after_provider_lock(
    engine,
    monkeypatch,
) -> None:
    provider_key = f"siur-{unique_suffix()}"
    baseline = replace(make_definition(), provider_key=provider_key)
    first_definition = replace(
        make_definition(
            layer_title="Clasificación urbanística primera",
            version=2,
        ),
        provider_key=provider_key,
    )
    second_definition = replace(
        make_definition(
            layer_title="Clasificación urbanística segunda",
            version=3,
        ),
        provider_key=provider_key,
    )
    with Session(engine) as seed_db:
        baseline_snapshot, _ = apply_catalog_definition(seed_db, baseline)
        baseline_snapshot_id = baseline_snapshot.id

    first_plan_built = threading.Event()
    release_first_apply = threading.Event()
    second_apply_started = threading.Event()
    first_worker = threading.local()
    second_backend_pid: list[int] = []
    original_build = reference_catalog.build_catalog_sync_plan

    def controlled_build(db, candidate_definition):
        plan = original_build(db, candidate_definition)
        if getattr(first_worker, "pause_after_build", False):
            first_plan_built.set()
            assert release_first_apply.wait(timeout=10)
        return plan

    monkeypatch.setattr(
        reference_catalog,
        "build_catalog_sync_plan",
        controlled_build,
    )

    def apply_reviewed_plan(
        candidate_definition,
        *,
        pause_after_build: bool,
    ) -> int:
        with Session(engine, expire_on_commit=False) as worker_db:
            first_worker.pause_after_build = False
            reviewed_plan = build_catalog_sync_plan(
                worker_db,
                candidate_definition,
            )
            assert reviewed_plan.updated_layers == ("layer:classification",)
            first_worker.pause_after_build = pause_after_build
            if not pause_after_build:
                second_backend_pid.append(
                    worker_db.execute(text("SELECT pg_backend_pid()"))
                    .scalar_one()
                )
                second_apply_started.set()
            snapshot, _ = apply_catalog_definition(
                worker_db,
                candidate_definition,
                expected_plan=reviewed_plan,
            )
            return snapshot.id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                apply_reviewed_plan,
                first_definition,
                pause_after_build=True,
            )
            assert first_plan_built.wait(timeout=10)
            second_future = executor.submit(
                apply_reviewed_plan,
                second_definition,
                pause_after_build=False,
            )
            assert second_apply_started.wait(timeout=10)
            try:
                _wait_for_pending_advisory_lock(
                    engine,
                    second_backend_pid[0],
                    second_future,
                )
            finally:
                release_first_apply.set()

            first_snapshot_id = first_future.result(timeout=10)
            with pytest.raises(
                ReferenceCatalogValidationError,
                match="state changed",
            ):
                second_future.result(timeout=10)

        with Session(engine) as verification_db:
            snapshots = verification_db.scalars(
                select(ReferenceCatalogSnapshot).where(
                    ReferenceCatalogSnapshot.provider_key == provider_key
                )
            ).all()
            current_snapshot_id = verification_db.scalar(
                select(ReferenceCatalogSnapshot.id).where(
                    ReferenceCatalogSnapshot.provider_key == provider_key,
                    ReferenceCatalogSnapshot.is_current.is_(True),
                )
            )
        assert {snapshot.id for snapshot in snapshots} == {
            baseline_snapshot_id,
            first_snapshot_id,
        }
        assert current_snapshot_id == first_snapshot_id
    finally:
        release_first_apply.set()
        with engine.begin() as cleanup:
            cleanup.execute(
                delete(ReferenceLayerStyle).where(
                    ReferenceLayerStyle.provider_key == provider_key
                )
            )
            cleanup.execute(
                delete(ReferenceLayer).where(
                    ReferenceLayer.provider_key == provider_key
                )
            )
            cleanup.execute(
                delete(ReferenceService).where(
                    ReferenceService.provider_key == provider_key
                )
            )
            cleanup.execute(
                delete(ReferenceCatalogSnapshot).where(
                    ReferenceCatalogSnapshot.provider_key == provider_key
                )
            )


def test_sync_marks_disappeared_layers_missing_without_deleting_preferences(
    db,
    make_organization,
) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    organization = make_organization()
    setting = OrganizationReferenceLayerSetting(
        organization_id=organization.id,
        layer_id=overlay.id,
        visible=False,
    )
    db.add(setting)
    db.commit()

    without_overlay = make_definition(include_overlay=False, version=3)
    plan = build_catalog_sync_plan(db, without_overlay)
    apply_catalog_definition(db, without_overlay)

    db.expire_all()
    persisted = db.get(ReferenceLayer, overlay.id)
    assert plan.missing_layers == ("layer:classification",)
    assert plan.missing_styles == (
        "layer:classification|urbanismo:plau_cyl_clasificacion_color",
        "layer:classification|urbanismo:plau_cyl_clasificacion_trama",
    )
    assert persisted.status == "missing"
    assert {style.status for style in persisted.styles} == {"missing"}
    assert not any(style.is_default for style in persisted.styles)
    assert db.scalar(
        select(func.count(OrganizationReferenceLayerSetting.id)).where(
            OrganizationReferenceLayerSetting.layer_id == overlay.id
        )
    ) == 1


def test_validation_blocks_duplicates_cycles_and_unsafe_service_urls(db) -> None:
    base = make_definition()
    oversized_provider = replace(base, provider_key="a" * 65)
    oversized_plan = build_catalog_sync_plan(db, oversized_provider)
    assert "Invalid provider_key" in oversized_plan.blocking_issues

    oversized_titles = replace(
        base,
        services=(replace(base.services[0], title="s" * 501),),
        layers=(
            base.layers[0],
            replace(
                base.layers[1],
                styles=(
                    replace(base.layers[1].styles[0], title="s" * 501),
                    base.layers[1].styles[1],
                ),
            ),
        ),
    )
    oversized_titles_plan = build_catalog_sync_plan(db, oversized_titles)
    assert "Invalid service title: service:urbanismo-wms" in (
        oversized_titles_plan.blocking_issues
    )
    assert any(
        "Invalid style title" in issue
        for issue in oversized_titles_plan.blocking_issues
    )

    duplicate = replace(base, layers=base.layers + (base.layers[0],))
    duplicate_plan = build_catalog_sync_plan(db, duplicate)
    assert "Duplicate layer key: group:planning" in duplicate_plan.blocking_issues

    cycle = replace(
        base,
        layers=(
            ReferenceLayerDefinition(
                source_key="group:a",
                node_type="group",
                title="A",
                parent_key="group:b",
            ),
            ReferenceLayerDefinition(
                source_key="group:b",
                node_type="group",
                title="B",
                parent_key="group:a",
            ),
        ),
    )
    cycle_plan = build_catalog_sync_plan(db, cycle)
    assert any("cycle" in issue for issue in cycle_plan.blocking_issues)

    unsafe_service = replace(
        base.services[0],
        base_url="http://127.0.0.1/internal",
    )
    unsafe = replace(base, services=(unsafe_service,))
    unsafe_plan = build_catalog_sync_plan(db, unsafe)
    assert "Invalid service URL: service:urbanismo-wms" in (
        unsafe_plan.blocking_issues
    )

    private_service = replace(
        base.services[0],
        base_url="https://[::1]/internal",
    )
    private_plan = build_catalog_sync_plan(
        db,
        replace(base, services=(private_service,)),
    )
    assert "Invalid service URL: service:urbanismo-wms" in (
        private_plan.blocking_issues
    )

    invalid_scale = replace(
        base.layers[1],
        min_scale_denominator=Decimal("0"),
    )
    scale_plan = build_catalog_sync_plan(
        db,
        replace(base, layers=(base.layers[0], invalid_scale)),
    )
    assert "Invalid scale denominator: layer:classification" in (
        scale_plan.blocking_issues
    )

    duplicate_style = replace(
        base.layers[1],
        styles=base.layers[1].styles + (base.layers[1].styles[0],),
    )
    duplicate_style_plan = build_catalog_sync_plan(
        db,
        replace(base, layers=(base.layers[0], duplicate_style)),
    )
    assert any(
        "Duplicate style key" in issue
        for issue in duplicate_style_plan.blocking_issues
    )

    two_defaults = replace(
        base.layers[1],
        styles=(
            base.layers[1].styles[0],
            replace(base.layers[1].styles[1], is_default=True),
        ),
    )
    default_plan = build_catalog_sync_plan(
        db,
        replace(base, layers=(base.layers[0], two_defaults)),
    )
    assert "Multiple default styles: layer:classification" in (
        default_plan.blocking_issues
    )

    mismatched_default = replace(
        base.layers[1],
        styles=(
            replace(base.layers[1].styles[0], is_default=False),
            replace(base.layers[1].styles[1], is_default=True),
        ),
    )
    mismatch_plan = build_catalog_sync_plan(
        db,
        replace(base, layers=(base.layers[0], mismatched_default)),
    )
    assert "Selected/default style mismatch: layer:classification" in (
        mismatch_plan.blocking_issues
    )

    undefined_selected = replace(base.layers[1], styles=())
    undefined_plan = build_catalog_sync_plan(
        db,
        replace(base, layers=(base.layers[0], undefined_selected)),
    )
    assert "Selected style has no definition: layer:classification" in (
        undefined_plan.blocking_issues
    )


def test_snapshots_keep_raw_and_normalized_payloads_immutable(db) -> None:
    original = make_definition()
    first, _ = apply_catalog_definition(db, original)
    first_id = first.id
    first_retrieved_at = first.retrieved_at

    permuted_layer = replace(
        original.layers[1],
        styles=tuple(reversed(original.layers[1].styles)),
    )
    permuted = replace(
        original,
        layers=(original.layers[0], permuted_layer),
    )
    same, same_plan = apply_catalog_definition(db, permuted)
    assert same.id == first.id
    assert same_plan.definition_sha256 == first.definition_sha256
    assert same_plan.unchanged_count == 5

    changed_service = replace(
        original.services[0],
        title="Servicio adaptado sin cambiar el bruto",
    )
    adapted = replace(
        original,
        services=(changed_service,),
        retrieved_at=datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc),
    )
    second, _ = apply_catalog_definition(db, adapted)

    db.expire_all()
    persisted_first = db.get(ReferenceCatalogSnapshot, first_id)
    assert second.id != first_id
    assert second.content_sha256 == persisted_first.content_sha256
    assert second.definition_sha256 != persisted_first.definition_sha256
    assert persisted_first.retrieved_at == first_retrieved_at
    assert persisted_first.normalized_definition_json["services"][0]["title"] == (
        "Urbanismo de Castilla y León"
    )
    assert second.normalized_definition_json["services"][0]["title"] == (
        "Servicio adaptado sin cambiar el bruto"
    )
    repeated, _ = apply_catalog_definition(
        db,
        replace(
            adapted,
            retrieved_at=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
        ),
    )
    assert repeated.id == second.id
    assert repeated.retrieved_at == adapted.retrieved_at


def test_catalog_requires_map_permission_and_never_exposes_upstream_urls(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    organization = make_organization()
    viewer = make_user()

    denied = client.get(
        f"/reference-layers/catalog?provider_key=siur&organization_id={organization.id}",
        headers=headers_for(viewer),
    )
    assert denied.status_code == 403

    grant_permissions(viewer, organization, ["map.view"])
    unavailable = client.get(
        f"/reference-layers/catalog?provider_key=siur&organization_id={organization.id}",
        headers=headers_for(viewer),
    )
    assert unavailable.status_code == 503

    apply_catalog_definition(db, make_definition())
    response = client.get(
        f"/reference-layers/catalog?provider_key=siur&organization_id={organization.id}",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["provider_key"] == "siur"
    assert [layer["source_key"] for layer in body["layers"]] == [
        "group:planning",
        "layer:classification",
    ]
    overlay = body["layers"][1]
    assert overlay["effective_visible"] is True
    assert overlay["effective_opacity"] == 0.75
    assert overlay["delivery_available"] is False
    assert overlay["identify_available"] is False
    assert overlay["delivery_blocker"] == "remote_proxy_disabled"
    assert overlay["available_style_ids"] == []
    assert overlay["legend_available"] is False
    assert overlay["metadata_available"] is True
    assert [style["title"] for style in body["styles"]] == [
        "Clasificación por color",
        "Clasificación por trama",
    ]
    assert body["styles"][0]["legend_available"] is False
    serialized = response.text
    assert "base_url" not in serialized
    assert "capabilities_url" not in serialized
    assert "license_url" not in serialized
    assert "last_error" not in serialized
    assert "legend_url" not in serialized
    assert "options_json" not in serialized
    assert "remote_name" not in serialized
    assert "style_name" not in serialized
    assert "supported_crs_json" not in serialized
    assert "license_status" not in serialized
    assert "cache_policy" not in serialized
    assert "reviewer" not in serialized
    assert "attestation_sha256" not in serialized
    assert "idecyl.jcyl.es" not in serialized


def test_catalog_excludes_historical_rows_missing_from_current_snapshot(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    organization = make_organization()
    viewer = make_user()
    grant_permissions(viewer, organization, ["map.view"])
    apply_catalog_definition(db, make_definition())
    current_snapshot, _ = apply_catalog_definition(
        db,
        replace(
            make_definition(include_overlay=False, version=3),
            services=(),
        ),
    )

    response = client.get(
        f"/reference-layers/catalog?provider_key=siur&organization_id={organization.id}",
        headers=headers_for(viewer),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["id"] == current_snapshot.id
    assert body["snapshot"]["service_count"] == len(body["services"]) == 0
    assert body["snapshot"]["group_count"] == 1
    assert body["snapshot"]["layer_count"] == 0
    assert [item["source_key"] for item in body["layers"]] == [
        "group:planning"
    ]
    assert body["styles"] == []


def test_catalog_requires_an_explicit_provider_and_keeps_providers_isolated(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    organization = make_organization()
    viewer = make_user()
    grant_permissions(viewer, organization, ["map.view"])
    apply_catalog_definition(db, make_definition())
    other = replace(
        make_definition(layer_title="Otra clasificación", version=4),
        provider_key="other",
    )
    apply_catalog_definition(db, other)

    missing_provider = client.get(
        f"/reference-layers/catalog?organization_id={organization.id}",
        headers=headers_for(viewer),
    )
    assert missing_provider.status_code == 422

    response = client.get(
        f"/reference-layers/catalog?provider_key=other&organization_id={organization.id}",
        headers=headers_for(viewer),
    )
    assert response.status_code == 200
    assert response.json()["snapshot"]["provider_key"] == "other"
    assert {item["title"] for item in response.json()["layers"]} >= {
        "Otra clasificación"
    }


def test_database_rejects_cross_provider_service_and_parent_links(db) -> None:
    apply_catalog_definition(db, make_definition())
    apply_catalog_definition(
        db,
        replace(make_definition(version=5), provider_key="other"),
    )
    siur_snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == "siur",
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )
    siur_service = db.scalar(
        select(ReferenceService).where(ReferenceService.provider_key == "siur")
    )
    other_service = db.scalar(
        select(ReferenceService).where(ReferenceService.provider_key == "other")
    )
    other_group = db.scalar(
        select(ReferenceLayer).where(
            ReferenceLayer.provider_key == "other",
            ReferenceLayer.source_key == "group:planning",
        )
    )
    other_snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == "other",
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )

    db.add(
        ReferenceService(
            provider_key="siur",
            source_key="service:cross-snapshot",
            last_seen_snapshot_id=other_snapshot.id,
            title="Invalid cross-provider snapshot",
            upstream_protocol="wms",
            base_url="https://example.test/wms",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    db.add(
        ReferenceLayerStyle(
            provider_key="siur",
            source_key="style:cross-layer",
            remote_name="style:cross-layer",
            title="Invalid cross-provider layer",
            last_seen_snapshot_id=siur_snapshot.id,
            layer_id=other_group.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    db.add(
        ReferenceLayer(
            provider_key="siur",
            source_key="layer:cross-snapshot",
            node_type="layer",
            title="Invalid cross-provider snapshot",
            last_seen_snapshot_id=other_snapshot.id,
            service_id=siur_service.id,
            role="overlay",
            renderer="raster_tile",
            delivery_mode="proxy",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    db.add(
        ReferenceLayer(
            provider_key="siur",
            source_key="layer:cross-service",
            node_type="layer",
            title="Invalid cross-provider service",
            last_seen_snapshot_id=siur_snapshot.id,
            service_id=other_service.id,
            role="overlay",
            renderer="raster_tile",
            delivery_mode="proxy",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    db.add(
        ReferenceLayer(
            provider_key="siur",
            source_key="layer:cross-parent",
            node_type="layer",
            title="Invalid cross-provider parent",
            last_seen_snapshot_id=siur_snapshot.id,
            service_id=siur_service.id,
            parent_id=other_group.id,
            role="overlay",
            renderer="raster_tile",
            delivery_mode="proxy",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_database_allows_only_one_default_style_per_layer(db) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    snapshot = db.scalar(
        select(ReferenceCatalogSnapshot).where(
            ReferenceCatalogSnapshot.provider_key == "siur",
            ReferenceCatalogSnapshot.is_current.is_(True),
        )
    )

    db.add(
        ReferenceLayerStyle(
            provider_key="siur",
            source_key="urbanismo:second-default",
            remote_name="urbanismo:second-default",
            title="Invalid second default",
            last_seen_snapshot_id=snapshot.id,
            layer_id=overlay.id,
            is_default=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_organization_settings_require_manage_and_restore_catalog_defaults(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    first_org = make_organization("First council")
    second_org = make_organization("Second council")
    viewer = make_user()
    manager = make_user()
    grant_permissions(viewer, first_org, ["map.view"])
    grant_permissions(manager, first_org, ["map.manage"])

    path = (
        f"/organizations/{first_org.id}/reference-layers/"
        f"{overlay.id}/settings"
    )
    denied = client.put(
        path,
        headers=headers_for(viewer),
        json={"visible": False},
    )
    assert denied.status_code == 403

    updated = client.put(
        path,
        headers=headers_for(manager),
        json={"visible": False, "opacity": 0.25},
    )
    assert updated.status_code == 200
    assert updated.json()["updated_by_id"] == manager.id

    catalog = client.get(
        f"/reference-layers/catalog?provider_key=siur&organization_id={first_org.id}",
        headers=headers_for(manager),
    ).json()
    effective = next(
        item
        for item in catalog["layers"]
        if item["source_key"] == "layer:classification"
    )
    assert effective["effective_visible"] is False
    assert effective["effective_opacity"] == 0.25

    cross_org = client.put(
        f"/organizations/{second_org.id}/reference-layers/"
        f"{overlay.id}/settings",
        headers=headers_for(manager),
        json={"visible": False},
    )
    assert cross_org.status_code == 403

    deleted = client.delete(path, headers=headers_for(manager))
    assert deleted.status_code == 204
    reset_catalog = client.get(
        f"/reference-layers/catalog?provider_key=siur&organization_id={first_org.id}",
        headers=headers_for(manager),
    ).json()
    reset = next(
        item
        for item in reset_catalog["layers"]
        if item["source_key"] == "layer:classification"
    )
    assert reset["effective_visible"] is True
    assert reset["effective_opacity"] == 0.75


def test_superuser_can_manage_reference_settings_for_any_organization(
    client,
    db,
    superuser,
    make_organization,
) -> None:
    apply_catalog_definition(db, make_definition())
    overlay = get_overlay(db)
    organization = make_organization()

    response = client.put(
        f"/organizations/{organization.id}/reference-layers/"
        f"{overlay.id}/settings",
        headers=headers_for(superuser),
        json={"opacity": 0.5},
    )

    assert response.status_code == 200
    assert response.json()["opacity"] == 0.5
    assert db.scalar(select(func.count(ReferenceService.id))) == 1
