import json
import multiprocessing
import socket
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import headers_for

from app.agent_office import service as agent_office_service
from app.agent_office.models import AgentOfficeTask, AgentOfficeTaskEvent
from app.agent_office.service import (
    approve_or_cancel_task,
    mark_task_queued,
    run_agent_office_task,
)
from app.assistant.models import AssistantAdminFeedback
from app.assistant.tool_authorization import (
    AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
    issue_agent_office_tool_authorization,
    tool_input_digest,
)
from app.assistant.tools import (
    TOOL_CATALOG,
    PreparedOrdinanceSearchEmbedding,
    execute_tool,
    normalize_tool_input,
)
from app.core.config import Settings, settings
from app.organizations.models import Organization
from app.ordinances import embeddings as embeddings_module
from app.ordinances.embeddings import (
    EMBEDDING_WORKER_MAX_CLEANUP_SECONDS,
    EmbeddingWorkerCleanupError,
    EmbeddingsUnavailableError,
    _ExternalEmbeddingConfig,
    _embedding_worker_entry,
    _run_supervised_external_embedding,
    supervised_embedding_claim_lease_seconds,
)
from app.requirements.models import Requirement, RequirementMessage
from app.users.models import User


def _slow_embedding_operation(text, config, deadline):
    del text, config, deadline
    while True:
        time.sleep(0.02)


def _successful_embedding_operation(text, config, deadline):
    del text, config, deadline
    return [0.25, -0.5]


def _partial_embedding_ipc_worker(
    send_socket,
    text,
    config,
    deadline,
    operation,
):
    del text, config, deadline, operation
    try:
        for byte in b'{"status":"ok","vector":[0.25':
            send_socket.send(bytes([byte]))
            time.sleep(0.02)
        time.sleep(5)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            send_socket.close()
        except OSError:
            pass


def _record_late_embedding_operation(event, text, config, deadline):
    del text, config, deadline
    event.set()
    return [1.0]


