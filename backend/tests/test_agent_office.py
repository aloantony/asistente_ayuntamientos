from conftest import headers_for

from app.requirements.models import Requirement


def test_agent_office_creates_and_runs_read_only_task(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization("Ayuntamiento de Prueba")
    user = make_user(full_name="Office User")
    grant_permissions(
        user,
        organization,
        [
            "agent_office.create",
            "agent_office.view",
            "agent_office.execute",
            "requirements.view",
        ],
    )

    response = client.post(
        "/agent-office/tasks",
        headers=headers_for(user),
        json={
            "organization_id": organization.id,
            "title": "Consultar necesidades visibles",
            "description": "Preparar un listado de necesidades visibles.",
            "department": "requirements",
            "requested_action": "list_requirements",
            "approval_policy": "never",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "approved"
    assert created["department"] == "requirements"
    assert created["requires_human_approval"] is False

    run_response = client.post(
        f"/agent-office/tasks/{created['id']}/run-inline",
        headers=headers_for(user),
    )

    assert run_response.status_code == 200
    finished = run_response.json()
    assert finished["status"] == "completed"
    assert finished["result"]["tool"] == "list_requirements"
    assert finished["result"]["ok"] is True
    assert finished["events"][-1]["event_type"] == "completed"


def test_agent_office_forces_approval_for_mutating_actions(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization("Ayuntamiento de Control")
    user = make_user(full_name="Reviewer")
    grant_permissions(
        user,
        organization,
        [
            "agent_office.create",
            "agent_office.view",
            "agent_office.approve",
            "agent_office.execute",
            "requirements.view",
            "requirements.create",
        ],
    )

    response = client.post(
        "/agent-office/tasks",
        headers=headers_for(user),
        json={
            "organization_id": organization.id,
            "title": "Crear necesidad de prueba",
            "description": "Crear un borrador de necesidad desde la oficina de agentes.",
            "department": "requirements",
            "requested_action": "create_requirement",
            "approval_policy": "never",
            "requires_human_approval": False,
            "input": {
                "title": "Necesidad desde oficina",
                "problem": "Hace falta verificar la delegación supervisada.",
            },
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "pending_approval"
    assert task["approval_policy"] == "before_execution"
    assert task["requires_human_approval"] is True

    blocked_run = client.post(
        f"/agent-office/tasks/{task['id']}/run-inline",
        headers=headers_for(user),
    )
    assert blocked_run.status_code == 409

    approval = client.patch(
        f"/agent-office/tasks/{task['id']}/approval",
        headers=headers_for(user),
        json={"decision": "approve", "notes": "Revisado."},
    )
    assert approval.status_code == 200
    assert approval.json()["status"] == "approved"

    run_response = client.post(
        f"/agent-office/tasks/{task['id']}/run-inline",
        headers=headers_for(user),
    )
    assert run_response.status_code == 200
    finished = run_response.json()
    assert finished["status"] == "completed"
    assert finished["result"]["tool"] == "create_requirement"
    assert finished["result"]["ok"] is True

    requirement = db.get(Requirement, finished["result"]["content"]["id"])
    assert requirement is not None
    assert requirement.status == "draft"
    assert requirement.source_type == "conversation"


def test_agent_office_hides_tasks_from_other_tenants(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization("Ayuntamiento Visible")
    other_organization = make_organization("Ayuntamiento Oculto")
    owner = make_user(full_name="Owner")
    outsider = make_user(full_name="Outsider")
    grant_permissions(owner, organization, ["agent_office.create", "agent_office.view"])
    grant_permissions(outsider, other_organization, ["agent_office.view"])

    response = client.post(
        "/agent-office/tasks",
        headers=headers_for(owner),
        json={
            "organization_id": organization.id,
            "title": "Tarea privada",
            "description": "No debe verse desde otro tenant.",
            "department": "projects",
            "requested_action": "list_projects",
            "approval_policy": "never",
        },
    )
    assert response.status_code == 201
    task_id = response.json()["id"]

    hidden = client.get(f"/agent-office/tasks/{task_id}", headers=headers_for(outsider))
    assert hidden.status_code == 404

    listing = client.get("/agent-office/tasks", headers=headers_for(outsider))
    assert listing.status_code == 200
    assert listing.json() == []
