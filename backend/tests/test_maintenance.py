import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import headers_for, unique_suffix
from fastapi import HTTPException
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.assets.models import (
    MunicipalAsset,
    MunicipalAssetCategory,
    MunicipalAssetType,
)
from app.db.base import Base
from app.maintenance.models import MaintenanceOrder, MaintenanceOrderEvent
from app.maintenance.routes import transition_maintenance_order
from app.maintenance.schemas import MaintenanceOrderTransition
from app.municipalities.models import Municipality
from app.organizations.models import Organization, organization_users
from app.users.crud import create_user
from app.users.models import User


@pytest.fixture
def make_maintenance_context(db, make_organization):
    def _make(*, organization_status="active", municipality_status="active"):
        suffix = unique_suffix()
        municipality = Municipality(
            name=f"Municipio {suffix}",
            province="Burgos",
            autonomous_community="Castilla y Leon",
            ine_code=f"mnt-{suffix}",
            status=municipality_status,
        )
        db.add(municipality)
        db.commit()
        organization = make_organization(
            name=f"Ayuntamiento {suffix}",
            municipality_id=municipality.id,
            status=organization_status,
        )
        category = MunicipalAssetCategory(
            organization_id=organization.id,
            code=f"cat-{suffix}",
            name=f"Categoria {suffix}",
        )
        db.add(category)
        db.flush()
        asset_type = MunicipalAssetType(
            organization_id=organization.id,
            category_id=category.id,
            code=f"type-{suffix}",
            name=f"Tipo {suffix}",
        )
        db.add(asset_type)
        db.flush()
        asset = MunicipalAsset(
            organization_id=organization.id,
            municipality_id=municipality.id,
            asset_type_id=asset_type.id,
            code=f"ASSET-{suffix}",
            name=f"Activo {suffix}",
        )
        db.add(asset)
        db.commit()
        return organization, municipality, asset

    return _make


@pytest.fixture
def maintenance_concurrency_engine(engine):
    source_url = make_url(engine.url)
    database_name = f"app_test_maint_concurrency_{uuid.uuid4().hex}"
    server_engine = create_engine(
        source_url.set(database="app"),
        isolation_level="AUTOCOMMIT",
    )
    with server_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    isolated_engine = create_engine(source_url.set(database=database_name))
    Base.metadata.create_all(isolated_engine)
    try:
        yield isolated_engine
    finally:
        isolated_engine.dispose()
        with server_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        server_engine.dispose()


def order_payload(asset_id, **overrides):
    payload = {
        "asset_id": asset_id,
        "title": f"Mantenimiento {unique_suffix()}",
        "description": "Revision del activo municipal",
        "maintenance_type": "preventive",
        "priority": "normal",
        "estimated_minutes": 90,
    }
    payload.update(overrides)
    return payload