def _make_authorizable_feedback_task(
    db,
    organization,
    user,
    *,
    status="running",
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
    db.flush()
    if status == "running":
        db.add(
            AgentOfficeTaskEvent(
                task_id=task.id,
                event_type="started",
                message="Intento de ejecución de prueba.",
                created_by_id=user.id,
            )
        )
    db.commit()
    return task


def _feedback_input(organization_id: int, *, title="Incidencia autorizada"):
    return {
        "organization_id": organization_id,
        "category": "bug",
        "title": title,
        "description": "Descripción exacta autorizada.",
    }


def _seed_engine_feedback_task(engine, *, status: str) -> dict[str, int]:
    suffix = uuid.uuid4().hex
    with Session(engine, expire_on_commit=False) as seed_db:
        user = User(
            email=f"agent-race-{suffix}@example.com",
            hashed_password="not-used",
            full_name="Agent Office Race",
            is_active=True,
            is_superuser=True,
        )
        organization = Organization(name=f"Agent race org {suffix}")
        seed_db.add_all([user, organization])
        seed_db.flush()
        task = AgentOfficeTask(
            organization_id=organization.id,
            title=f"Incidencia concurrente {suffix}",
            description="Descripción concurrente exacta.",
            department="admin_feedback",
            requested_action="send_admin_feedback",
            status=status,
            approval_policy="before_execution",
            requires_human_approval=True,
            input_json=json.dumps(
                {
                    "organization_id": organization.id,
                    "category": "bug",
                    "title": f"Incidencia concurrente {suffix}",
                    "description": "Descripción concurrente exacta.",
                    "priority": "medium",
                }
            ),
            requested_by_id=user.id,
            approved_by_id=user.id,
        )
        seed_db.add(task)
        seed_db.flush()
        if status == "running":
            seed_db.add(
                AgentOfficeTaskEvent(
                    task_id=task.id,
                    event_type="started",
                    message="Committed execution attempt for recovery.",
                    created_by_id=user.id,
                )
            )
        seed_db.commit()
        return {
            "task_id": task.id,
            "user_id": user.id,
            "organization_id": organization.id,
        }


def _cleanup_engine_feedback_task(engine, ids: dict[str, int]) -> None:
    with Session(engine) as cleanup_db:
        task = cleanup_db.get(AgentOfficeTask, ids["task_id"])
        if task is not None:
            cleanup_db.delete(task)
        for feedback in cleanup_db.scalars(
            select(AssistantAdminFeedback).where(
                AssistantAdminFeedback.organization_id == ids["organization_id"]
            )
        ):
            cleanup_db.delete(feedback)
        cleanup_db.commit()
        user = cleanup_db.get(User, ids["user_id"])
        if user is not None:
            cleanup_db.delete(user)
        organization = cleanup_db.get(
            Organization,
            ids["organization_id"],
        )
        if organization is not None:
            cleanup_db.delete(organization)
        cleanup_db.commit()


def _seed_engine_ordinance_task(engine) -> dict[str, int | str]:
    suffix = uuid.uuid4().hex
    query = f"dominio público viario {suffix}"
    with Session(engine, expire_on_commit=False) as seed_db:
        user = User(
            email=f"agent-ordinance-{suffix}@example.com",
            hashed_password="not-used",
            full_name="Agent Office Ordinance",
            is_active=True,
            is_superuser=True,
        )
        organization = Organization(name=f"Agent ordinance org {suffix}")
        seed_db.add_all([user, organization])
        seed_db.flush()
        task = AgentOfficeTask(
            organization_id=organization.id,
            title=f"Buscar ordenanzas {suffix}",
            description=query,
            department="ordinances",
            requested_action="semantic_search_ordinances",
            status="queued",
            approval_policy="never",
            requires_human_approval=False,
            input_json=json.dumps({"query": query, "limit": 3}),
            requested_by_id=user.id,
        )
        seed_db.add(task)
        seed_db.commit()
        return {
            "task_id": task.id,
            "user_id": user.id,
            "organization_id": organization.id,
            "query": query,
        }


def _cleanup_engine_ordinance_task(
    engine,
    ids: dict[str, int | str],
) -> None:
    with Session(engine) as cleanup_db:
        task = cleanup_db.get(AgentOfficeTask, ids["task_id"])
        if task is not None:
            cleanup_db.delete(task)
        cleanup_db.commit()
        user = cleanup_db.get(User, ids["user_id"])
        if user is not None:
            cleanup_db.delete(user)
        organization = cleanup_db.get(Organization, ids["organization_id"])
        if organization is not None:
            cleanup_db.delete(organization)
        cleanup_db.commit()


def _embedding_test_config(timeout_seconds: float) -> _ExternalEmbeddingConfig:
    return _ExternalEmbeddingConfig(
        base_url="https://embeddings.invalid/v1",
        api_key="test-secret",
        model="test-embedding",
        timeout_seconds=timeout_seconds,
    )


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


def test_agent_office_authorization_replays_completed_result_without_new_effect(
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
    assert replay == first
    assert db.query(AssistantAdminFeedback).count() == 1


def test_agent_office_authorization_rejects_different_payload_but_remains_retriable(
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
    db.commit()

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
    assert exact_replay.ok is True
    assert db.query(AssistantAdminFeedback).count() == 1


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
    first = execute_tool(
        db,
        user,
        "send_admin_feedback",
        _feedback_input(organization.id),
        authorization=authorization,
    )
    assert first.ok is True
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

    replay = execute_tool(
        db,
        user,
        "send_admin_feedback",
        _feedback_input(organization.id),
        authorization=authorization,
    )
    assert replay == first
    assert db.query(AssistantAdminFeedback).count() == 1


def test_two_agent_workers_serialize_one_task_effect(engine):
    ids = _seed_engine_feedback_task(engine, status="queued")
    barrier = threading.Barrier(2)

    def run_worker() -> tuple[str, dict]:
        with Session(engine, expire_on_commit=False) as candidate_db:
            barrier.wait(timeout=15)
            task = run_agent_office_task(ids["task_id"], db=candidate_db)
            return task.status, task.result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run_worker(), range(2)))

        assert [status for status, _ in results] == ["completed", "completed"]
        assert results[0][1] == results[1][1]
        with Session(engine) as verification_db:
            feedback = verification_db.scalars(
                select(AssistantAdminFeedback).where(
                    AssistantAdminFeedback.organization_id
                    == ids["organization_id"]
                )
            ).all()
            events = verification_db.scalars(
                select(AgentOfficeTaskEvent).where(
                    AgentOfficeTaskEvent.task_id == ids["task_id"]
                )
            ).all()
            event_types = [event.event_type for event in events]
            assert len(feedback) == 1
            assert event_types.count("started") == 1
            assert event_types.count("tool_authorization_issued") == 1
            assert event_types.count("tool_authorization_claimed") == 1
            assert event_types.count("tool_execution_completed") == 1
            assert event_types.count("completed") == 1
    finally:
        _cleanup_engine_feedback_task(engine, ids)


@pytest.mark.parametrize(
    "timeout",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -1.0,
        0.0,
        0.099,
        120.001,
        1_000_000.0,
    ],
)
def test_embeddings_timeout_rejects_nonfinite_and_out_of_bounds(timeout):
    with pytest.raises(ValidationError, match="embeddings_timeout_seconds"):
        Settings(
            embeddings_timeout_seconds=timeout,
            _env_file=None,
        )


