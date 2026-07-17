import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.assistant import guards as assistant_guards
from app.assistant.models import (
    AssistantAdminFeedback,
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessage,
)
from app.assistant.routes import get_gateway
from app.main import app
from conftest import headers_for


def make_memory_entry(
    db,
    organization,
    proposed_by,
    *,
    content: str,
    status: str = "proposed",
    sensitivity: str = "normal",
    reviewed_by=None,
) -> AssistantMemoryEntry:
    entry = AssistantMemoryEntry(
        organization_id=organization.id,
        category="context",
        content=content,
        status=status,
        sensitivity=sensitivity,
        proposed_by_id=proposed_by.id,
        reviewed_by_id=reviewed_by.id if reviewed_by else None,
        reviewed_at=datetime.now(timezone.utc) if reviewed_by else None,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_admin_feedback(
    db,
    organization,
    submitted_by,
    *,
    title: str,
    status: str = "submitted",
) -> AssistantAdminFeedback:
    feedback = AssistantAdminFeedback(
        organization_id=organization.id if organization else None,
        category="improvement",
        title=title,
        description=f"Detalle de {title}",
        priority="medium",
        status=status,
        submitted_by_id=submitted_by.id,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


@pytest.fixture()
def feedback_assistant_user(
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user(full_name="Alcalde que envía feedback")
    organization = make_organization(name="Ayuntamiento que envía feedback")
    grant_permissions(user, organization, ["assistant.use"])
    return user, organization


def test_memory_listing_is_scoped_paginated_and_includes_organization(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable de memoria")
    proposer = make_user(full_name="Proponente")
    allowed_organization = make_organization(name="Ayuntamiento permitido")
    foreign_organization = make_organization(name="Ayuntamiento ajeno")
    grant_permissions(
        reviewer,
        allowed_organization,
        ["assistant.use", "assistant.memory.review"],
    )
    allowed_entries = [
        make_memory_entry(
            db,
            allowed_organization,
            proposer,
            content=f"Contexto permitido {index}",
        )
        for index in range(2)
    ]
    foreign_entry = make_memory_entry(
        db,
        foreign_organization,
        proposer,
        content="Contexto de otra organización",
    )

    first_page = client.get(
        "/assistant/memory?status=proposed&limit=1&offset=0",
        headers=headers_for(reviewer),
    )
    second_page = client.get(
        "/assistant/memory?status=proposed&limit=1&offset=1",
        headers=headers_for(reviewer),
    )

    assert first_page.status_code == 200
    assert first_page.headers["X-Total-Count"] == "2"
    assert len(first_page.json()) == 1
    assert len(second_page.json()) == 1
    assert first_page.json()[0]["id"] != second_page.json()[0]["id"]
    assert {
        first_page.json()[0]["id"],
        second_page.json()[0]["id"],
    } == {entry.id for entry in allowed_entries}
    assert first_page.json()[0]["organization"] == {
        "id": allowed_organization.id,
        "name": "Ayuntamiento permitido",
        "status": "active",
    }
    assert foreign_entry.id not in {
        first_page.json()[0]["id"],
        second_page.json()[0]["id"],
    }

    foreign_scope = client.get(
        f"/assistant/memory?status=proposed"
        f"&organization_id={foreign_organization.id}",
        headers=headers_for(reviewer),
    )

    assert foreign_scope.status_code == 200
    assert foreign_scope.headers["X-Total-Count"] == "0"
    assert foreign_scope.json() == []


def test_memory_reviewer_can_list_approved_without_memory_view(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable de memoria")
    proposer = make_user(full_name="Proponente")
    organization = make_organization(name="Ayuntamiento revisable")
    grant_permissions(
        reviewer,
        organization,
        ["assistant.use", "assistant.memory.review"],
    )
    approved = make_memory_entry(
        db,
        organization,
        proposer,
        content="Protocolo ya aprobado",
        status="approved",
    )
    make_memory_entry(
        db,
        organization,
        proposer,
        content="Protocolo pendiente",
    )

    response = client.get(
        "/assistant/memory?status=approved",
        headers=headers_for(reviewer),
    )

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "1"
    assert [entry["id"] for entry in response.json()] == [approved.id]


def test_approved_memory_is_redacted_for_viewer_but_detailed_for_reviewer(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user(full_name="Responsable con ámbitos distintos")
    proposer = make_user(full_name="Persona proponente")
    previous_reviewer = make_user(full_name="Persona revisora")
    view_only_organization = make_organization(name="Consulta aprobada")
    review_organization = make_organization(name="Revisión completa")
    grant_permissions(
        user,
        view_only_organization,
        ["assistant.use", "assistant.memory.view"],
    )
    grant_permissions(
        user,
        review_organization,
        ["assistant.use", "assistant.memory.review"],
    )
    source_conversation = AssistantConversation(
        title="Conversación de procedencia",
        status="active",
        channel="web",
        created_by_id=proposer.id,
    )
    source_message = AssistantMessage(
        conversation=source_conversation,
        role="user",
        content="Información municipal de procedencia",
    )
    db.add_all([source_conversation, source_message])
    db.commit()
    view_only_entry = make_memory_entry(
        db,
        view_only_organization,
        proposer,
        content="Contenido aprobado visible",
        status="approved",
        reviewed_by=previous_reviewer,
    )
    reviewable_entry = make_memory_entry(
        db,
        review_organization,
        proposer,
        content="Contenido aprobado revisable",
        status="approved",
        reviewed_by=previous_reviewer,
    )
    for entry in (view_only_entry, reviewable_entry):
        entry.source_conversation_id = source_conversation.id
        entry.source_message_id = source_message.id
        entry.review_notes = "Nota interna de revisión"
    db.commit()

    response = client.get(
        "/assistant/memory?status=approved",
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "2"
    entries = {entry["id"]: entry for entry in response.json()}
    redacted = entries[view_only_entry.id]
    assert redacted["content"] == "Contenido aprobado visible"
    assert redacted["organization"]["id"] == view_only_organization.id
    assert redacted["status"] == "approved"
    assert redacted["sensitivity"] == "normal"
    assert redacted["created_at"] is not None
    assert redacted["updated_at"] is not None
    for field in (
        "source_conversation_id",
        "source_message_id",
        "proposed_by_id",
        "reviewed_by_id",
        "proposed_by",
        "reviewed_by",
        "review_notes",
    ):
        assert redacted[field] is None

    detailed = entries[reviewable_entry.id]
    assert detailed["source_conversation_id"] == source_conversation.id
    assert detailed["source_message_id"] == source_message.id
    assert detailed["proposed_by_id"] == proposer.id
    assert detailed["reviewed_by_id"] == previous_reviewer.id
    assert detailed["proposed_by"]["id"] == proposer.id
    assert detailed["reviewed_by"]["id"] == previous_reviewer.id
    assert detailed["review_notes"] == "Nota interna de revisión"


def test_memory_reviewable_only_excludes_view_only_approved_entries(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable con ámbitos distintos")
    proposer = make_user(full_name="Persona proponente")
    view_only_organization = make_organization(name="Solo consulta")
    review_organization = make_organization(name="Revisión completa")
    grant_permissions(
        reviewer,
        view_only_organization,
        ["assistant.use", "assistant.memory.view"],
    )
    grant_permissions(
        reviewer,
        review_organization,
        ["assistant.use", "assistant.memory.review"],
    )
    view_only_entry = make_memory_entry(
        db,
        view_only_organization,
        proposer,
        content="Contenido aprobado de consulta",
        status="approved",
    )
    reviewable_entry = make_memory_entry(
        db,
        review_organization,
        proposer,
        content="Contenido aprobado revisable",
        status="approved",
    )

    response = client.get(
        "/assistant/memory?status=approved&reviewable_only=true",
        headers=headers_for(reviewer),
    )

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "1"
    assert [entry["id"] for entry in response.json()] == [reviewable_entry.id]
    assert view_only_entry.id not in {entry["id"] for entry in response.json()}


def test_memory_patch_outside_reviewer_organizations_returns_404(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable de memoria")
    proposer = make_user(full_name="Proponente")
    allowed_organization = make_organization()
    foreign_organization = make_organization()
    grant_permissions(
        reviewer,
        allowed_organization,
        ["assistant.use", "assistant.memory.review"],
    )
    foreign_entry = make_memory_entry(
        db,
        foreign_organization,
        proposer,
        content="No debe revelarse",
    )

    response = client.patch(
        f"/assistant/memory/{foreign_entry.id}",
        json={
            "expected_updated_at": foreign_entry.updated_at.isoformat(),
            "status": "approved",
        },
        headers=headers_for(reviewer),
    )

    assert response.status_code == 404


def test_memory_patch_rejects_stale_expected_updated_at(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable de memoria")
    proposer = make_user(full_name="Proponente")
    organization = make_organization()
    grant_permissions(
        reviewer,
        organization,
        ["assistant.use", "assistant.memory.review"],
    )
    entry = make_memory_entry(
        db,
        organization,
        proposer,
        content="Contenido vigente",
    )
    expected_updated_at = entry.updated_at
    entry.content = "Contenido actualizado por otro revisor"
    entry.updated_at = expected_updated_at + timedelta(seconds=1)
    db.commit()

    response = client.patch(
        f"/assistant/memory/{entry.id}",
        json={
            "expected_updated_at": expected_updated_at.isoformat(),
            "content": "Edición sobre una versión obsoleta",
        },
        headers=headers_for(reviewer),
    )

    assert response.status_code == 409
    db.refresh(entry)
    assert entry.content == "Contenido actualizado por otro revisor"


def test_material_memory_edit_reopens_approved_unless_explicitly_reapproved(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable actual")
    previous_reviewer = make_user(full_name="Responsable anterior")
    proposer = make_user(full_name="Proponente")
    organization = make_organization()
    grant_permissions(
        reviewer,
        organization,
        ["assistant.use", "assistant.memory.review"],
    )
    entry = make_memory_entry(
        db,
        organization,
        proposer,
        content="Contenido aprobado",
        status="approved",
        reviewed_by=previous_reviewer,
    )

    reopened = client.patch(
        f"/assistant/memory/{entry.id}",
        json={
            "expected_updated_at": entry.updated_at.isoformat(),
            "content": "Contenido materialmente corregido",
        },
        headers=headers_for(reviewer),
    )

    assert reopened.status_code == 200
    reopened_body = reopened.json()
    assert reopened_body["status"] == "proposed"
    assert reopened_body["reviewed_by_id"] is None
    assert reopened_body["reviewed_by"] is None
    assert reopened_body["reviewed_at"] is None

    reapproved = client.patch(
        f"/assistant/memory/{entry.id}",
        json={
            "expected_updated_at": reopened_body["updated_at"],
            "content": "Contenido corregido y aprobado",
            "status": "approved",
            "review_notes": "Comprobado con la fuente municipal.",
        },
        headers=headers_for(reviewer),
    )

    assert reapproved.status_code == 200
    reapproved_body = reapproved.json()
    assert reapproved_body["status"] == "approved"
    assert reapproved_body["reviewed_by_id"] == reviewer.id
    assert reapproved_body["reviewed_by"]["id"] == reviewer.id
    assert reapproved_body["reviewed_at"] is not None
    assert reapproved_body["review_notes"] == "Comprobado con la fuente municipal."


def test_sensitive_memory_approval_requires_explicit_confirmation(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable de memoria sensible")
    proposer = make_user(full_name="Proponente")
    organization = make_organization()
    grant_permissions(
        reviewer,
        organization,
        ["assistant.use", "assistant.memory.review"],
    )
    entry = make_memory_entry(
        db,
        organization,
        proposer,
        content="Coordinación municipal con datos personales",
        sensitivity="personal",
    )
    update = {
        "expected_updated_at": entry.updated_at.isoformat(),
        "status": "approved",
    }

    missing_confirmation = client.patch(
        f"/assistant/memory/{entry.id}",
        json=update,
        headers=headers_for(reviewer),
    )

    assert missing_confirmation.status_code == 422
    assert missing_confirmation.json()["detail"] == (
        "Sensitive assistant memory approval requires explicit confirmation"
    )
    db.refresh(entry)
    assert entry.status == "proposed"

    confirmed = client.patch(
        f"/assistant/memory/{entry.id}",
        json={**update, "sensitive_approval_confirmed": True},
        headers=headers_for(reviewer),
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "approved"
    assert confirmed.json()["reviewed_by_id"] == reviewer.id


@pytest.mark.parametrize(
    ("target_status", "new_content"),
    [
        ("rejected", None),
        ("blocked", "Contenido corregido durante el bloqueo"),
        ("archived", None),
    ],
)
def test_approved_memory_can_be_directly_revoked(
    target_status,
    new_content,
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable actual")
    previous_reviewer = make_user(full_name="Responsable anterior")
    proposer = make_user(full_name="Proponente")
    organization = make_organization()
    grant_permissions(
        reviewer,
        organization,
        ["assistant.use", "assistant.memory.review"],
    )
    entry = make_memory_entry(
        db,
        organization,
        proposer,
        content="Contenido aprobado que debe revocarse",
        status="approved",
        reviewed_by=previous_reviewer,
    )
    update = {
        "expected_updated_at": entry.updated_at.isoformat(),
        "status": target_status,
        "review_notes": f"Revocada como {target_status}.",
    }
    if new_content is not None:
        update["content"] = new_content

    response = client.patch(
        f"/assistant/memory/{entry.id}",
        json=update,
        headers=headers_for(reviewer),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == target_status
    assert body["reviewed_by_id"] == reviewer.id
    assert body["reviewed_by"]["id"] == reviewer.id
    assert body["reviewed_at"] is not None
    assert body["review_notes"] == f"Revocada como {target_status}."
    assert body["content"] == (
        new_content or "Contenido aprobado que debe revocarse"
    )


def test_memory_rejects_invalid_archived_to_approved_transition(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    reviewer = make_user(full_name="Responsable de memoria")
    proposer = make_user(full_name="Proponente")
    organization = make_organization()
    grant_permissions(
        reviewer,
        organization,
        ["assistant.use", "assistant.memory.review"],
    )
    entry = make_memory_entry(
        db,
        organization,
        proposer,
        content="Memoria archivada",
        status="archived",
    )

    response = client.patch(
        f"/assistant/memory/{entry.id}",
        json={
            "expected_updated_at": entry.updated_at.isoformat(),
            "status": "approved",
        },
        headers=headers_for(reviewer),
    )

    assert response.status_code == 409


def test_admin_feedback_listing_is_superuser_only_paginated_and_scoped_by_status(
    client,
    db,
    superuser,
    make_user,
    make_organization,
):
    submitter = make_user(full_name="Alcalde remitente")
    ordinary_user = make_user(full_name="Usuario sin acceso")
    organization = make_organization(name="Ayuntamiento remitente")
    submitted = [
        make_admin_feedback(
            db,
            organization,
            submitter,
            title=f"Mejora {index}",
        )
        for index in range(2)
    ]
    make_admin_feedback(
        db,
        organization,
        submitter,
        title="Ya revisado",
        status="reviewed",
    )

    forbidden = client.get(
        "/assistant/admin-feedback",
        headers=headers_for(ordinary_user),
    )
    first_page = client.get(
        "/assistant/admin-feedback?status=submitted&limit=1&offset=0",
        headers=headers_for(superuser),
    )
    second_page = client.get(
        "/assistant/admin-feedback?status=submitted&limit=1&offset=1",
        headers=headers_for(superuser),
    )

    assert forbidden.status_code == 403
    assert first_page.status_code == 200
    assert first_page.headers["X-Total-Count"] == "2"
    assert len(first_page.json()) == 1
    assert len(second_page.json()) == 1
    assert {
        first_page.json()[0]["id"],
        second_page.json()[0]["id"],
    } == {feedback.id for feedback in submitted}
    assert first_page.json()[0]["organization"] == {
        "id": organization.id,
        "name": "Ayuntamiento remitente",
        "status": "active",
    }
    assert first_page.json()[0]["submitted_by"]["id"] == submitter.id


def test_admin_feedback_update_is_superuser_only_attributed_and_optimistic(
    client,
    db,
    superuser,
    make_user,
    make_organization,
):
    submitter = make_user(full_name="Remitente")
    ordinary_user = make_user(full_name="Usuario sin acceso")
    organization = make_organization()
    feedback = make_admin_feedback(
        db,
        organization,
        submitter,
        title="Mejorar el flujo",
    )
    expected_updated_at = feedback.updated_at.isoformat()
    update = {
        "expected_updated_at": expected_updated_at,
        "status": "reviewed",
        "priority": "urgent",
        "review_notes": "Validado para el siguiente ciclo.",
    }

    forbidden = client.patch(
        f"/assistant/admin-feedback/{feedback.id}",
        json=update,
        headers=headers_for(ordinary_user),
    )
    reviewed = client.patch(
        f"/assistant/admin-feedback/{feedback.id}",
        json=update,
        headers=headers_for(superuser),
    )

    assert forbidden.status_code == 403
    assert reviewed.status_code == 200
    reviewed_body = reviewed.json()
    assert reviewed_body["status"] == "reviewed"
    assert reviewed_body["priority"] == "urgent"
    assert reviewed_body["review_notes"] == "Validado para el siguiente ciclo."
    assert reviewed_body["reviewed_by_id"] == superuser.id
    assert reviewed_body["reviewed_by"]["id"] == superuser.id
    assert reviewed_body["reviewed_at"] is not None

    db.refresh(feedback)
    feedback.updated_at = feedback.updated_at + timedelta(seconds=1)
    db.commit()

    stale = client.patch(
        f"/assistant/admin-feedback/{feedback.id}",
        json={
            "expected_updated_at": expected_updated_at,
            "status": "dismissed",
        },
        headers=headers_for(superuser),
    )

    assert stale.status_code == 409


def test_admin_feedback_rejects_invalid_archived_to_reviewed_transition(
    client,
    db,
    superuser,
    make_user,
    make_organization,
):
    submitter = make_user(full_name="Remitente")
    organization = make_organization()
    feedback = make_admin_feedback(
        db,
        organization,
        submitter,
        title="Feedback archivado",
        status="archived",
    )

    response = client.patch(
        f"/assistant/admin-feedback/{feedback.id}",
        json={
            "expected_updated_at": feedback.updated_at.isoformat(),
            "status": "reviewed",
        },
        headers=headers_for(superuser),
    )

    assert response.status_code == 409


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(call_id: str, name: str, tool_input: dict):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=tool_input)


def fake_response(stop_reason: str, content: list):
    return SimpleNamespace(
        model="fake-model",
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        deltas=[],
    )


class ScriptedGateway:
    enabled = True
    runtime_healthy = True

    def __init__(self, responses: list):
        self.responses = list(responses)

    def complete(
        self,
        *,
        system,
        messages,
        tools,
        timeout_seconds=None,
        safety_identifier=None,
    ):
        if not self.responses:
            raise AssertionError("ScriptedGateway ran out of responses")
        return self.responses.pop(0)


def test_send_admin_feedback_requires_explicit_matching_confirmation(
    client,
    feedback_assistant_user,
    db,
):
    user, organization = feedback_assistant_user
    tool_input = {
        "organization_id": organization.id,
        "category": "improvement",
        "title": "Añadir una bandeja de revisión",
        "description": "Permitir revisar el feedback pendiente desde la aplicación.",
        "priority": "high",
    }
    gateway = ScriptedGateway(
        [
            fake_response(
                "tool_use",
                [tool_use_block("feedback-1", "send_admin_feedback", tool_input)],
            ),
            fake_response(
                "end_turn",
                [text_block("Puedo preparar ese feedback para revisión.")],
            ),
            fake_response(
                "tool_use",
                [tool_use_block("feedback-2", "send_admin_feedback", tool_input)],
            ),
            fake_response(
                "end_turn",
                [text_block("El feedback ha quedado enviado.")],
            ),
        ]
    )
    app.dependency_overrides[get_gateway] = lambda: gateway
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Envía esta mejora al equipo de desarrollo"},
        headers=headers_for(user),
    )

    assert proposed.status_code == 200
    proposed_message = proposed.json()["messages"][-1]
    proposed_action = proposed_message["actions"][0]
    assert proposed_action["tool"] == "send_admin_feedback"
    assert proposed_action["ok"] is False
    assert "confirmación" in proposed_action["result"].lower()
    assert tool_input["title"] in proposed_message["content"]
    assert db.scalar(select(AssistantAdminFeedback)) is None
    state = json.loads(
        db.get(AssistantConversation, conversation["id"]).state or "{}"
    )
    assert state["pending_confirmation"]["tool"] == "send_admin_feedback"

    confirmed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, envíalo"},
        headers=headers_for(user),
    )

    assert confirmed.status_code == 200
    confirmed_action = confirmed.json()["messages"][-1]["actions"][0]
    assert confirmed_action["tool"] == "send_admin_feedback"
    assert confirmed_action["ok"] is True
    feedback = db.scalar(select(AssistantAdminFeedback))
    assert feedback is not None
    assert feedback.title == tool_input["title"]
    assert feedback.description == tool_input["description"]
    assert feedback.status == "submitted"


def test_send_admin_feedback_rejects_requirement_specific_confirmation(
    client,
    feedback_assistant_user,
    db,
):
    user, organization = feedback_assistant_user
    tool_input = {
        "organization_id": organization.id,
        "category": "improvement",
        "title": "Añadir una bandeja de revisión",
        "description": "Permitir revisar el feedback pendiente desde la aplicación.",
    }
    gateway = ScriptedGateway(
        [
            fake_response(
                "tool_use",
                [tool_use_block("feedback-cross-1", "send_admin_feedback", tool_input)],
            ),
            fake_response("end_turn", [text_block("Revisa el feedback.")]),
            fake_response(
                "tool_use",
                [tool_use_block("feedback-cross-2", "send_admin_feedback", tool_input)],
            ),
            fake_response("end_turn", [text_block("Sigue pendiente.")]),
        ]
    )
    app.dependency_overrides[get_gateway] = lambda: gateway
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Envía esta mejora al equipo de desarrollo"},
        headers=headers_for(user),
    )
    assert proposed.status_code == 200
    original_pending = json.loads(
        db.get(AssistantConversation, conversation["id"]).state or "{}"
    )["pending_confirmation"]

    attempted = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert attempted.status_code == 200
    attempted_action = attempted.json()["messages"][-1]["actions"][0]
    assert attempted_action["tool"] == "send_admin_feedback"
    assert attempted_action["ok"] is False
    assert db.scalar(select(AssistantAdminFeedback)) is None
    db.expire_all()
    state = json.loads(
        db.get(AssistantConversation, conversation["id"]).state or "{}"
    )
    assert state["pending_confirmation"]["confirmation_id"] == (
        original_pending["confirmation_id"]
    )
    assert "Sí, envíalo" in attempted.json()["messages"][-1]["content"]


def test_send_admin_feedback_changed_payload_keeps_active_confirmation(
    client,
    feedback_assistant_user,
    db,
):
    user, organization = feedback_assistant_user
    proposed_input = {
        "organization_id": organization.id,
        "category": "bug",
        "title": "El filtro no responde",
        "description": "El filtro de pendientes no actualiza la lista.",
    }
    changed_input = {
        **proposed_input,
        "description": "El filtro de archivados no actualiza la lista.",
    }
    gateway = ScriptedGateway(
        [
            fake_response(
                "tool_use",
                [
                    tool_use_block(
                        "feedback-change-1",
                        "send_admin_feedback",
                        proposed_input,
                    )
                ],
            ),
            fake_response("end_turn", [text_block("Revisa el feedback.")]),
            fake_response(
                "tool_use",
                [
                    tool_use_block(
                        "feedback-change-2",
                        "send_admin_feedback",
                        changed_input,
                    )
                ],
            ),
            fake_response("end_turn", [text_block("Revisa el cambio.")]),
        ]
    )
    app.dependency_overrides[get_gateway] = lambda: gateway
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Envía este error al equipo"},
        headers=headers_for(user),
    )
    assert proposed.status_code == 200
    original_pending = json.loads(
        db.get(AssistantConversation, conversation["id"]).state or "{}"
    )["pending_confirmation"]
    changed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Confirmo"},
        headers=headers_for(user),
    )

    assert changed.status_code == 200
    changed_action = changed.json()["messages"][-1]["actions"][0]
    assert changed_action["tool"] == "send_admin_feedback"
    assert changed_action["ok"] is False
    assert db.scalar(select(AssistantAdminFeedback)) is None
    db.expire_all()
    state = json.loads(
        db.get(AssistantConversation, conversation["id"]).state or "{}"
    )
    pending = state["pending_confirmation"]
    assert pending["tool"] == "send_admin_feedback"
    assert pending["confirmation_id"] == original_pending["confirmation_id"]
    assert pending["input"]["description"] == proposed_input["description"]


def test_send_admin_feedback_cancellation_phrase_is_unambiguous():
    assert (
        assistant_guards.classify_confirmation_response("No lo envíes")
        == "cancelled"
    )