def create_order(client, auth, asset_id, **overrides):
    response = client.post(
        "/maintenance/orders",
        json=order_payload(asset_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def transition(client, auth, order_id, target_status, **overrides):
    response = client.post(
        f"/maintenance/orders/{order_id}/transition",
        json={"status": target_status, **overrides},
        headers=auth,
    )
    return response


def test_maintenance_requires_authentication(client):
    assert (
        client.get("/maintenance/orders", params={"organization_id": 1}).status_code
        == 401
    )
    assert client.post("/maintenance/orders", json={}).status_code == 401


def test_create_derives_tenant_and_records_minimal_immutable_audit(
    client,
    superuser,
    make_user,
    add_member,
    make_maintenance_context,
):
    organization, municipality, asset = make_maintenance_context()
    assignee = make_user(full_name="Tecnica Municipal")
    add_member(assignee, organization)
    auth = headers_for(superuser)

    created = create_order(
        client,
        auth,
        asset.id,
        title="Revision anual",
        scheduled_for="2026-08-20",
        assigned_to_id=assignee.id,
    )

    assert created["organization_id"] == organization.id
    assert created["municipality_id"] == municipality.id
    assert created["status"] == "scheduled"
    assert created["asset"] == {
        "id": asset.id,
        "code": asset.code,
        "name": asset.name,
        "status": "active",
    }
    assert created["assigned_to"] == {
        "id": assignee.id,
        "full_name": "Tecnica Municipal",
    }
    assert created["created_by_id"] == superuser.id
    assert len(created["events"]) == 1
    event = created["events"][0]
    assert event["event_type"] == "created"
    assert event["from_status"] is None
    assert event["to_status"] == "scheduled"
    assert event["actor_id"] == superuser.id
    assert "changes" not in event
    assert set(event["changed_fields"]) == {
        "asset_id",
        "title",
        "description",
        "maintenance_type",
        "priority",
        "status",
        "scheduled_for",
        "estimated_minutes",
        "assigned_to_id",
    }

    forbidden = client.post(
        "/maintenance/orders",
        json={
            **order_payload(asset.id),
            "organization_id": organization.id,
            "status": "completed",
        },
        headers=auth,
    )
    assert forbidden.status_code == 422
    assert {
        error["loc"][-1]
        for error in forbidden.json()["detail"]
        if error["type"] == "extra_forbidden"
    } == {"organization_id", "status"}
    assert (
        client.delete(
            f"/maintenance/orders/{created['id']}", headers=auth
        ).status_code
        == 405
    )


def test_permissions_are_separate_and_require_asset_visibility(
    client,
    make_user,
    grant_permissions,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    super_auth = headers_for(superuser)
    order = create_order(client, super_auth, asset.id)

    viewer = make_user()
    grant_permissions(viewer, organization, ["maintenance.view"])
    viewer_auth = headers_for(viewer)
    denied_asset_view = client.get(
        "/maintenance/orders",
        params={"organization_id": organization.id},
        headers=viewer_auth,
    )
    assert denied_asset_view.status_code == 403
    assert denied_asset_view.json()["detail"] == "Permission required: assets.view"

    grant_permissions(viewer, organization, ["assets.view"])
    assert (
        client.get(
            "/maintenance/orders",
            params={"organization_id": organization.id},
            headers=viewer_auth,
        ).status_code
        == 200
    )
    denied_create = client.post(
        "/maintenance/orders",
        json=order_payload(asset.id),
        headers=viewer_auth,
    )
    assert denied_create.status_code == 403
    assert denied_create.json()["detail"] == "Permission required: maintenance.create"

    creator = make_user()
    grant_permissions(
        creator,
        organization,
        ["assets.view", "maintenance.create"],
    )
    created = create_order(client, headers_for(creator), asset.id)
    denied_edit = client.patch(
        f"/maintenance/orders/{created['id']}",
        json={"priority": "high"},
        headers=headers_for(creator),
    )
    assert denied_edit.status_code == 403
    assert denied_edit.json()["detail"] == "Permission required: maintenance.edit"

    editor = make_user()
    grant_permissions(
        editor,
        organization,
        ["assets.view", "maintenance.edit"],
    )
    edited = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"priority": "urgent"},
        headers=headers_for(editor),
    )
    assert edited.status_code == 200
    assert edited.json()["priority"] == "urgent"
    assert edited.json()["events"][-1]["changed_fields"] == ["priority"]


def test_patch_forbids_control_fields_and_noop_does_not_add_event(
    client,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    order = create_order(client, auth, asset.id, priority="normal")
    original_event_count = len(order["events"])

    forbidden = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={
            "status": "completed",
            "asset_id": asset.id,
            "organization_id": organization.id,
        },
        headers=auth,
    )
    noop = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"priority": "normal"},
        headers=auth,
    )

    assert forbidden.status_code == 422
    assert {
        error["loc"][-1]
        for error in forbidden.json()["detail"]
        if error["type"] == "extra_forbidden"
    } == {"status", "asset_id", "organization_id"}
    assert noop.status_code == 200
    assert len(noop.json()["events"]) == original_event_count