@pytest.mark.parametrize("timeout", [0.1, 120.0])
def test_embeddings_timeout_accepts_bounded_endpoints(timeout):
    configured = Settings(
        embeddings_timeout_seconds=timeout,
        _env_file=None,
    )
    assert configured.embeddings_timeout_seconds == timeout


@pytest.mark.parametrize("workers", [0, -1, 33, 1_000_000])
def test_embeddings_worker_capacity_rejects_out_of_bounds(workers):
    with pytest.raises(
        ValidationError,
        match="embeddings_max_concurrent_workers",
    ):
        Settings(
            embeddings_max_concurrent_workers=workers,
            _env_file=None,
        )


def test_supervised_embedding_enforces_total_deadline_and_reaps_child():
    before = {process.pid for process in multiprocessing.active_children()}
    timeout = 5.0
    started_at = monotonic()

    with pytest.raises(EmbeddingsUnavailableError):
        _run_supervised_external_embedding(
            "consulta lenta",
            _embedding_test_config(timeout),
            worker_operation=_slow_embedding_operation,
        )

    elapsed = monotonic() - started_at
    assert elapsed <= timeout + EMBEDDING_WORKER_MAX_CLEANUP_SECONDS + 1.0
    after = {process.pid for process in multiprocessing.active_children()}
    assert after <= before


def test_supervised_embedding_stops_slow_trickle_http_before_socket_timeout():
    request_started = threading.Event()

    class SlowTrickleHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(content_length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000")
            self.end_headers()
            request_started.set()
            try:
                while True:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def log_message(self, format, *args):
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowTrickleHandler)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="embedding-slow-trickle-test-server",
    )
    server_thread.start()
    timeout = 5.0
    config = _ExternalEmbeddingConfig(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key="test-secret",
        model="test-embedding",
        timeout_seconds=timeout,
    )
    started_at = monotonic()
    try:
        with pytest.raises(EmbeddingsUnavailableError):
            _run_supervised_external_embedding(
                "consulta HTTP lenta",
                config,
            )
        assert request_started.wait(timeout=1)
        elapsed = monotonic() - started_at
        assert elapsed <= timeout + EMBEDDING_WORKER_MAX_CLEANUP_SECONDS + 1.0
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        assert server_thread.is_alive() is False


def test_supervised_embedding_spawn_path_accepts_complete_bounded_ipc():
    vector = _run_supervised_external_embedding(
        "consulta completa con spawn",
        _embedding_test_config(5.0),
        worker_operation=_successful_embedding_operation,
    )
    assert vector == [0.25, -0.5]


def test_semantic_tool_without_prepared_embedding_uses_supervisor(
    db,
    make_user,
    monkeypatch,
):
    user = make_user(is_superuser=True)
    calls: list[str] = []

    def deadline_embedding(text: str):
        calls.append(text)
        raise EmbeddingsUnavailableError("supervised total deadline")

    monkeypatch.setattr(
        "app.assistant.tools.embed_text_supervised",
        deadline_embedding,
    )

    with pytest.raises(
        EmbeddingsUnavailableError,
        match="supervised total deadline",
    ):
        execute_tool(
            db,
            user,
            "semantic_search_ordinances",
            {"query": "dominio público viario"},
        )

    assert calls == ["dominio público viario"]


def test_supervised_embedding_times_out_partial_slow_ipc_and_reaps_child():
    before = {process.pid for process in multiprocessing.active_children()}
    timeout = 5.0
    started_at = monotonic()

    with pytest.raises(EmbeddingsUnavailableError):
        _run_supervised_external_embedding(
            "consulta con IPC parcial",
            _embedding_test_config(timeout),
            worker_entry=_partial_embedding_ipc_worker,
            worker_operation=_successful_embedding_operation,
        )

    elapsed = monotonic() - started_at
    assert elapsed <= timeout + EMBEDDING_WORKER_MAX_CLEANUP_SECONDS + 1.0
    after = {process.pid for process in multiprocessing.active_children()}
    assert after <= before


def test_embedding_child_scheduled_after_deadline_never_calls_provider():
    process_context = multiprocessing.get_context("fork")
    provider_called = process_context.Event()
    receive_socket, send_socket = socket.socketpair(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )
    process = process_context.Process(
        target=_embedding_worker_entry,
        args=(
            send_socket,
            "consulta vencida",
            _embedding_test_config(0.1),
            time.time() - 1.0,
            partial(_record_late_embedding_operation, provider_called),
        ),
        daemon=True,
    )
    try:
        process.start()
        send_socket.close()
        process.join(timeout=2)
        assert not process.is_alive()
        assert provider_called.is_set() is False
    finally:
        try:
            send_socket.close()
        except OSError:
            pass
        receive_socket.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()


