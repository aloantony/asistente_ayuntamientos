import json

import pytest

from conftest import headers_for

from app.agent_office.models import AgentOfficeTask, AgentOfficeTaskEvent
from app.assistant.models import AssistantAdminFeedback
from app.assistant.tool_authorization import (
    AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
    claim_agent_office_tool_authorization,
    issue_agent_office_tool_authorization,
    tool_input_digest,
)
from app.assistant.tools import execute_tool, normalize_tool_input
from app.requirements.models import Requirement, RequirementMessage


def _make_authorizable_feedback_task(
    db,
    organization,
    user,
    *,
    status="approved",
    approved=True,
):
    task = AgentOfficeTask(
        organization_id=organization.id,
        title="Enviar feedback supervisado",
        description="Prueba de autorización tipada.",
        department="admin_feedback",
        requested_action="send_admin_feedback",
        status=status,
        approval_policy="before_execution",
        requires_human_approval=True,
        requested_by_id=user.id,
        approved_by_id=user.id if approved else None,
    )
    db.add(task)
    db.commit()
    return task


def _feedback_input(organization_id: int, *, title="Incidencia autorizada"):
    return {
        "organization_id": organization_id,
        "category": "bug",
        "title": title,
        "description": "Descripción exacta autorizada.",
    }


def _run_cross_organization_requirement_task(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    *,
    requested_action: str,
    task_input: dict,
):
    organization = make_organization("Ayuntamiento Auditor")
    other_organization = make_organization("Ayuntamiento Fuera de Alcance")
    user = make_user(full_name=f"Office Scope {requested_action}")
    grant_permissions(
        user,
        organization,
        [
            "agent_office.create",
            "agent_office.view",
            "agent_office.approve",
            "agent_office.execute",
            "requirements.view",
            "requirements.edit",
        ],
    )
    grant_permissions(
        user,
        other_organization,
        ["requirements.view", "requirements.edit"],
    )
    foreign_requirement = Requirement(
        organization_id=other_organization.id,
        title=f"Necesidad ajena para {requested_action}",
        problem="Contenido original fuera del alcance de la tarea.",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(foreign_requirement)
    db.commit()

    response = client.post(
        "/agent-office/tasks",
        headers=headers_for(user),
        json={
            "organization_id": organization.id,
            "title": f"Ejecutar {requested_action} fuera de alcance",
            "description": "No debe poder operar sobre una necesidad de otro tenant.",
            "department": "requirements",
            "requested_action": requested_action,
            "approval_policy": "never",
            "requires_human_approval": False,
            "input": {"requirement_id": foreign_requirement.id, **task_input},
        },
    )

    assert response.status_code == 201
    task = response.json()
    if task["status"] == "pending_approval":
        approval = client.patch(
            f"/agent-office/tasks/{task['id']}/approval",
            headers=headers_for(user),
            json={"decision": "approve", "notes": "Aprobación de prueba."},
        )
        assert approval.status_code == 200

    run_response = client.post(
        f"/agent-office/tasks/{task['id']}/run-inline",
        headers=headers_for(user),
    )

    assert run_response.status_code == 200
    return run_response.json(), foreign_requirement.id


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


@pytest.mark.parametrize("approval_policy", ["never", "after_draft"])
def test_agent_office_marks_unsuccessful_task_result_as_failed(
    client,
    make_user,
    make_organization,
    grant_permissions,
    monkeypatch,
    approval_policy,
):
    organization = make_organization("Ayuntamiento con fallo de agente")
    user = make_user(full_name="Office Failure User")
    grant_permissions(
        user,
        organization,
        [
            "agent_office.create",
            "agent_office.view",
            "agent_office.approve",
            "agent_office.execute",
            "requirements.view",
        ],
    )
    monkeypatch.setattr(
        "app.agent_office.service.execute_task_body",
        lambda *args, **kwargs: {
            "mode": "tool",
            "tool": "list_requirements",
            "ok": False,
            "content": "Error (503): dependency unavailable",
        },
    )
    response = client.post(
        "/agent-office/tasks",
        headers=headers_for(user),
        json={
            "organization_id": organization.id,
            "title": "Ejecutar una herramienta que falla",
            "description": "El resultado negativo debe cerrar la tarea como fallida.",
            "department": "requirements",
            "requested_action": "list_requirements",
            "approval_policy": approval_policy,
        },
    )
    assert response.status_code == 201
    task = response.json()
    if task["status"] == "pending_approval":
        approval = client.patch(
            f"/agent-office/tasks/{task['id']}/approval",
            headers=headers_for(user),
            json={"decision": "approve", "notes": "Aprobada para la regresión."},
        )
        assert approval.status_code == 200

    run_response = client.post(
        f"/agent-office/tasks/{task['id']}/run-inline",
        headers=headers_for(user),
    )

    assert run_response.status_code == 200
    finished = run_response.json()
    assert finished["status"] == "failed"
    assert finished["result"] == {
        "mode": "tool",
        "tool": "list_requirements",
        "ok": False,
        "content": "Error (503): dependency unavailable",
    }
    assert finished["error_message"] == "Error (503): dependency unavailable"
    assert finished["events"][-1]["event_type"] == "failed"
    assert finished["events"][-1]["message"] == finished["error_message"]
    assert finished["events"][-1]["payload"] == finished["result"]


def test_agent_office_canonicalizes_tool_input_organization_scope(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization("Ayuntamiento Seguro")
    other_organization = make_organization("Ayuntamiento Ajeno")
    user = make_user(full_name="Office Scope User")
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
    grant_permissions(user, other_organization, ["requirements.view"])
    leaked_requirement = Requirement(
        organization_id=other_organization.id,
        title="Dato de otro ayuntamiento",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(leaked_requirement)
    db.commit()

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
            "input": {"organization_id": other_organization.id},
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["organization_id"] == organization.id
    assert created["input"]["organization_id"] == organization.id

    run_response = client.post(
        f"/agent-office/tasks/{created['id']}/run-inline",
        headers=headers_for(user),
    )

    assert run_response.status_code == 200
    finished = run_response.json()
    assert finished["result"]["tool"] == "list_requirements"
    assert finished["result"]["ok"] is True
    assert finished["result"]["content"] == []


def test_agent_office_rejects_get_requirement_outside_task_organization(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    finished, _requirement_id = _run_cross_organization_requirement_task(
        client,
        db,
        make_user,
        make_organization,
        grant_permissions,
        requested_action="get_requirement",
        task_input={},
    )

    assert finished["status"] == "failed"
    assert "outside task organization" in finished["error_message"]
    assert finished["result"] == {}


def test_agent_office_rejects_update_requirement_outside_task_organization(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    finished, requirement_id = _run_cross_organization_requirement_task(
        client,
        db,
        make_user,
        make_organization,
        grant_permissions,
        requested_action="update_requirement",
        task_input={"problem": "Actualización que no debe aplicarse."},
    )

    assert finished["status"] == "failed"
    assert "outside task organization" in finished["error_message"]
    db.expire_all()
    requirement = db.get(Requirement, requirement_id)
    assert requirement is not None
    assert requirement.problem == "Contenido original fuera del alcance de la tarea."


def test_agent_office_rejects_add_requirement_message_outside_task_organization(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    finished, requirement_id = _run_cross_organization_requirement_task(
        client,
        db,
        make_user,
        make_organization,
        grant_permissions,
        requested_action="add_requirement_message",
        task_input={"body": "Nota que no debe crearse."},
    )

    assert finished["status"] == "failed"
    assert "outside task organization" in finished["error_message"]
    assert db.query(RequirementMessage).filter_by(requirement_id=requirement_id).count() == 0


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


def test_agent_office_authorization_rejects_unapproved_task(
    db,
    make_user,
    make_organization,
):
    organization = make_organization("Ayuntamiento sin aprobación")
    user = make_user(is_superuser=True)
    task = _make_authorizable_feedback_task(
        db,
        organization,
        user,
        status="pending_approval",
        approved=False,
    )
    tool_input = _feedback_input(organization.id)
    canonical_input = normalize_tool_input(
        db,
        user,
        "send_admin_feedback",
        tool_input,
    )

    with pytest.raises(ValueError, match="cannot authorize tools"):
        issue_agent_office_tool_authorization(
            db,
            task_id=task.id,
            actor_id=user.id,
            tool="send_admin_feedback",
            input_digest=tool_input_digest(
                "send_admin_feedback",
                canonical_input,
            ),
        )


def test_agent_office_authorization_rejects_inactive_actor(
    db,
    make_user,
    make_organization,
):
    organization = make_organization("Ayuntamiento con actor inactivo")
    user = make_user(is_superuser=True, is_active=False)
    task = _make_authorizable_feedback_task(db, organization, user)
    canonical_input = normalize_tool_input(
        db,
        user,
        "send_admin_feedback",
        _feedback_input(organization.id),
    )

    with pytest.raises(ValueError, match="actor is not active"):
        issue_agent_office_tool_authorization(
            db,
            task_id=task.id,
            actor_id=user.id,
            tool="send_admin_feedback",
            input_digest=tool_input_digest(
                "send_admin_feedback",
                canonical_input,
            ),
        )


def test_agent_office_authorization_allows_exact_payload_once(
    db,
    make_user,
    make_organization,
):
    organization = make_organization("Ayuntamiento con autorización exacta")
    user = make_user(is_superuser=True)
    task = _make_authorizable_feedback_task(db, organization, user)
    tool_input = _feedback_input(organization.id)
    canonical_input = normalize_tool_input(
        db,
        user,
        "send_admin_feedback",
        tool_input,
    )
    authorization = issue_agent_office_tool_authorization(
        db,
        task_id=task.id,
        actor_id=user.id,
        tool="send_admin_feedback",
        input_digest=tool_input_digest("send_admin_feedback", canonical_input),
    )

    first = execute_tool(
        db,
        user,
        "send_admin_feedback",
        tool_input,
        authorization=authorization,
    )
    replay = execute_tool(
        db,
        user,
        "send_admin_feedback",
        tool_input,
        authorization=authorization,
    )

    assert first.ok is True
    assert replay.ok is False
    assert "inválida o consumida" in replay.content
    assert db.query(AssistantAdminFeedback).count() == 1


def test_agent_office_authorization_rejects_different_payload_and_is_consumed(
    db,
    make_user,
    make_organization,
):
    organization = make_organization("Ayuntamiento con payload protegido")
    user = make_user(is_superuser=True)
    task = _make_authorizable_feedback_task(db, organization, user)
    exact_input = _feedback_input(organization.id)
    canonical_input = normalize_tool_input(
        db,
        user,
        "send_admin_feedback",
        exact_input,
    )
    authorization = issue_agent_office_tool_authorization(
        db,
        task_id=task.id,
        actor_id=user.id,
        tool="send_admin_feedback",
        input_digest=tool_input_digest("send_admin_feedback", canonical_input),
    )

    changed = execute_tool(
        db,
        user,
        "send_admin_feedback",
        _feedback_input(organization.id, title="Payload distinto"),
        authorization=authorization,
    )
    exact_replay = execute_tool(
        db,
        user,
        "send_admin_feedback",
        exact_input,
        authorization=authorization,
    )

    assert changed.ok is False
    assert "efectos actuales" in changed.content
    assert exact_replay.ok is False
    assert "inválida o consumida" in exact_replay.content
    assert db.query(AssistantAdminFeedback).count() == 0


def test_agent_office_claim_history_has_no_replay_window_after_100_events(
    db,
    make_user,
    make_organization,
):
    organization = make_organization("Ayuntamiento sin ventana de replay")
    user = make_user(is_superuser=True)
    task = _make_authorizable_feedback_task(db, organization, user)
    canonical_input = normalize_tool_input(
        db,
        user,
        "send_admin_feedback",
        _feedback_input(organization.id),
    )
    authorization = issue_agent_office_tool_authorization(
        db,
        task_id=task.id,
        actor_id=user.id,
        tool="send_admin_feedback",
        input_digest=tool_input_digest("send_admin_feedback", canonical_input),
    )
    assert claim_agent_office_tool_authorization(db, authorization) is True
    db.add_all(
        [
            AgentOfficeTaskEvent(
                task_id=task.id,
                event_type=AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
                message="Evento posterior de prueba.",
                payload_json=json.dumps(
                    {"token_digest": f"posterior-{index}", "accepted": True}
                ),
                created_by_id=user.id,
            )
            for index in range(101)
        ]
    )
    db.commit()

    assert claim_agent_office_tool_authorization(db, authorization) is False


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


def test_agent_office_without_action_uses_front_desk_instead_of_keyword_routing(
    client,
    make_user,
    make_organization,
    grant_permissions,
):
    organization = make_organization("Ayuntamiento Sin Marcadores")
    user = make_user(full_name="Front Desk User")
    grant_permissions(user, organization, ["agent_office.create", "agent_office.view"])

    response = client.post(
        "/agent-office/tasks",
        headers=headers_for(user),
        json={
            "organization_id": organization.id,
            "title": "Revisar ordenanza mencionada en una petición vecinal",
            "description": (
                "El texto libre puede mencionar ordenanza, mapa o necesidad, "
                "pero sin acción estructurada debe entrar por triage."
            ),
        },
    )

    assert response.status_code == 201
    task = response.json()
    assert task["department"] == "front_desk"
    assert task["requested_action"] == "triage"
    assert task["status"] == "pending_approval"