def test_tenant_resources_and_invalid_assignees_are_hidden(
    client,
    db,
    make_user,
    grant_permissions,
    add_member,
    superuser,
    make_maintenance_context,
):
    first, _, first_asset = make_maintenance_context()
    second, _, second_asset = make_maintenance_context()
    second_order = create_order(client, headers_for(superuser), second_asset.id)
    user = make_user()
    grant_permissions(
        user,
        first,
        [
            "assets.view",
            "maintenance.view",
            "maintenance.create",
            "maintenance.edit",
        ],
    )
    auth = headers_for(user)

    for response in (
        client.get(f"/maintenance/orders/{second_order['id']}", headers=auth),
        client.patch(
            f"/maintenance/orders/{second_order['id']}",
            json={"priority": "high"},
            headers=auth,
        ),
        client.post(
            "/maintenance/orders",
            json=order_payload(second_asset.id),
            headers=auth,
        ),
    ):
        assert response.status_code == 404

    outsider = make_user()
    invalid_assignment = client.post(
        "/maintenance/orders",
        json=order_payload(first_asset.id, assigned_to_id=outsider.id),
        headers=auth,
    )
    assert invalid_assignment.status_code == 404
    add_member(outsider, first)
    outsider.is_active = False
    db.commit()
    invalid_inactive = client.post(
        "/maintenance/orders",
        json=order_payload(first_asset.id, assigned_to_id=outsider.id),
        headers=auth,
    )
    assert invalid_inactive.status_code == 404


def test_paused_is_read_only_and_archived_is_hidden(
    client,
    db,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    order = create_order(client, auth, asset.id)

    organization.status = "paused"
    db.commit()
    paused_list = client.get(
        "/maintenance/orders",
        params={"organization_id": organization.id},
        headers=auth,
    )
    paused_write = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"priority": "high"},
        headers=auth,
    )
    assert paused_list.status_code == 200
    assert paused_write.status_code == 409

    organization.status = "archived"
    db.commit()
    archived_list = client.get(
        "/maintenance/orders",
        params={"organization_id": organization.id},
        headers=auth,
    )
    archived_detail = client.get(
        f"/maintenance/orders/{order['id']}", headers=auth
    )
    archived_write = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"priority": "urgent"},
        headers=auth,
    )
    assert archived_list.status_code == 404
    assert archived_detail.status_code == 404
    assert archived_write.status_code == 404


def test_schedule_date_invariants_and_filters(
    client,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    order = create_order(client, auth, asset.id)
    assert order["status"] == "planned"
    assert order["scheduled_for"] is None

    direct_date = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"scheduled_for": "2026-09-01"},
        headers=auth,
    )
    missing_date = transition(client, auth, order["id"], "scheduled")
    scheduled = transition(
        client,
        auth,
        order["id"],
        "scheduled",
        scheduled_for="2026-09-01",
    )
    assert direct_date.status_code == 409
    assert missing_date.status_code == 409
    assert scheduled.status_code == 200
    assert scheduled.json()["scheduled_for"] == "2026-09-01"
    assert set(scheduled.json()["events"][-1]["changed_fields"]) == {
        "status",
        "scheduled_for",
    }

    clear_scheduled = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"scheduled_for": None},
        headers=auth,
    )
    back_to_planned = transition(client, auth, order["id"], "planned")
    assert clear_scheduled.status_code == 409
    assert back_to_planned.status_code == 200
    assert back_to_planned.json()["scheduled_for"] is None

    invalid_range = client.get(
        "/maintenance/orders",
        params={
            "organization_id": organization.id,
            "scheduled_from": "2026-10-01",
            "scheduled_to": "2026-09-01",
        },
        headers=auth,
    )
    assert invalid_range.status_code == 422