def test_blocked_process_start_is_bounded_and_retains_admission_slot(
    monkeypatch,
):
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(
        embeddings_module,
        "_EMBEDDING_ADMISSION",
        admission,
    )
    start_entered = threading.Event()
    release_start = threading.Event()
    cleanup_done = threading.Event()
    provider_calls = 0

    def provider_operation(text, config, provider_deadline_epoch):
        nonlocal provider_calls
        del text, config, provider_deadline_epoch
        provider_calls += 1
        return [1.0]

    class BlockingStartProcess:
        def __init__(self, *, target, args, daemon):
            del daemon
            self._target = target
            self._args = args
            self._alive = False

        def start(self):
            start_entered.set()
            assert release_start.wait(timeout=5)
            self._alive = True
            self._target(*self._args)
            self._alive = False

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            del timeout

        def terminate(self):
            self._alive = False

        def kill(self):
            self._alive = False

        def close(self):
            cleanup_done.set()

    class BlockingStartContext:
        @staticmethod
        def Process(*, target, args, daemon):
            return BlockingStartProcess(
                target=target,
                args=args,
                daemon=daemon,
            )

    started_at = monotonic()
    try:
        with pytest.raises(
            EmbeddingWorkerCleanupError,
            match="launch exceeded",
        ):
            _run_supervised_external_embedding(
                "consulta con start bloqueado",
                _embedding_test_config(0.1),
                worker_operation=provider_operation,
                process_context=BlockingStartContext(),
            )
        assert monotonic() - started_at < 1.0
        assert start_entered.is_set()
        assert provider_calls == 0
        # Ownership moved to the still-blocked launcher; capacity cannot be
        # reused to create an unbounded number of launch threads.
        assert admission.acquire(blocking=False) is False
    finally:
        release_start.set()

    assert cleanup_done.wait(timeout=2)
    assert provider_calls == 0
    assert admission.acquire(timeout=1)
    admission.release()


def test_late_worker_cleanup_failure_keeps_admission_slot_quarantined(
    monkeypatch,
):
    release_called = threading.Event()

    class RecordingAdmission:
        def __init__(self):
            self._semaphore = threading.BoundedSemaphore(1)

        def acquire(self, *args, **kwargs):
            return self._semaphore.acquire(*args, **kwargs)

        def release(self):
            release_called.set()
            self._semaphore.release()

    admission = RecordingAdmission()
    monkeypatch.setattr(
        embeddings_module,
        "_EMBEDDING_ADMISSION",
        admission,
    )
    start_entered = threading.Event()
    release_start = threading.Event()
    cleanup_attempted = threading.Event()

    class UncleanableLateProcess:
        def __init__(self, *, target, args, daemon):
            del daemon
            self._target = target
            self._args = args

        def start(self):
            start_entered.set()
            assert release_start.wait(timeout=5)
            self._target(*self._args)

        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def join(timeout=None):
            del timeout

        @staticmethod
        def close():
            cleanup_attempted.set()
            raise RuntimeError("process cleanup remains indeterminate")

    class UncleanableLateContext:
        @staticmethod
        def Process(*, target, args, daemon):
            return UncleanableLateProcess(
                target=target,
                args=args,
                daemon=daemon,
            )

    try:
        with pytest.raises(EmbeddingWorkerCleanupError, match="launch exceeded"):
            _run_supervised_external_embedding(
                "consulta con limpieza indeterminada",
                _embedding_test_config(0.1),
                worker_operation=_successful_embedding_operation,
                process_context=UncleanableLateContext(),
            )
        assert start_entered.is_set()
    finally:
        release_start.set()

    assert cleanup_attempted.wait(timeout=2)
    assert release_called.wait(timeout=0.2) is False
    assert admission.acquire(blocking=False) is False


def test_embedding_claim_lease_outlives_deadline_and_cleanup():
    for timeout in (0.1, 60.0, 120.0):
        lease = supervised_embedding_claim_lease_seconds(timeout)
        assert lease > timeout + EMBEDDING_WORKER_MAX_CLEANUP_SECONDS


