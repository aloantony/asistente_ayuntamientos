from datetime import date, timedelta

import pytest
from conftest import headers_for, unique_suffix

from app.projects.models import Project


@pytest.fixture
def task_organization(make_organization):
    return make_organization(name=f"Ayuntamiento {unique_suffix()}")


def task_payload(organization_id, **overrides):
    suffix = unique_suffix()
    payload = {
        "organization_id": organization_id,
        "title": f"Tarea {suffix}",
        "description": "Trabajo pendiente del ayuntamiento",
        "priority": "normal",
    }
    payload.update(overrides)
    return payload


def create_task(client, auth, organization_id, **overrides):
    response = client.post(
        "/tasks",
        json=task_payload(organization_id, **overrides),
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_worker(client, auth, organization_id, **overrides):
    payload = {
        "organization_id": organization_id,
        "full_name": f"Trabajador {unique_suffix()}",
    }
    payload.update(overrides)
    response = client.post("/staff/workers", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def transition(client, auth, task_id, status_value, reason=None):
    body = {"status": status_value}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"/tasks/{task_id}/transition", json=body, headers=auth)


@pytest.mark.parametrize(
    "path",
    ["/tasks?organization_id=1", "/tasks/summary?organization_id=1", "/tasks/1"],
)
def test_roadmap_requires_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401


def test_task_permissions_are_scoped_to_the_target_organization(
    client,
    make_user,
    make_organization,
    grant_permissions,
    add_member,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    task = create_task(client, headers_for(superuser), target.id)

    user = make_user()
    grant_permissions(user, other, ["tasks.manage"])
    add_member(user, target)
    auth = headers_for(user)

    listed = client.get("/tasks", params={"organization_id": target.id}, headers=auth)
    detail = client.get(f"/tasks/{task['id']}", headers=auth)
    created = client.post("/tasks", json=task_payload(target.id), headers=auth)

    assert listed.status_code == 403
    assert listed.json()["detail"] == "Permission required: tasks.view"
    assert detail.status_code == 403
    assert created.status_code == 403
    assert created.json()["detail"] == "Permission required: tasks.create"


def test_overdue_is_derived_from_the_due_date_and_never_stored(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)
    today = date.today()

    overdue = create_task(
        client,
        auth,
        task_organization.id,
        due_date=str(today - timedelta(days=1)),
    )
    due_today = create_task(
        client,
        auth,
        task_organization.id,
        due_date=str(today),
    )
    future = create_task(
        client,
        auth,
        task_organization.id,
        due_date=str(today + timedelta(days=7)),
    )
    undated = create_task(client, auth, task_organization.id)

    # Ninguna respuesta lleva un campo "vencida": es una lectura del calendario,
    # no un dato guardado.
    assert "overdue" not in overdue
    assert "is_overdue" not in overdue

    filtered = client.get(
        "/tasks",
        params={"organization_id": task_organization.id, "overdue": True},
        headers=auth,
    )
    assert [item["id"] for item in filtered.json()] == [overdue["id"]]

    summary = client.get(
        "/tasks/summary",
        params={"organization_id": task_organization.id},
        headers=auth,
    ).json()
    assert summary["overdue"] == 1
    assert summary["total"] == 4
    assert summary["reference_date"] == str(today)
    assert due_today["id"] and future["id"] and undated["id"]


def test_completing_a_task_stops_it_from_being_overdue(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)

    task = create_task(
        client,
        auth,
        task_organization.id,
        due_date=str(date.today() - timedelta(days=3)),
    )
    before = client.get(
        "/tasks/summary",
        params={"organization_id": task_organization.id},
        headers=auth,
    ).json()

    transition(client, auth, task["id"], "in_progress")
    completed = transition(client, auth, task["id"], "completed")

    after = client.get(
        "/tasks/summary",
        params={"organization_id": task_organization.id},
        headers=auth,
    ).json()

    assert before["overdue"] == 1
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None
    # Una tarea cerrada tarde deja de reclamar atención, aunque su fecha
    # siguiera en el pasado.
    assert after["overdue"] == 0
    assert after["completed"] == 1


def test_transitions_follow_the_declared_graph(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)
    task = create_task(client, auth, task_organization.id)

    started = transition(client, auth, task["id"], "in_progress")
    assert started.status_code == 200

    same = transition(client, auth, task["id"], "in_progress")
    assert same.status_code == 409

    completed = transition(client, auth, task["id"], "completed")
    assert completed.status_code == 200

    # Desde un estado terminal solo se reabre; no se salta a en curso.
    illegal = transition(client, auth, task["id"], "in_progress", reason="No")
    assert illegal.status_code == 409
    assert "Invalid task transition" in illegal.json()["detail"]

    reopened = transition(client, auth, task["id"], "pending", reason="Faltó revisar")
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None


def test_blocking_cancelling_and_reopening_require_a_reason(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)
    task = create_task(client, auth, task_organization.id)

    without_reason = transition(client, auth, task["id"], "blocked")
    assert without_reason.status_code == 422

    blocked = transition(
        client,
        auth,
        task["id"],
        "blocked",
        reason="Falta la licencia de obra",
    )
    assert blocked.status_code == 200
    assert blocked.json()["blocked_reason"] == "Falta la licencia de obra"

    unblocked = transition(client, auth, task["id"], "in_progress")
    assert unblocked.status_code == 200
    # Al desbloquear, el motivo deja de aplicar y no puede quedarse pegado.
    assert unblocked.json()["blocked_reason"] is None

    cancelled = transition(
        client,
        auth,
        task["id"],
        "cancelled",
        reason="La resuelve la diputación",
    )
    assert cancelled.status_code == 200


def test_cancelling_and_reopening_need_manage_while_editing_needs_edit(
    client,
    make_user,
    task_organization,
    grant_permissions,
    superuser,
):
    task = create_task(client, headers_for(superuser), task_organization.id)

    editor = make_user()
    grant_permissions(editor, task_organization, ["tasks.view", "tasks.edit"])
    editor_auth = headers_for(editor)

    assert transition(client, editor_auth, task["id"], "in_progress").status_code == 200
    denied_cancel = transition(
        client,
        editor_auth,
        task["id"],
        "cancelled",
        reason="No procede",
    )
    assert denied_cancel.status_code == 403
    assert denied_cancel.json()["detail"] == "Permission required: tasks.manage"

    assert transition(client, editor_auth, task["id"], "completed").status_code == 200
    denied_reopen = transition(
        client,
        editor_auth,
        task["id"],
        "pending",
        reason="Revisar",
    )
    assert denied_reopen.status_code == 403

    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    assert (
        transition(
            client,
            headers_for(manager),
            task["id"],
            "pending",
            reason="Revisar",
        ).status_code
        == 200
    )


def test_a_closed_task_must_be_reopened_before_editing(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)
    task = create_task(client, auth, task_organization.id)
    transition(client, auth, task["id"], "in_progress")
    transition(client, auth, task["id"], "completed")

    denied = client.patch(
        f"/tasks/{task['id']}",
        json={"title": "Retoque a posteriori"},
        headers=auth,
    )

    assert denied.status_code == 409
    assert denied.json()["detail"] == "Reopen the task before editing it"


def test_every_change_leaves_an_event_in_the_history(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage", "staff.manage"])
    auth = headers_for(manager)
    worker = create_worker(client, auth, task_organization.id)
    task = create_task(client, auth, task_organization.id)

    client.patch(
        f"/tasks/{task['id']}",
        json={"title": "Título corregido", "assignee_worker_id": worker["id"]},
        headers=auth,
    )
    transition(client, auth, task["id"], "in_progress")

    detail = client.get(f"/tasks/{task['id']}", headers=auth).json()
    event_types = [event["event_type"] for event in detail["events"]]

    assert detail["assignee"]["full_name"] == worker["full_name"]
    # Del más reciente al más antiguo.
    assert event_types[-1] == "created"
    assert event_types[0] == "status_changed"
    assert "assigned" in event_types
    assert any(event["changed_fields"] == ["title"] for event in detail["events"])
    assert all(event["actor_id"] == manager.id for event in detail["events"])


def test_assignee_and_project_must_belong_to_the_organization(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    foreign_worker = create_worker(client, headers_for(superuser), other.id)
    foreign_project = Project(name=f"Proyecto {unique_suffix()}", organization_id=other.id)
    db.add(foreign_project)
    db.commit()

    manager = make_user()
    grant_permissions(manager, target, ["tasks.manage"])
    auth = headers_for(manager)

    wrong_worker = client.post(
        "/tasks",
        json=task_payload(target.id, assignee_worker_id=foreign_worker["id"]),
        headers=auth,
    )
    wrong_project = client.post(
        "/tasks",
        json=task_payload(target.id, project_id=foreign_project.id),
        headers=auth,
    )

    assert wrong_worker.status_code == 409
    assert (
        wrong_worker.json()["detail"]
        == "Staff worker does not belong to the organization"
    )
    assert wrong_project.status_code == 409
    assert (
        wrong_project.json()["detail"]
        == "Project does not belong to the organization"
    )


def test_archived_workers_cannot_take_tasks(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage", "staff.manage"])
    auth = headers_for(manager)
    worker = create_worker(client, auth, task_organization.id)
    client.patch(
        f"/staff/workers/{worker['id']}",
        json={"status": "archived"},
        headers=auth,
    )

    response = client.post(
        "/tasks",
        json=task_payload(task_organization.id, assignee_worker_id=worker["id"]),
        headers=auth,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Archived staff workers cannot take tasks"


def test_open_tasks_are_listed_by_due_date_with_undated_last(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)
    today = date.today()

    late = create_task(
        client, auth, task_organization.id, due_date=str(today + timedelta(days=30))
    )
    soon = create_task(
        client, auth, task_organization.id, due_date=str(today + timedelta(days=1))
    )
    undated = create_task(client, auth, task_organization.id)
    closed = create_task(client, auth, task_organization.id)
    transition(client, auth, closed["id"], "cancelled", reason="Duplicada")

    listed = client.get(
        "/tasks",
        params={"organization_id": task_organization.id},
        headers=auth,
    )
    with_closed = client.get(
        "/tasks",
        params={"organization_id": task_organization.id, "include_closed": True},
        headers=auth,
    )

    assert [item["id"] for item in listed.json()] == [
        soon["id"],
        late["id"],
        undated["id"],
    ]
    assert closed["id"] in {item["id"] for item in with_closed.json()}


def test_tasks_can_be_filtered_by_worker_and_unassigned(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage", "staff.manage"])
    auth = headers_for(manager)
    worker = create_worker(client, auth, task_organization.id)

    assigned = create_task(
        client,
        auth,
        task_organization.id,
        assignee_worker_id=worker["id"],
    )
    loose = create_task(client, auth, task_organization.id)

    by_worker = client.get(
        "/tasks",
        params={
            "organization_id": task_organization.id,
            "assignee_worker_id": worker["id"],
        },
        headers=auth,
    )
    without_owner = client.get(
        "/tasks",
        params={"organization_id": task_organization.id, "unassigned": True},
        headers=auth,
    )
    summary = client.get(
        "/tasks/summary",
        params={"organization_id": task_organization.id},
        headers=auth,
    ).json()

    assert [item["id"] for item in by_worker.json()] == [assigned["id"]]
    assert [item["id"] for item in without_owner.json()] == [loose["id"]]
    assert summary["unassigned"] == 1


def test_tasks_from_other_organizations_are_never_listed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    target = make_organization(name=f"Objetivo {unique_suffix()}")
    other = make_organization(name=f"Otra {unique_suffix()}")
    super_auth = headers_for(superuser)
    own = create_task(client, super_auth, target.id)
    foreign = create_task(client, super_auth, other.id)

    manager = make_user()
    grant_permissions(manager, target, ["tasks.manage"])
    auth = headers_for(manager)

    listed = client.get("/tasks", params={"organization_id": target.id}, headers=auth)
    foreign_detail = client.get(f"/tasks/{foreign['id']}", headers=auth)
    summary = client.get(
        "/tasks/summary",
        params={"organization_id": target.id},
        headers=auth,
    ).json()

    assert [item["id"] for item in listed.json()] == [own["id"]]
    assert foreign_detail.status_code == 403
    assert summary["total"] == 1


def test_paused_organization_keeps_the_roadmap_read_only(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    superuser,
):
    organization = make_organization(name=f"Pausada {unique_suffix()}")
    task = create_task(client, headers_for(superuser), organization.id)
    organization.status = "paused"
    db.commit()

    manager = make_user()
    grant_permissions(manager, organization, ["tasks.manage"])
    auth = headers_for(manager)

    listed = client.get(
        "/tasks",
        params={"organization_id": organization.id},
        headers=auth,
    )
    denied_edit = client.patch(
        f"/tasks/{task['id']}",
        json={"title": "Cambio en organización pausada"},
        headers=auth,
    )
    denied_transition = transition(client, auth, task["id"], "in_progress")

    assert listed.status_code == 200
    assert denied_edit.status_code == 409
    assert denied_transition.status_code == 409


def test_unknown_task_and_organization_are_reported_as_missing(
    client,
    make_user,
    task_organization,
    grant_permissions,
):
    manager = make_user()
    grant_permissions(manager, task_organization, ["tasks.manage"])
    auth = headers_for(manager)

    missing_task = client.get("/tasks/999999", headers=auth)
    missing_organization = client.get(
        "/tasks", params={"organization_id": 999999}, headers=auth
    )

    assert missing_task.status_code == 404
    assert missing_organization.status_code == 404