def test_restrictive_transition_matrix_and_terminal_audit(
    client,
    make_user,
    grant_permissions,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    super_auth = headers_for(superuser)
    order = create_order(client, super_auth, asset.id)

    editor = make_user()
    grant_permissions(
        editor,
        organization,
        ["assets.view", "maintenance.edit"],
    )
    editor_auth = headers_for(editor)
    started = transition(client, editor_auth, order["id"], "in_progress")
    denied_cancel = transition(
        client,
        editor_auth,
        order["id"],
        "cancelled",
        note="No procede",
    )
    assert started.status_code == 200
    assert denied_cancel.status_code == 403
    assert denied_cancel.json()["detail"] == "Permission required: maintenance.manage"

    completer = make_user()
    grant_permissions(
        completer,
        organization,
        ["assets.view", "maintenance.complete"],
    )
    completed = transition(
        client,
        headers_for(completer),
        order["id"],
        "completed",
        note="Trabajo verificado",
    )
    assert completed.status_code == 200
    assert completed.json()["events"][-1]["from_status"] == "in_progress"
    assert completed.json()["events"][-1]["to_status"] == "completed"

    terminal_edit = client.patch(
        f"/maintenance/orders/{order['id']}",
        json={"description": "Cambio no autorizado"},
        headers=editor_auth,
    )
    missing_reopen_reason = transition(
        client, super_auth, order["id"], "planned"
    )
    reopened = transition(
        client,
        super_auth,
        order["id"],
        "planned",
        note="Nueva incidencia detectada",
    )
    assert terminal_edit.status_code == 403
    assert missing_reopen_reason.status_code == 422
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "planned"
    assert reopened.json()["events"][-1]["note"] == "Nueva incidencia detectada"

    no_cancel_reason = transition(
        client, super_auth, order["id"], "cancelled"
    )
    cancelled = transition(
        client,
        super_auth,
        order["id"],
        "cancelled",
        note="Activo fuera de servicio",
    )
    assert no_cancel_reason.status_code == 422
    assert cancelled.status_code == 200


def test_scheduled_cannot_complete_without_starting(
    client,
    superuser,
    make_maintenance_context,
):
    _, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    order = create_order(
        client,
        auth,
        asset.id,
        scheduled_for="2026-10-10",
    )

    response = transition(client, auth, order["id"], "completed")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Invalid maintenance transition: scheduled -> completed"
    )