def test_worker_resuming_after_stale_claim_never_gets_fresh_provider_deadline(
    engine,
    monkeypatch,
):
    ids = _seed_engine_ordinance_task(engine)
    first_worker_paused = threading.Event()
    release_first_worker = threading.Event()
    prepare_lock = threading.Lock()
    prepare_calls = 0
    provider_calls = 0
    original_prepare = agent_office_service.prepare_ordinance_search_embedding

    monkeypatch.setattr(settings, "embeddings_timeout_seconds", 0.5)
    monkeypatch.setattr(
        agent_office_service,
        "supervised_embedding_cleanup_margin_seconds",
        lambda: 0.1,
    )

    def deadline_bound_embedding(text, *, provider_deadline_at=None):
        nonlocal provider_calls
        assert provider_deadline_at is not None
        if datetime.now(timezone.utc) >= provider_deadline_at:
            raise EmbeddingsUnavailableError("durable provider deadline expired")
        provider_calls += 1
        return None, "test-disabled", "disabled"

    monkeypatch.setattr(
        "app.assistant.tools.embed_text_supervised",
        deadline_bound_embedding,
    )

    def pause_first_prepare(
        tool_input,
        *,
        provider_deadline_at=None,
    ):
        nonlocal prepare_calls
        with prepare_lock:
            prepare_calls += 1
            call_number = prepare_calls
        if call_number == 1:
            first_worker_paused.set()
            assert release_first_worker.wait(timeout=5)
        return original_prepare(
            tool_input,
            provider_deadline_at=provider_deadline_at,
        )

    monkeypatch.setattr(
        agent_office_service,
        "prepare_ordinance_search_embedding",
        pause_first_prepare,
    )

    def run_worker() -> str:
        with Session(engine, expire_on_commit=False) as worker_db:
            return run_agent_office_task(
                int(ids["task_id"]),
                db=worker_db,
            ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stale_worker = executor.submit(run_worker)
            assert first_worker_paused.wait(timeout=5)
            time.sleep(0.75)
            takeover_worker = executor.submit(run_worker)
            takeover_status = takeover_worker.result(timeout=5)
            if takeover_status != "completed":
                with Session(engine) as failure_db:
                    failed_task = failure_db.get(
                        AgentOfficeTask,
                        int(ids["task_id"]),
                    )
                    failure_detail = (
                        failed_task.error_message if failed_task else None
                    )
                raise AssertionError(
                    f"takeover status={takeover_status}: {failure_detail}"
                )
            release_first_worker.set()
            assert stale_worker.result(timeout=5) == "completed"

        assert provider_calls == 1
        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, int(ids["task_id"]))
            assert task is not None
            claims = [
                event
                for event in task.events
                if event.event_type == "external_read_claimed"
            ]
            assert len(claims) == 2
            for claim in claims:
                provider_deadline = datetime.fromisoformat(
                    claim.payload["provider_deadline_at"]
                )
                lease_expiry = datetime.fromisoformat(
                    claim.payload["lease_expires_at"]
                )
                assert provider_deadline < lease_expiry
            assert [event.event_type for event in task.events].count(
                "completed"
            ) == 1
    finally:
        release_first_worker.set()
        _cleanup_engine_ordinance_task(engine, ids)


def test_stale_cleanup_failure_quarantines_newer_external_claim(
    engine,
    monkeypatch,
):
    ids = _seed_engine_ordinance_task(engine)
    first_worker_paused = threading.Event()
    second_worker_started = threading.Event()
    release_first_worker = threading.Event()
    release_second_worker = threading.Event()
    prepare_lock = threading.Lock()
    prepare_calls = 0

    monkeypatch.setattr(settings, "embeddings_timeout_seconds", 0.1)
    monkeypatch.setattr(
        agent_office_service,
        "supervised_embedding_cleanup_margin_seconds",
        lambda: 0.1,
    )

    def controlled_prepare(tool_input, *, provider_deadline_at=None):
        nonlocal prepare_calls
        del tool_input, provider_deadline_at
        with prepare_lock:
            prepare_calls += 1
            call_number = prepare_calls
        if call_number == 1:
            first_worker_paused.set()
            assert release_first_worker.wait(timeout=5)
            raise EmbeddingWorkerCleanupError(
                "stale worker cleanup is indeterminate"
            )
        second_worker_started.set()
        assert release_second_worker.wait(timeout=5)
        return PreparedOrdinanceSearchEmbedding(
            query=str(ids["query"]),
            vector=None,
            model="test-disabled",
            status="disabled",
        )

    monkeypatch.setattr(
        agent_office_service,
        "prepare_ordinance_search_embedding",
        controlled_prepare,
    )

    def run_worker() -> str:
        with Session(engine, expire_on_commit=False) as worker_db:
            return run_agent_office_task(
                int(ids["task_id"]),
                db=worker_db,
            ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stale_worker = executor.submit(run_worker)
            assert first_worker_paused.wait(timeout=5)
            time.sleep(0.25)
            takeover_worker = executor.submit(run_worker)
            assert second_worker_started.wait(timeout=5)
            # Even if the takeover completes first, a late cleanup failure
            # from the stale worker must invalidate that result durably.
            release_second_worker.set()
            assert takeover_worker.result(timeout=5) == "completed"
            release_first_worker.set()
            assert stale_worker.result(timeout=5) == "failed"

        with Session(engine, expire_on_commit=False) as verification_db:
            task = verification_db.get(AgentOfficeTask, int(ids["task_id"]))
            user = verification_db.get(User, int(ids["user_id"]))
            assert task is not None
            assert user is not None
            event_types = [event.event_type for event in task.events]
            assert event_types.count("external_read_claimed") == 2
            assert event_types.count("external_read_quarantined") == 1
            assert event_types.count("failed") == 1
            assert task.status == "failed"
            assert event_types[-3:] == [
                "completed",
                "external_read_quarantined",
                "failed",
            ]
            with pytest.raises(HTTPException) as rejected_retry:
                mark_task_queued(verification_db, user, task)
            assert rejected_retry.value.status_code == 409
            verification_db.rollback()
    finally:
        release_first_worker.set()
        release_second_worker.set()
        _cleanup_engine_ordinance_task(engine, ids)


def test_indeterminate_embedding_cleanup_quarantines_agent_task_retry(
    engine,
    monkeypatch,
):
    ids = _seed_engine_ordinance_task(engine)
    provider_calls = 0

    def indeterminate_cleanup(tool_input, *, provider_deadline_at=None):
        nonlocal provider_calls
        del tool_input, provider_deadline_at
        provider_calls += 1
        raise EmbeddingWorkerCleanupError("cleanup could not be confirmed")

    monkeypatch.setattr(
        "app.agent_office.service.prepare_ordinance_search_embedding",
        indeterminate_cleanup,
    )

    try:
        with Session(engine, expire_on_commit=False) as worker_db:
            task = run_agent_office_task(int(ids["task_id"]), db=worker_db)
            assert task.status == "failed"

        with Session(engine, expire_on_commit=False) as retry_db:
            task = retry_db.get(AgentOfficeTask, int(ids["task_id"]))
            user = retry_db.get(User, int(ids["user_id"]))
            assert task is not None
            assert user is not None
            event_types = [event.event_type for event in task.events]
            assert event_types.count("started") == 1
            assert event_types.count("external_read_claimed") == 1
            assert event_types.count("external_read_quarantined") == 1
            assert event_types.count("failed") == 1
            with pytest.raises(HTTPException) as rejected_retry:
                mark_task_queued(retry_db, user, task)
            assert rejected_retry.value.status_code == 409
            retry_db.rollback()

        assert provider_calls == 1
    finally:
        _cleanup_engine_ordinance_task(engine, ids)


def test_external_ordinance_provider_does_not_block_task_cancellation(
    engine,
    monkeypatch,
):
    ids = _seed_engine_ordinance_task(engine)
    provider_started = threading.Event()
    release_provider = threading.Event()

    def blocked_embedding(
        tool_input: dict,
        *,
        provider_deadline_at=None,
    ) -> PreparedOrdinanceSearchEmbedding:
        del provider_deadline_at
        provider_started.set()
        assert release_provider.wait(timeout=15)
        return PreparedOrdinanceSearchEmbedding(
            query=str(tool_input["query"]),
            vector=None,
            model="test-disabled",
            status="disabled",
        )

    monkeypatch.setattr(
        "app.agent_office.service.prepare_ordinance_search_embedding",
        blocked_embedding,
    )

    def run_worker() -> str:
        with Session(engine, expire_on_commit=False) as worker_db:
            return run_agent_office_task(
                int(ids["task_id"]),
                db=worker_db,
            ).status

    def cancel_task() -> str:
        with Session(engine, expire_on_commit=False) as cancellation_db:
            user = cancellation_db.get(User, int(ids["user_id"]))
            task = cancellation_db.get(
                AgentOfficeTask,
                int(ids["task_id"]),
            )
            assert user is not None
            assert task is not None
            return approve_or_cancel_task(
                cancellation_db,
                user,
                task,
                decision="cancel",
                notes="Cancel while the embeddings provider is blocked.",
            ).status

    def attempt_approval() -> int:
        with Session(engine, expire_on_commit=False) as approval_db:
            user = approval_db.get(User, int(ids["user_id"]))
            task = approval_db.get(AgentOfficeTask, int(ids["task_id"]))
            assert user is not None
            assert task is not None
            try:
                approve_or_cancel_task(
                    approval_db,
                    user,
                    task,
                    decision="approve",
                    notes="Approval check while provider is blocked.",
                )
            except HTTPException as error:
                approval_db.rollback()
                return error.status_code
            raise AssertionError("Running external read must not be approvable")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            worker_future = executor.submit(run_worker)
            assert provider_started.wait(timeout=15)
            approval_future = executor.submit(attempt_approval)
            # Approval is not valid from running, but it must acquire/release
            # the row immediately instead of waiting for provider I/O.
            assert approval_future.result(timeout=5) == 409
            cancellation_future = executor.submit(cancel_task)
            # This completes before the provider is released, proving that the
            # HTTP phase retains no task row lock.
            assert cancellation_future.result(timeout=5) == "cancelled"
            release_provider.set()
            assert worker_future.result(timeout=15) == "cancelled"

        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, int(ids["task_id"]))
            assert task is not None
            event_types = [event.event_type for event in task.events]
            assert task.status == "cancelled"
            assert event_types.count("started") == 1
            assert event_types.count("external_read_claimed") == 1
            assert event_types.count("cancelled") == 1
            assert "completed" not in event_types
    finally:
        release_provider.set()
        _cleanup_engine_ordinance_task(engine, ids)