@pytest.mark.parametrize("asset_status", ["retired", "archived"])
def test_terminal_order_cannot_reopen_retired_or_archived_asset(
    client,
    superuser,
    make_maintenance_context,
    asset_status,
):
    _, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    order = create_order(client, auth, asset.id)
    assert transition(client, auth, order["id"], "in_progress").status_code == 200
    completed = transition(client, auth, order["id"], "completed")
    assert completed.status_code == 200
    event_count = len(completed.json()["events"])
    terminal_asset = client.patch(
        f"/assets/{asset.id}",
        json={"status": asset_status},
        headers=auth,
    )
    assert terminal_asset.status_code == 200

    reopened = transition(
        client,
        auth,
        order["id"],
        "planned",
        note="Nueva incidencia",
    )

    assert reopened.status_code == 409
    assert reopened.json()["detail"] == (
        "Retired or archived assets cannot reopen maintenance orders"
    )
    detail = client.get(f"/maintenance/orders/{order['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert len(detail.json()["events"]) == event_count


@pytest.mark.parametrize("invalidation", ["membership", "inactive"])
def test_reopen_revalidates_preserved_assignee(
    client,
    make_user,
    add_member,
    superuser,
    make_maintenance_context,
    invalidation,
):
    organization, _, asset = make_maintenance_context()
    assignee = make_user(full_name="Asignada historica")
    add_member(assignee, organization)
    auth = headers_for(superuser)
    order = create_order(
        client,
        auth,
        asset.id,
        assigned_to_id=assignee.id,
    )
    assert transition(client, auth, order["id"], "in_progress").status_code == 200
    completed = transition(client, auth, order["id"], "completed")
    assert completed.status_code == 200
    event_count = len(completed.json()["events"])

    if invalidation == "membership":
        invalidated = client.delete(
            f"/organizations/{organization.id}/users/{assignee.id}",
            headers=auth,
        )
    else:
        invalidated = client.patch(
            f"/admin/users/{assignee.id}",
            json={"is_active": False},
            headers=auth,
        )
    assert invalidated.status_code == 200

    reopened = transition(
        client,
        auth,
        order["id"],
        "planned",
        note="Nueva incidencia",
    )

    assert reopened.status_code == 404
    assert reopened.json()["detail"] == "Resource not found"
    detail = client.get(f"/maintenance/orders/{order['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert len(detail.json()["events"]) == event_count


def test_open_assignment_blocks_membership_deactivation_and_asset_retirement(
    client,
    db,
    make_user,
    add_member,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    assignee = make_user(full_name="Operaria asignada")
    add_member(assignee, organization)
    auth = headers_for(superuser)
    order = create_order(
        client,
        auth,
        asset.id,
        assigned_to_id=assignee.id,
    )

    remove_open = client.delete(
        f"/organizations/{organization.id}/users/{assignee.id}",
        headers=auth,
    )
    deactivate_open = client.patch(
        f"/admin/users/{assignee.id}",
        json={"is_active": False},
        headers=auth,
    )
    retire_open = client.patch(
        f"/assets/{asset.id}",
        json={"status": "retired"},
        headers=auth,
    )
    assert remove_open.status_code == 409
    assert deactivate_open.status_code == 409
    assert retire_open.status_code == 409

    assert transition(client, auth, order["id"], "in_progress").status_code == 200
    assert transition(client, auth, order["id"], "completed").status_code == 200
    remove_closed = client.delete(
        f"/organizations/{organization.id}/users/{assignee.id}",
        headers=auth,
    )
    delete_historical = client.delete(
        f"/admin/users/{assignee.id}",
        headers=auth,
    )
    assert remove_closed.status_code == 200
    assert delete_historical.status_code == 409
    assert delete_historical.json()["detail"] == (
        "User has maintenance history that must be preserved"
    )

    retired = client.patch(
        f"/assets/{asset.id}",
        json={"status": "retired"},
        headers=auth,
    )
    assert retired.status_code == 200
    db.expire_all()
    assert db.get(User, assignee.id) is not None


def test_retired_asset_rejects_new_maintenance(
    client,
    superuser,
    make_maintenance_context,
):
    _, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    retired = client.patch(
        f"/assets/{asset.id}",
        json={"status": "retired"},
        headers=auth,
    )
    response = client.post(
        "/maintenance/orders",
        json=order_payload(asset.id),
        headers=auth,
    )

    assert retired.status_code == 200
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Retired or archived assets cannot receive maintenance orders"
    )


def test_closed_orders_are_opt_in_and_list_is_paginated(
    client,
    superuser,
    make_maintenance_context,
):
    organization, _, asset = make_maintenance_context()
    auth = headers_for(superuser)
    open_order = create_order(
        client,
        auth,
        asset.id,
        title="Inspeccion abierta",
        priority="high",
    )
    closed_order = create_order(
        client,
        auth,
        asset.id,
        title="Limpieza finalizada",
        maintenance_type="cleaning",
    )
    cancelled = transition(
        client,
        auth,
        closed_order["id"],
        "cancelled",
        note="Trabajo duplicado",
    )
    assert cancelled.status_code == 200

    default_list = client.get(
        "/maintenance/orders",
        params={"organization_id": organization.id},
        headers=auth,
    )
    all_orders = client.get(
        "/maintenance/orders",
        params={"organization_id": organization.id, "include_closed": True},
        headers=auth,
    )
    search = client.get(
        "/maintenance/orders",
        params={
            "organization_id": organization.id,
            "include_closed": True,
            "q": "Limpieza",
            "limit": 1,
        },
        headers=auth,
    )
    assert [item["id"] for item in default_list.json()] == [open_order["id"]]
    assert {item["id"] for item in all_orders.json()} == {
        open_order["id"],
        closed_order["id"],
    }
    assert search.headers["X-Total-Count"] == "1"
    assert [item["id"] for item in search.json()] == [closed_order["id"]]


def test_database_rejects_cross_tenant_order_and_event_mutation(
    client,
    db,
    superuser,
    make_maintenance_context,
):
    first, _, first_asset = make_maintenance_context()
    second, second_municipality, _ = make_maintenance_context()
    order = create_order(client, headers_for(superuser), first_asset.id)

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(
                insert(MaintenanceOrder).values(
                    organization_id=second.id,
                    municipality_id=second_municipality.id,
                    asset_id=first_asset.id,
                    title="Cruce de tenant",
                    created_by_id=superuser.id,
                    updated_by_id=superuser.id,
                )
            )
            db.flush()

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(
                insert(MaintenanceOrderEvent).values(
                    order_id=order["id"],
                    organization_id=second.id,
                    event_type="updated",
                    changed_fields=["title"],
                    actor_id=superuser.id,
                )
            )
            db.flush()

    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(
                insert(MaintenanceOrderEvent).values(
                    order_id=order["id"],
                    organization_id=first.id,
                    event_type="updated",
                    changed_fields=["title"],
                    actor_id=None,
                )
            )
            db.flush()

    event_id = order["events"][0]["id"]
    for statement in (
        "UPDATE maintenance_order_events SET note = 'alterado' WHERE id = :id",
        "DELETE FROM maintenance_order_events WHERE id = :id",
    ):
        with pytest.raises(
            DBAPIError,
            match="maintenance order events are immutable",
        ):
            with db.begin_nested():
                db.execute(text(statement), {"id": event_id})

    assert db.scalar(
        select(MaintenanceOrderEvent.id).where(
            MaintenanceOrderEvent.id == event_id
        )
    ) == event_id


def test_concurrent_transition_has_one_winner_and_one_audit_event(
    maintenance_concurrency_engine,
):
    engine = maintenance_concurrency_engine
    suffix = unique_suffix()
    with Session(engine) as setup:
        user = create_user(
            setup,
            email=f"concurrent-{suffix}@example.com",
            password="password-123",
            full_name="Concurrent Manager",
            is_superuser=True,
        )
        municipality = Municipality(
            name=f"Concurrent {suffix}",
            province="Burgos",
            autonomous_community="Castilla y Leon",
            ine_code=f"cc-{suffix}",
        )
        setup.add(municipality)
        setup.flush()
        organization = Organization(
            name=f"Concurrent Org {suffix}",
            municipality_id=municipality.id,
        )
        setup.add(organization)
        setup.flush()
        category = MunicipalAssetCategory(
            organization_id=organization.id,
            code=f"cc-{suffix}",
            name="Concurrent",
        )
        setup.add(category)
        setup.flush()
        asset_type = MunicipalAssetType(
            organization_id=organization.id,
            category_id=category.id,
            code=f"cc-{suffix}",
            name="Concurrent",
        )
        setup.add(asset_type)
        setup.flush()
        asset = MunicipalAsset(
            organization_id=organization.id,
            municipality_id=municipality.id,
            asset_type_id=asset_type.id,
            name="Concurrent asset",
        )
        setup.add(asset)
        setup.flush()
        order = MaintenanceOrder(
            organization_id=organization.id,
            municipality_id=municipality.id,
            asset_id=asset.id,
            title="Concurrent order",
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        setup.add(order)
        setup.commit()
        user_id = user.id
        order_id = order.id

    barrier = Barrier(2)

    def run_transition() -> int:
        with Session(engine, expire_on_commit=False) as session:
            current_user = session.get(User, user_id)
            barrier.wait()
            try:
                transition_maintenance_order(
                    order_id,
                    MaintenanceOrderTransition(status="in_progress"),
                    session,
                    current_user,
                )
            except HTTPException as error:
                session.rollback()
                return error.status_code
            return 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _: run_transition(), range(2)))

    assert results == [200, 409]
    with Session(engine) as verification:
        stored = verification.get(MaintenanceOrder, order_id)
        events = list(
            verification.scalars(
                select(MaintenanceOrderEvent).where(
                    MaintenanceOrderEvent.order_id == order_id,
                    MaintenanceOrderEvent.event_type == "transition",
                )
            )
        )
        assert stored.status == "in_progress"
        assert len(events) == 1
        assert events[0].from_status == "planned"
        assert events[0].to_status == "in_progress"