def test_two_workers_make_one_external_ordinance_provider_call(
    engine,
    monkeypatch,
):
    ids = _seed_engine_ordinance_task(engine)
    workers_ready = threading.Barrier(2)
    provider_started = threading.Event()
    release_provider = threading.Event()
    call_count = 0
    call_count_lock = threading.Lock()

    def blocked_embedding(
        tool_input: dict,
        *,
        provider_deadline_at=None,
    ) -> PreparedOrdinanceSearchEmbedding:
        del provider_deadline_at
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        provider_started.set()
        assert release_provider.wait(timeout=15)
        return PreparedOrdinanceSearchEmbedding(
            query=str(tool_input["query"]),
            vector=None,
            model="test-disabled",
            status="disabled",
        )

    monkeypatch.setattr(
        "app.agent_office.service.prepare_ordinance_search_embedding",
        blocked_embedding,
    )

    def run_worker() -> str:
        with Session(engine, expire_on_commit=False) as worker_db:
            workers_ready.wait(timeout=15)
            return run_agent_office_task(
                int(ids["task_id"]),
                db=worker_db,
            ).status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_worker) for _ in range(2)]
            assert provider_started.wait(timeout=15)
            completed_while_provider_blocked, _ = wait(
                futures,
                timeout=5,
                return_when=FIRST_COMPLETED,
            )
            assert len(completed_while_provider_blocked) == 1
            assert next(iter(completed_while_provider_blocked)).result() == "running"
            with call_count_lock:
                assert call_count == 1

            release_provider.set()
            statuses = [future.result(timeout=15) for future in futures]

        assert sorted(statuses) == ["completed", "running"]
        with call_count_lock:
            assert call_count == 1
        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, int(ids["task_id"]))
            assert task is not None
            event_types = [event.event_type for event in task.events]
            assert task.status == "completed"
            assert event_types.count("started") == 1
            assert event_types.count("external_read_claimed") == 1
            assert event_types.count("completed") == 1
    finally:
        release_provider.set()
        _cleanup_engine_ordinance_task(engine, ids)


@pytest.mark.parametrize("initial_status", ["queued", "running"])
def test_execute_tool_rollback_keeps_started_then_failed_history(
    engine,
    monkeypatch,
    initial_status,
):
    ids = _seed_engine_feedback_task(engine, status=initial_status)
    original_spec = TOOL_CATALOG["send_admin_feedback"]

    def fail_after_effect_flush(db, user, tool_input, context):
        original_spec.executor(db, user, tool_input, context)
        raise ValueError("forced executor rollback")

    monkeypatch.setitem(
        TOOL_CATALOG,
        "send_admin_feedback",
        replace(original_spec, executor=fail_after_effect_flush),
    )

    try:
        with Session(engine, expire_on_commit=False) as worker_db:
            result = run_agent_office_task(
                ids["task_id"],
                db=worker_db,
            )
            assert result.status == "failed"

        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, ids["task_id"])
            assert task is not None
            event_types = [event.event_type for event in task.events]
            assert event_types.count("started") == 1
            assert event_types.count("failed") == 1
            assert event_types.index("started") < event_types.index("failed")
            assert "tool_authorization_issued" not in event_types
            assert "tool_authorization_claimed" not in event_types
            assert "tool_execution_completed" not in event_types
            assert (
                verification_db.query(AssistantAdminFeedback)
                .filter_by(organization_id=ids["organization_id"])
                .count()
                == 0
            )
    finally:
        _cleanup_engine_feedback_task(engine, ids)


def test_early_execution_context_error_keeps_started_then_failed_history(engine):
    ids = _seed_engine_feedback_task(engine, status="queued")
    try:
        with Session(engine) as setup_db:
            user = setup_db.get(User, ids["user_id"])
            assert user is not None
            user.is_active = False
            setup_db.commit()

        with Session(engine, expire_on_commit=False) as worker_db:
            result = run_agent_office_task(
                ids["task_id"],
                db=worker_db,
            )
            assert result.status == "failed"

        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, ids["task_id"])
            assert task is not None
            execution_events = [
                event.event_type
                for event in task.events
                if event.event_type in {"started", "failed"}
            ]
            assert execution_events == ["started", "failed"]
            assert task.error_message == "Task has no user context for RBAC execution"
    finally:
        _cleanup_engine_feedback_task(engine, ids)


def test_agent_task_cancellation_wins_row_lock_before_worker(engine):
    ids = _seed_engine_feedback_task(engine, status="queued")
    cancellation_locked = threading.Event()
    worker_read_snapshot = threading.Event()

    def cancel_task() -> str:
        with Session(engine, expire_on_commit=False) as cancellation_db:
            user = cancellation_db.get(User, ids["user_id"])
            stale_task = cancellation_db.get(AgentOfficeTask, ids["task_id"])
            assert user is not None
            assert stale_task is not None
            locked = cancellation_db.scalar(
                select(AgentOfficeTask)
                .where(AgentOfficeTask.id == ids["task_id"])
                .with_for_update()
            )
            assert locked is not None
            cancellation_locked.set()
            assert worker_read_snapshot.wait(timeout=15)
            cancelled = approve_or_cancel_task(
                cancellation_db,
                user,
                stale_task,
                decision="cancel",
                notes="Cancellation wins the task row lock.",
            )
            return cancelled.status

    def run_worker() -> str:
        assert cancellation_locked.wait(timeout=15)
        with Session(engine, expire_on_commit=False) as worker_db:
            snapshot = worker_db.get(AgentOfficeTask, ids["task_id"])
            assert snapshot is not None
            assert snapshot.status == "queued"
            worker_read_snapshot.set()
            result = run_agent_office_task(ids["task_id"], db=worker_db)
            return result.status

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            cancel_future = executor.submit(cancel_task)
            worker_future = executor.submit(run_worker)
            assert cancel_future.result(timeout=20) == "cancelled"
            assert worker_future.result(timeout=20) == "cancelled"

        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, ids["task_id"])
            assert task is not None
            event_types = [event.event_type for event in task.events]
            feedback_count = verification_db.query(AssistantAdminFeedback).filter_by(
                organization_id=ids["organization_id"]
            ).count()
            assert task.status == "cancelled"
            assert event_types.count("cancelled") == 1
            assert "started" not in event_types
            assert feedback_count == 0
    finally:
        _cleanup_engine_feedback_task(engine, ids)


def test_two_enqueue_requests_create_one_queue_transition(engine):
    ids = _seed_engine_feedback_task(engine, status="approved")
    barrier = threading.Barrier(2)

    def enqueue() -> str | int:
        with Session(engine, expire_on_commit=False) as candidate_db:
            user = candidate_db.get(User, ids["user_id"])
            task = candidate_db.get(AgentOfficeTask, ids["task_id"])
            assert user is not None
            assert task is not None
            barrier.wait(timeout=15)
            try:
                return mark_task_queued(candidate_db, user, task).status
            except HTTPException as error:
                candidate_db.rollback()
                return error.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: enqueue(), range(2)))

        assert set(results) == {409, "queued"}
        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, ids["task_id"])
            assert task is not None
            assert task.status == "queued"
            assert [event.event_type for event in task.events].count("queued") == 1
    finally:
        _cleanup_engine_feedback_task(engine, ids)


def test_running_agent_task_recovers_committed_incomplete_intent(engine):
    ids = _seed_engine_feedback_task(engine, status="running")
    try:
        with Session(engine, expire_on_commit=False) as intent_db:
            task = intent_db.get(AgentOfficeTask, ids["task_id"])
            user = intent_db.get(User, ids["user_id"])
            assert task is not None
            assert user is not None
            canonical_input = normalize_tool_input(
                intent_db,
                user,
                "send_admin_feedback",
                task.input,
            )
            issued = issue_agent_office_tool_authorization(
                intent_db,
                task_id=task.id,
                actor_id=user.id,
                tool="send_admin_feedback",
                input_digest=tool_input_digest(
                    "send_admin_feedback",
                    canonical_input,
                ),
            )
            intent_db.commit()

        with Session(engine, expire_on_commit=False) as recovery_db:
            recovered = run_agent_office_task(
                ids["task_id"],
                db=recovery_db,
            )
            assert recovered.status == "completed"

        with Session(engine) as verification_db:
            task = verification_db.get(AgentOfficeTask, ids["task_id"])
            assert task is not None
            event_types = [event.event_type for event in task.events]
            assert event_types.count("started") == 1
            assert event_types.count("tool_authorization_issued") == 1
            assert event_types.count("tool_authorization_claimed") == 1
            assert event_types.count("tool_execution_completed") == 1
            assert event_types.count("completed") == 1
            assert (
                verification_db.query(AssistantAdminFeedback)
                .filter_by(organization_id=ids["organization_id"])
                .count()
                == 1
            )
            completion = next(
                event
                for event in task.events
                if event.event_type == "tool_execution_completed"
            )
            assert completion.payload["execution_attempt_id"] == (
                issued.execution_attempt_id
            )
    finally:
        _cleanup_engine_feedback_task(engine, ids)


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
