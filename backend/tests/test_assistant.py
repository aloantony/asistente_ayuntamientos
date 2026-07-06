import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.agent_office.models import AgentOfficeTask
from app.assistant import planner as assistant_planner
from app.assistant import tools as assistant_tools
from app.assistant import service as assistant_service
from app.assistant.agents import AGENT_REGISTRY, get_agent_tools
from app.assistant.gateway import _from_openai_response, _hermes_agent_url
from app.assistant.models import (
    AssistantAdminFeedback,
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessage,
    AssistantTransversalFeature,
    AssistantTransversalFeatureAdoption,
)
from app.assistant.planner import SemanticTurnPlan, choose_agent
from app.assistant.routes import get_gateway
from app.core.config import settings
from app.main import app
from app.municipalities.models import Municipality
from app.ordinances.embeddings import embed_text
from app.ordinances.models import Ordinance, OrdinanceLegalChunk
from app.projects.models import Project
from app.requirements.models import Requirement
from conftest import headers_for


class FakeTextBlock(SimpleNamespace):
    pass


class FakeToolUseBlock(SimpleNamespace):
    pass


def text_block(text: str) -> FakeTextBlock:
    return FakeTextBlock(type="text", text=text)


def tool_use_block(block_id: str, name: str, tool_input: dict) -> FakeToolUseBlock:
    return FakeToolUseBlock(type="tool_use", id=block_id, name=name, input=tool_input)


class FakeGateway:
    def __init__(
        self,
        responses: list,
        enabled: bool = True,
        runtime_healthy: bool | None = None,
    ):
        self.enabled = enabled
        self.runtime_healthy = runtime_healthy
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeGateway ran out of scripted responses")
        return self.responses.pop(0)


def fake_response(stop_reason: str, content: list):
    return SimpleNamespace(stop_reason=stop_reason, content=content)


@pytest.fixture()
def use_gateway(client):
    def _use(gateway: FakeGateway) -> FakeGateway:
        app.dependency_overrides[get_gateway] = lambda: gateway
        return gateway

    yield _use
    app.dependency_overrides.pop(get_gateway, None)


@pytest.fixture()
def assistant_user(make_user, make_organization, grant_permissions):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Ayuntamiento Test")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    return user, organization


def test_assistant_requires_permission(client, make_user):
    user = make_user()

    response = client.get("/assistant/conversations", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_conversation_folders_are_private_and_persisted(
    client,
    assistant_user,
    make_user,
    make_organization,
    grant_permissions,
):
    user, _ = assistant_user
    other_user = make_user(full_name="Otra Alcaldesa")
    grant_permissions(other_user, make_organization(), ["assistant.use"])

    folder_response = client.post(
        "/assistant/conversation-folders",
        json={"name": "Seguimiento político"},
        headers=headers_for(user),
    )
    assert folder_response.status_code == 201
    folder = folder_response.json()
    assert folder["name"] == "Seguimiento político"

    conversation = client.post(
        "/assistant/conversations",
        json={"title": "Licencias urbanísticas"},
        headers=headers_for(user),
    ).json()
    assigned = client.patch(
        f"/assistant/conversations/{conversation['id']}",
        json={"folder_id": folder["id"]},
        headers=headers_for(user),
    )

    assert assigned.status_code == 200
    assert assigned.json()["folder_id"] == folder["id"]
    listed = client.get("/assistant/conversations", headers=headers_for(user)).json()
    assert listed[0]["folder_id"] == folder["id"]

    other_folders = client.get(
        "/assistant/conversation-folders",
        headers=headers_for(other_user),
    )
    assert other_folders.status_code == 200
    assert other_folders.json() == []


def test_conversation_folder_delete_unassigns_conversations(client, assistant_user):
    user, _ = assistant_user
    folder = client.post(
        "/assistant/conversation-folders",
        json={"name": "Borradores"},
        headers=headers_for(user),
    ).json()
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    client.patch(
        f"/assistant/conversations/{conversation['id']}",
        json={"folder_id": folder["id"]},
        headers=headers_for(user),
    )

    response = client.delete(
        f"/assistant/conversation-folders/{folder['id']}",
        headers=headers_for(user),
    )

    assert response.status_code == 204
    refreshed = client.get(
        f"/assistant/conversations/{conversation['id']}",
        headers=headers_for(user),
    ).json()
    assert refreshed["folder_id"] is None


def test_conversation_folder_duplicate_name_returns_conflict(client, assistant_user):
    user, _ = assistant_user
    first = client.post(
        "/assistant/conversation-folders",
        json={"name": "Borradores"},
        headers=headers_for(user),
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/assistant/conversation-folders",
        json={"name": "Borradores"},
        headers=headers_for(user),
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Assistant conversation folder already exists"


def test_assistant_feedback_issue_delegates_to_conversation(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Veo el problema. Puedo ayudarte a explicarlo y, "
                            "si quieres, preparar un aviso para administración."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    suggestion = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "El chat falla cuando intento enviar feedback"},
        headers=headers_for(user),
    )

    assert suggestion.status_code == 200
    suggestion_message = suggestion.json()["messages"][-1]
    assert suggestion_message["routing"]["source"] == "disabled"
    assert suggestion_message["actions"] == []
    assert "administración" in suggestion_message["content"].lower()
    assert "esto parece feedback útil" not in suggestion_message["content"].lower()
    assert db.scalar(select(func.count()).select_from(AssistantAdminFeedback)) == 0
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    assert "pending_action" not in json.loads(stored_conversation.state or "{}")
    assert len(gateway.calls) == 1


def test_feedback_can_be_explained_and_converted_to_requirement(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "last_admin_feedback": {
                "id": 12,
                "status": "submitted",
                "organization_id": organization.id,
                "category": "missing_capability",
                "title": "Respuesta por voz del asistente",
                "description": "El asistente debería poder responder también por voz.",
                "priority": "medium",
            }
        },
        ensure_ascii=False,
    )
    db.commit()

    where = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "dónde puede consultar el administrador esto?"},
        headers=headers_for(user),
    )
    assert where.status_code == 200
    where_message = where.json()["messages"][-1]
    where_content = where_message["content"].lower()
    assert where_message["actions"] == []
    assert where_message["routing"]["reason"] == "direct_admin_feedback_location"
    assert "feedback interno" in where_content
    assert "superusuarios" in where_content
    assert "no tengo una herramienta" not in where_content
    assert "no puedo confirmar" not in where_content

    create = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "créalo como necesidad"},
        headers=headers_for(user),
    )
    assert create.status_code == 200
    create_message = create.json()["messages"][-1]
    assert [action["tool"] for action in create_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    assert create_message["routing"]["reason"] == "direct_feedback_to_requirement"
    assert "borrador" in create_message["content"].lower()
    assert "no puedo crear" not in create_message["content"].lower()
    requirement = db.scalar(select(Requirement).where(Requirement.organization_id == organization.id))
    assert requirement is not None
    assert requirement.title == "Respuesta por voz del asistente"
    assert "responder también por voz" in requirement.problem.lower()
    assert gateway.calls == []


def test_semantic_planner_admin_feedback_suggestion_delegates_to_gateway(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="suggest_admin_feedback",
            action="send_admin_feedback",
            target={"organization_id": organization.id},
            draft={
                "category": "ux",
                "title": "Flujo de pantalla confuso",
                "description": "La pantalla de revisión no deja claro cuál es el siguiente paso.",
                "priority": "medium",
            },
            confidence=0.92,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Sí, esa pantalla puede explicarse mejor. "
                            "Lo podemos convertir en una nota para administración si quieres."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    suggestion = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "La pantalla de revisión no deja claro cuál es el siguiente paso."},
        headers=headers_for(user),
    )

    assert suggestion.status_code == 200
    suggestion_message = suggestion.json()["messages"][-1]
    assert suggestion_message["actions"] == []
    assert suggestion_message["routing"]["source"] == "disabled"
    assert "administra" in suggestion_message["content"].lower()
    assert db.scalar(select(func.count()).select_from(AssistantAdminFeedback)) == 0

    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_action" not in state
    assert len(gateway.calls) == 1


def test_semantic_planner_confirms_pending_admin_feedback_without_phrase_match(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="suggest_admin_feedback",
            action="send_admin_feedback",
            confidence=0.96,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "pending_action": {
                "type": "send_admin_feedback",
                "tool_input": {
                    "category": "bug",
                    "title": "Error al cargar",
                    "description": "La pantalla queda cargando indefinidamente.",
                    "priority": "high",
                    "organization_id": organization.id,
                },
            }
        },
        ensure_ascii=False,
    )
    db.add(stored_conversation)
    db.commit()

    confirmation = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "proceda con ello"},
        headers=headers_for(user),
    )

    assert confirmation.status_code == 200
    confirmation_message = confirmation.json()["messages"][-1]
    assert confirmation_message["routing"]["reason"] == "action_policy_suggest_admin_feedback"
    assert confirmation_message["routing"]["semantic_plan"]["source"] == "planner"
    assert confirmation_message["actions"][0]["tool"] == "send_admin_feedback"
    feedback = db.scalar(select(AssistantAdminFeedback))
    assert feedback is not None
    assert feedback.organization_id == organization.id
    assert feedback.category == "bug"
    assert feedback.status == "submitted"
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    assert "pending_action" not in json.loads(updated_conversation.state or "{}")
    assert gateway.calls == []


def test_semantic_planner_cancels_pending_action_without_phrase_match(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="cancel_pending_action",
            action="cancel_pending_action",
            confidence=0.93,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "pending_action": {
                "type": "send_admin_feedback",
                "tool_input": {
                    "category": "ux",
                    "title": "Pantalla confusa",
                    "description": "La pantalla no explica el siguiente paso.",
                    "priority": "medium",
                    "organization_id": organization.id,
                },
            }
        },
        ensure_ascii=False,
    )
    db.add(stored_conversation)
    db.commit()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "déjalo sin efecto"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_cancel_pending_action"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    assert assistant_message["actions"] == []
    assert db.scalar(select(func.count()).select_from(AssistantAdminFeedback)) == 0
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    assert "pending_action" not in json.loads(updated_conversation.state or "{}")
    assert gateway.calls == []


def test_superuser_can_list_and_review_admin_feedback(
    client,
    assistant_user,
    db,
    superuser,
):
    user, organization = assistant_user
    feedback = AssistantAdminFeedback(
        organization_id=organization.id,
        category="ux",
        title="Mensaje confuso",
        description="El asistente debería sugerir enviar feedback.",
        priority="medium",
        submitted_by_id=user.id,
    )
    db.add(feedback)
    db.commit()

    listed = client.get("/assistant/admin-feedback", headers=headers_for(superuser))

    assert listed.status_code == 200
    assert listed.json()[0]["title"] == "Mensaje confuso"

    reviewed = client.patch(
        f"/assistant/admin-feedback/{feedback.id}",
        json={"status": "reviewed", "review_notes": "Visto"},
        headers=headers_for(superuser),
    )

    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["status"] == "reviewed"
    assert body["review_notes"] == "Visto"
    assert body["reviewed_by_id"] == superuser.id


def test_assistant_can_create_supervised_agent_office_task(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    organization = make_organization("Ayuntamiento Oficina")
    user = make_user(full_name="Alcaldesa Oficina")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "agent_office.create"],
    )
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "office_1",
                            "create_agent_office_task",
                            {
                                "organization_id": organization.id,
                                "title": "Revisión supervisada",
                                "description": "Preparar una revisión supervisada para mañana.",
                                "department": "front_desk",
                                "requested_action": "triage",
                                "priority": "medium",
                                "approval_policy": "before_execution",
                                "requires_human_approval": True,
                            },
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("He dejado creada una tarea supervisada para revisión.")],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "Necesito que prepares una tarea supervisada para revisar este asunto mañana."
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"][0]["tool"] == "create_agent_office_task"
    assert assistant_message["actions"][0]["ok"] is True
    assert "agente de" not in assistant_message["content"].lower()

    action_result = json.loads(assistant_message["actions"][0]["result"])
    task = db.get(AgentOfficeTask, action_result["id"])
    assert task is not None
    assert task.organization_id == organization.id
    assert task.department == "front_desk"
    assert task.requested_action == "triage"
    assert task.status == "pending_approval"
    assert task.requires_human_approval is True
    assert task.source_conversation_id == conversation["id"]
    assert task.source_message_id is not None
    assert gateway.calls[0]["tools"]


def test_semantic_plan_can_create_supervised_agent_office_task_without_gateway(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    organization = make_organization("Ayuntamiento Planificado")
    user = make_user(full_name="Alcaldesa Planificada")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "agent_office.create"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: SemanticTurnPlan(
            intent="delegate_agent_office",
            action="create_agent_office_task",
            confidence=0.92,
            target={
                "organization_id": organization.id,
                "title": "Revisión semántica supervisada",
                "description": "Preparar una revisión desde el plan semántico.",
                "department": "front_desk",
                "requested_action": "triage",
                "priority": "medium",
                "approval_policy": "before_execution",
                "requires_human_approval": True,
            },
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Prepara esto como tarea supervisada para revisarlo mañana."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "delegate_agent_office"
    assert assistant_message["routing"]["semantic_plan"]["action"] == "create_agent_office_task"
    assert assistant_message["actions"][0]["tool"] == "create_agent_office_task"
    assert assistant_message["actions"][0]["ok"] is True
    assert "tarea supervisada" in assistant_message["content"].lower()
    assert "agente de" not in assistant_message["content"].lower()
    assert gateway.calls == []

    task = db.get(
        AgentOfficeTask,
        json.loads(assistant_message["actions"][0]["result"])["id"],
    )
    assert task is not None
    assert task.source_conversation_id == conversation["id"]
    assert task.source_message_id is not None
    assert task.status == "pending_approval"


def test_semantic_plan_rejects_agent_office_action_when_intent_mismatches(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    organization = make_organization("Ayuntamiento Acción Mutante")
    user = make_user(full_name="Alcaldesa Acción Mutante")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "agent_office.create"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: SemanticTurnPlan(
            intent="read_requirements",
            action="create_agent_office_task",
            confidence=0.92,
            target={
                "organization_id": organization.id,
                "title": "No debe crearse",
                "description": "Plan con intención incompatible.",
                "requested_action": "triage",
            },
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [fake_response("end_turn", [text_block("No ejecuto esa acción directa.")])]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Consulta esto, pero el plan viene mal formado."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"] == []
    assert db.scalar(select(func.count()).select_from(AgentOfficeTask)) == 0
    assert gateway.calls


def test_status_reports_disabled_gateway(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    use_gateway(FakeGateway([], enabled=False))

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["planner"]["runtime"] == "disabled"
    assert body["planner"]["enabled"] is False
    assert {agent["key"] for agent in body["agents"]} == {
        "requirements_intake",
        "consultation",
    }
    tool_names = {tool["name"] for tool in body["tools"]}
    assert "create_requirement" in tool_names
    assert "web_search" not in tool_names


def test_transcribe_audio_requires_assistant_permission(client, make_user):
    user = make_user()

    response = client.post(
        "/assistant/audio-transcriptions",
        headers=headers_for(user),
        files={"file": ("voice.ogg", b"audio", "audio/ogg")},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_transcribe_audio_returns_text_for_assistant_user(
    client,
    assistant_user,
    monkeypatch,
):
    user, _ = assistant_user

    from app.assistant import routes as assistant_routes

    monkeypatch.setattr(
        assistant_routes,
        "transcribe_audio_bytes",
        lambda audio, language_code=None: "Necesito preparar un informe",
    )

    response = client.post(
        "/assistant/audio-transcriptions",
        headers=headers_for(user),
        files={"file": ("voice.ogg", b"audio", "audio/ogg")},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "Necesito preparar un informe"}


def test_prepare_audio_for_riva_transcodes_browser_webm(monkeypatch):
    from app.assistant import speech

    calls = []

    def fake_run(command, input, capture_output, check):
        calls.append(command)
        return SimpleNamespace(stdout=b"wav-pcm")

    monkeypatch.setattr(speech.subprocess, "run", fake_run)

    audio, encoding, sample_rate = speech.prepare_audio_for_riva(b"\x1a\x45\xdf\xa3webm")

    assert audio == b"wav-pcm"
    assert encoding == speech.riva_audio_encoding("LINEAR_PCM")
    assert sample_rate == 16000
    assert calls[0][:2] == ["ffmpeg", "-hide_banner"]


def test_status_reports_hermes_agent_runtime(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "hermes_agent")
    monkeypatch.setattr(settings, "hermes_agent_model", "hermes-agent-test")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    use_gateway(FakeGateway([], runtime_healthy=True))

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["runtime"] == "hermes_agent"
    assert body["model"] == "hermes-agent-test"
    assert body["runtime_healthy"] is True
    assert body["planner"]["runtime"] == "hermes_agent"
    assert body["planner"]["model"] == settings.assistant_planner_model


def test_hermes_agent_urls_support_v1_base_url(monkeypatch):
    monkeypatch.setattr(settings, "hermes_agent_base_url", "http://127.0.0.1:8642/v1")

    assert _hermes_agent_url("chat/completions") == (
        "http://127.0.0.1:8642/v1/chat/completions"
    )
    assert _hermes_agent_url("health") == "http://127.0.0.1:8642/health"


def test_hermes_agent_openai_tool_calls_are_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Voy a registrar el requisito.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "create_requirement",
                                    "arguments": json.dumps(
                                        {"title": "Cita previa"},
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 7},
        }
    )

    assert completion.model == "hermes-agent"
    assert completion.stop_reason == "tool_use"
    assert completion.usage.input_tokens == 10
    assert completion.usage.output_tokens == 7
    assert completion.content[0].text == "Voy a registrar el requisito."
    assert completion.content[1].name == "create_requirement"
    assert completion.content[1].input == {"title": "Cita previa"}


def test_hermes_agent_inline_tool_calls_are_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": (
                            'Hecho <tool_call>{"name":"propose_memory_entry",'
                            '"arguments":{"category":"protocol"}}</tool_call>'
                        ),
                    },
                }
            ],
        }
    )

    assert completion.stop_reason == "tool_use"
    assert completion.content[0].text == "Hecho"
    assert completion.content[1].name == "propose_memory_entry"
    assert completion.content[1].input == {"category": "protocol"}


def test_hermes_agent_standalone_json_tool_call_is_normalized():
    completion = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "name": "list_requirements",
                                "arguments": {"organization_id": 1},
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }
    )

    assert completion.stop_reason == "tool_use"
    assert len(completion.content) == 1
    assert completion.content[0].name == "list_requirements"
    assert completion.content[0].input == {"organization_id": 1}


def test_consultation_agent_has_only_read_only_tools():
    consultation = AGENT_REGISTRY["consultation"]

    assert all(
        assistant_tools.TOOL_CATALOG[tool_name].read_only
        for tool_name in consultation.tool_names
    )


def test_consultation_prompt_lists_read_tools(db, assistant_user):
    user, _ = assistant_user
    agent = AGENT_REGISTRY["consultation"]

    prompt = assistant_service.build_system_prompt(
        db,
        user,
        agent,
        get_agent_tools(agent),
    )

    assert "Agente activo: Consulta (consultation)" in prompt
    assert "Tu tarea es consultar información visible" in prompt
    assert "No digas que estás en modo consulta" in prompt
    assert "HERRAMIENTAS DISPONIBLES PARA ESTE AGENTE" in prompt
    assert "- list_requirements" in prompt
    assert "- get_requirement" in prompt
    assert "- create_requirement" not in prompt
    assert "No digas que no tienes una herramienta" in prompt
    assert "no respondas como si solo pudieras consultar" in prompt
    assert "crear o actualizar necesidades/requisitos como borrador" in prompt


def test_requirements_intake_prompt_lists_write_tools(db, assistant_user):
    user, _ = assistant_user
    agent = AGENT_REGISTRY["requirements_intake"]

    prompt = assistant_service.build_system_prompt(
        db,
        user,
        agent,
        get_agent_tools(agent),
    )

    assert "Agente activo: Necesidades (requirements_intake)" in prompt
    assert "Tu tarea es capturar necesidades" in prompt
    assert "- create_requirement" in prompt
    assert "- update_requirement" in prompt
    assert "- record_transversal_feature_acceptance" in prompt
    assert "borrador" in prompt


def test_agent_turn_persists_disabled_planner_routing(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Puedo ayudarte a capturar el requisito.")],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Necesitamos gestionar citas previas"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["routing"]["chosen"] == "requirements_intake"


def test_short_followup_keeps_previous_agent():
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content="¿De qué organización quieres que consulte los requisitos?",
                agent_key="consultation",
            )
        ]
    )

    decision = choose_agent(
        conversation=conversation,
        user_text="defaul",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "consultation"
    assert decision.routing["source"] == "shortcut"
    assert decision.routing["fallback_reason"] == "short_followup_previous_agent"


def test_greeting_new_chat_routes_to_general_consultation():
    conversation = SimpleNamespace(messages=[])

    decision = choose_agent(
        conversation=conversation,  # type: ignore[arg-type]
        user_text="hola",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "consultation"


def test_new_need_language_still_routes_to_requirements_intake():
    conversation = SimpleNamespace(messages=[])

    decision = choose_agent(
        conversation=conversation,  # type: ignore[arg-type]
        user_text="Necesitamos gestionar citas previas",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "requirements_intake"


def test_global_capability_question_returns_product_capabilities_without_gateway(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "hola, qué puedes hacer?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "solo consultar" not in normalized_content
    assert "crear o actualizar necesidades/requisitos como borrador" in normalized_content
    assert "buscar información pública actual" not in normalized_content
    assert "no apruebo trámites" in normalized_content
    assert gateway.calls == []


def test_ordinance_availability_question_answers_without_corpus_search(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcaldesa Ordenanzas")
    organization = make_organization(name="Ayuntamiento de Fuentelcésped")
    grant_permissions(user, organization, ["assistant.use", "ordinances.compare"])
    municipality = Municipality(
        name="Sasamón",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add(municipality)
    db.flush()
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title="Ordenanza reguladora de barracas",
        topic="ordenanzas municipales",
        ordinance_type="ordinance",
        source_url="https://example.test/sasamon.pdf",
        curation_status="approved",
        status="active",
    )
    db.add(ordinance)
    db.flush()
    embedding, model, status = embed_text("ordenanzas municipales Sasamón barracas")
    db.add(
        OrdinanceLegalChunk(
            ordinance_id=ordinance.id,
            chunk_index=0,
            citation="Fragmento 22",
            text="Las disposiciones de esta ordenanza son de aplicación en Sasamón.",
            source_url=ordinance.source_url,
            review_status="approved",
            embedding=embedding,
            embedding_model=model,
            embedding_status=status,
        )
    )
    db.commit()
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "dispones de ordenanzas municipales?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "ordinance_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "ordenanzas" in normalized_content
    assert "municipio" in normalized_content
    assert "materia" in normalized_content
    assert "sasamón" not in normalized_content
    assert "fragmento 22" not in normalized_content
    assert gateway.calls == []


def test_semantic_planner_global_capabilities_uses_plan_without_gateway(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="global_capabilities",
            action="none",
            confidence=0.9,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Explícame el alcance útil del producto."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    assert assistant_message["actions"] == []
    assert "crear o actualizar necesidades/requisitos como borrador" in assistant_message["content"].lower()
    assert gateway.calls == []


def test_map_location_question_executes_map_tool_without_gateway(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Mapa")
    organization = make_organization(name="Ayuntamiento de Fuentelcésped")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "map.view", "map.edit", "projects.view_all"],
    )
    project = Project(
        organization_id=organization.id,
        name="Demo mapa municipal",
        description="Proyecto con ubicación de prueba",
        status="active",
    )
    db.add(project)
    db.commit()
    assert client.post(
        "/geo/entity-locations",
        json={
            "entity_type": "project",
            "entity_id": project.id,
            "role": "primary",
            "location": {
                "label": "Plaza Mayor de Fuentelcésped",
                "latitude": 41.5917,
                "longitude": -3.6404,
            },
        },
        headers=headers_for(user),
    ).status_code == 201
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Hay algún proyecto con ubicación en el mapa?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "action_policy_read_map_items"
    assert assistant_message["routing"]["intent"] == "read_map_items"
    assert assistant_message["actions"][0]["tool"] == "get_map_items"
    assert assistant_message["actions"][0]["ok"] is True
    assert assistant_message["actions"][0]["input"]["entity_type"] == "project"
    action_result = json.loads(assistant_message["actions"][0]["result"])
    assert action_result["results"][0]["map_url"].startswith(
        "/mapa?entity_type=project"
    )
    assert "Demo mapa municipal" in assistant_message["content"]
    assert "/mapa?entity_type=project" in assistant_message["content"]
    assert gateway.calls == []


def test_semantic_planner_map_intent_executes_action_policy(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Mapa Semántico")
    organization = make_organization(name="Ayuntamiento Mapa Semántico")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "map.view", "map.edit", "projects.view_all"],
    )
    project = Project(
        organization_id=organization.id,
        name="Plan de accesibilidad",
        description="Proyecto con ubicación desde plan semántico",
        status="active",
    )
    db.add(project)
    db.commit()
    assert client.post(
        "/geo/entity-locations",
        json={
            "entity_type": "project",
            "entity_id": project.id,
            "role": "primary",
            "location": {
                "label": "Casa consistorial",
                "latitude": 41.5917,
                "longitude": -3.6404,
            },
        },
        headers=headers_for(user),
    ).status_code == 201
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="read_map_items",
            action="get_map_items",
            target={"organization_id": organization.id, "entity_type": "project"},
            confidence=0.93,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Enséñame lo geolocalizado."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_read_map_items"
    assert assistant_message["routing"]["intent"] == "read_map_items"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    action = assistant_message["actions"][0]
    assert action["tool"] == "get_map_items"
    assert action["input"] == {
        "organization_id": organization.id,
        "entity_type": "project",
        "limit": 5,
    }
    assert "Plan de accesibilidad" in assistant_message["content"]
    assert gateway.calls == []


def test_semantic_planner_map_intent_ignores_invalid_limit(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Límite Mapa")
    organization = make_organization(name="Ayuntamiento Límite Mapa")
    grant_permissions(user, organization, ["assistant.use", "map.view"])
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="read_map_items",
            action="get_map_items",
            target={"organization_id": organization.id, "limit": "cinco"},
            confidence=0.93,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Enséñame el mapa con un límite raro."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_read_map_items"
    assert assistant_message["actions"][0]["tool"] == "get_map_items"
    assert assistant_message["actions"][0]["input"]["limit"] == 5
    assert gateway.calls == []


def test_read_intents_are_backed_by_action_policies():
    expected = {
        "read_map_items": ("consultation", "get_map_items"),
        "read_requirements": ("consultation", "list_requirements"),
        "read_ordinances": ("consultation", "semantic_search_ordinances"),
    }

    for intent, (agent_key, tool_name) in expected.items():
        policy = assistant_service.ACTION_POLICIES[intent]
        assert policy.intent == intent
        assert policy.agent_key == agent_key
        assert policy.tool_name == tool_name
        assert policy.reason == f"action_policy_{intent}"


def test_requirement_capture_after_chat_intro_stays_conversational(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    requirement = Requirement(
        organization_id=organization.id,
        title="Mapa municipal",
        summary="Mostrar tareas y ubicaciones pendientes en el mapa.",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Cuéntame la idea y la trabajamos juntos.")],
                ),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Tiene sentido trabajarlo como una mejora del mapa. "
                            "Antes de pensar en guardarlo, aclaremos qué ve el alguacil."
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    intro = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "Vamos a hacer como que soy el alcalde y quiero contarte un "
                "nuevo requisito."
            )
        },
        headers=headers_for(user),
    )
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "En el mapa quiero que al alguacil se le pongan todas las cosas "
                "que puede tener pendientes o incluso que él pueda registrar cosas."
            )
        },
        headers=headers_for(user),
    )

    assert intro.status_code == 200
    intro_message = intro.json()["messages"][-1]
    assert intro_message["routing"]["source"] == "disabled"
    assert intro_message["actions"] == []
    assert "trabajamos juntos" in intro_message["content"].lower()
    assert "he comprobado" not in intro_message["content"].lower()
    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "he comprobado" not in normalized_content
    assert "mapa municipal" not in normalized_content
    assert "alguacil" in normalized_content
    assert len(gateway.calls) == 2


def test_register_need_request_delegates_to_conversation_without_duplicate_check(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    requirement = Requirement(
        organization_id=organization.id,
        title="control de personal municipal",
        summary=(
            "Me gustaría ir desarrollando una ficha de cada trabajador municipal "
            "con sus labores y calendarios."
        ),
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response("end_turn", [text_block("Hola, hacemos una prueba.")]),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Perfecto, cuéntame la idea con tus palabras y la vamos aterrizando."
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    intro = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "prueba"},
        headers=headers_for(user),
    )
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "me gustaría registrar una necesidad"},
        headers=headers_for(user),
    )

    assert intro.status_code == 200
    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "cuéntame la idea" in normalized_content
    assert "he comprobado" not in normalized_content
    assert "control de personal municipal" not in normalized_content
    assert len(gateway.calls) == 2


def test_semantic_planner_capture_requirement_delegates_to_gateway(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    requirement = Requirement(
        organization_id=organization.id,
        title="Mapa municipal",
        summary="Mostrar tareas y ubicaciones pendientes en el mapa.",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="capture_requirement",
            action="list_requirements",
            target={"organization_id": organization.id},
            query=(
                "En el mapa quiero que al alguacil se le pongan todas las cosas "
                "que puede tener pendientes."
            ),
            confidence=0.91,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Lo enfocaría como una conversación de diseño: "
                            "primero concretamos qué pendientes aparecen en el mapa."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "Quiero explorar una idea: en el mapa quiero que al alguacil "
                "se le pongan todas las cosas pendientes."
            )
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "he comprobado" not in normalized_content
    assert "mapa municipal" not in normalized_content
    assert "pendientes" in normalized_content
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_action" not in state
    assert len(gateway.calls) == 1


def test_semantic_planner_capture_requirement_intro_delegates_to_gateway(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="capture_requirement_intro",
            action="none",
            confidence=0.9,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Cuéntame qué quieres conseguir y lo vamos ordenando.")],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Quiero contarte un nuevo requisito para trabajarlo."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    assert "qué quieres conseguir" in assistant_message["content"].lower()
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_action" not in state
    assert len(gateway.calls) == 1


def test_requirement_capture_without_matches_does_not_announce_empty_check(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Cuéntame el contexto y lo vamos ordenando.")],
                ),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Podemos trabajarlo como una necesidad: quién usa las llaves, "
                            "qué hay que registrar y qué controles hacen falta."
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "quiero contarte un nuevo requisito"},
        headers=headers_for(user),
    )
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "Me gustaría que en la app se pudieran gestionar llaves municipales"
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "he comprobado" not in normalized_content
    assert "0 en total" not in normalized_content
    assert "qué controles hacen falta" in normalized_content
    assert len(gateway.calls) == 2


def test_classify_turn_intent_maps_common_direct_requests():
    assert assistant_service.classify_turn_intent(
        "hola, qué puedes hacer?"
    ) == assistant_service.TurnIntent("global_capabilities", "global_capabilities")
    assert assistant_service.classify_turn_intent(
        "Qué necesidades tenemos registradas?"
    ) == assistant_service.TurnIntent(
        "read_requirements",
        "direct_list_requirements",
        use_needs=True,
    )
    assert assistant_service.classify_turn_intent(
        "crea otra necesidad"
    ) == assistant_service.TurnIntent(
        "unknown",
        "unclassified",
    )
    assert assistant_service.classify_turn_intent(
        "crea un requisito de prueba"
    ) == assistant_service.TurnIntent(
        "create_test_requirement",
        "direct_create_test_requirement",
    )
    assert assistant_service.classify_turn_intent(
        "Vamos a hacer una prueba, como que soy el alcalde y quiero contarte un nuevo requisito"
    ) == assistant_service.TurnIntent(
        "unknown",
        "unclassified",
    )
    assert assistant_service.classify_turn_intent(
        "me gustaría registrar una necesidad"
    ) == assistant_service.TurnIntent(
        "unknown",
        "unclassified",
    )
    assert assistant_service.classify_turn_intent(
        "En el mapa quiero que el alguacil pueda registrar cosas"
    ) == assistant_service.TurnIntent(
        "unknown",
        "unclassified",
    )
    assert assistant_service.classify_turn_intent(
        "¿Dispones de ordenanzas municipales que se puedan contrastar de unos municipios y otros para poder verificar cuál sería más adecuada a las necesidades de mi municipio?"
    ) == assistant_service.TurnIntent(
        "read_ordinances",
        "direct_ordinance_search",
    )
    assert assistant_service.classify_turn_intent(
        "Pues la necesidad que tengo identificada es que ahora mismo querría desarrollar algo que me permita controlar a todos los trabajadores que hay en el ayuntamiento"
    ) == assistant_service.TurnIntent(
        "unknown",
        "unclassified",
    )


def test_invalid_planner_fallback_routes_new_need_to_requirements_intake(monkeypatch):
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content="¿De qué organización quieres consultarlo?",
                agent_key="consultation",
            )
        ]
    )
    monkeypatch.setattr(assistant_planner, "planner_enabled", lambda: True)
    monkeypatch.setattr(
        assistant_planner,
        "_route_with_hermes",
        lambda **kwargs: "Consulta",
    )

    decision = assistant_planner.choose_agent(
        conversation=conversation,
        user_text="quiero que el sistema pueda cargar datos en un mapa del pueblo",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "requirements_intake"
    assert decision.routing["source"] == "fallback"
    assert decision.routing["fallback_reason"] == "invalid_agent_key"
    assert decision.routing["raw_agent_key"] == "Consulta"


def test_valid_planner_choice_is_overridden_for_obvious_new_need(monkeypatch):
    conversation = SimpleNamespace(messages=[])
    monkeypatch.setattr(assistant_planner, "planner_enabled", lambda: True)
    monkeypatch.setattr(
        assistant_planner,
        "_route_with_hermes",
        lambda **kwargs: "consultation",
    )

    decision = assistant_planner.choose_agent(
        conversation=conversation,
        user_text="quiero que el sistema pueda cargar datos en un mapa del pueblo",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "requirements_intake"
    assert decision.routing["source"] == "shortcut"
    assert decision.routing["fallback_reason"] == "heuristic_override"
    assert decision.routing["raw_agent_key"] == "consultation"


def test_invalid_planner_fallback_keeps_explicit_read_requests_on_consultation(
    monkeypatch,
):
    conversation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant",
                content="He creado el borrador.",
                agent_key="requirements_intake",
            )
        ]
    )
    monkeypatch.setattr(assistant_planner, "planner_enabled", lambda: True)
    monkeypatch.setattr(
        assistant_planner,
        "_route_with_hermes",
        lambda **kwargs: "Consulta",
    )

    decision = assistant_planner.choose_agent(
        conversation=conversation,
        user_text="qué requisitos tenemos registrados",
        allowed_agents=list(AGENT_REGISTRY.values()),
    )

    assert decision.agent.key == "consultation"
    assert decision.routing["source"] == "fallback"
    assert decision.routing["fallback_reason"] == "invalid_agent_key"


def test_agent_tool_ceiling_blocks_tools_outside_selected_agent(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
    db,
):
    user, organization = assistant_user

    def fake_choose_agent(**kwargs):
        return SimpleNamespace(
            agent=AGENT_REGISTRY["consultation"],
            routing={
                "candidates": ["requirements_intake", "consultation"],
                "chosen": "consultation",
                "source": "router",
            },
        )

    monkeypatch.setattr(assistant_service, "choose_agent", fake_choose_agent)
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "No debería crearse",
                            },
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No puedo crear requisitos desde consulta.")],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Haz algo"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "No debería crearse")
    )
    assert requirement is None
    assistant_message = response.json()["messages"][1]
    assert assistant_message["agent_key"] == "consultation"
    action = assistant_message["actions"][0]
    assert action["ok"] is False
    assert "Herramienta no disponible para este agente" in action["result"]


def test_agent_recovers_hermes_argument_only_read_tool_call(
    assistant_user,
):
    _, organization = assistant_user
    response = fake_response(
        "end_turn",
        [
            text_block(
                f'{{"organization_id":{organization.id}}}\n'
                "La herramienta no ha devuelto resultados visibles."
            )
        ],
    )

    recovered = assistant_service.recover_textual_read_tool_call(
        response,
        get_agent_tools(AGENT_REGISTRY["consultation"]),
        [{"role": "user", "content": "Qué requisitos tenemos registrados?"}],
    )

    assert recovered.stop_reason == "tool_use"
    assert recovered.content[0].name == "list_requirements"
    assert recovered.content[0].input == {"organization_id": organization.id}


def test_direct_list_requirements_handles_default_empty_result(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    other = make_organization(name="Tenant Smoke B")
    grant_permissions(user, other, ["assistant.use", "requirements.view"])
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué requisitos tenemos registrados?"},
        headers=headers_for(user),
    )
    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "DEFAULT"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert "¿De qué organización" in first.json()["messages"][1]["content"]
    assert second.status_code == 200
    assistant_message = second.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "action_policy_read_requirements"
    assert first.json()["messages"][1]["routing"]["intent"] == "read_requirements"
    assert assistant_message["content"] == (
        f"No hay requisitos visibles registrados en {organization.name}."
    )
    action = assistant_message["actions"][0]
    assert action["tool"] == "list_requirements"
    assert action["ok"] is True
    assert action["input"] == {"organization_id": organization.id}
    assert json.loads(action["result"]) == []
    assert gateway.calls == []


def test_semantic_planner_requirements_intent_executes_action_policy(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    requirement = Requirement(
        organization_id=organization.id,
        title="Revisión de licencias",
        summary="Ordenar expedientes pendientes antes del pleno.",
        status="draft",
        priority="high",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="read_requirements",
            action="list_requirements",
            target={"organization_id": organization.id},
            confidence=0.94,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Dame una foto rápida del trabajo abierto."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_read_requirements"
    assert assistant_message["routing"]["intent"] == "read_requirements"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    action = assistant_message["actions"][0]
    assert action["tool"] == "list_requirements"
    assert action["input"] == {"organization_id": organization.id}
    assert "Revisión de licencias" in assistant_message["content"]
    assert gateway.calls == []


def test_semantic_planner_create_requirement_delegates_grounded_draft(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="create_requirement",
            action="create_requirement",
            target={"organization_id": organization.id},
            draft={
                "title": "Inventario de caminos rurales",
                "problem": "El ayuntamiento necesita registrar el estado de los caminos rurales.",
            },
            confidence=0.95,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Puedo ayudarte a darle forma antes de guardarlo. "
                            "Primero reviso contigo si ese borrador está completo."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Apunta esto como trabajo nuevo municipal."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Inventario de caminos rurales")
    )
    assert requirement is None
    assert len(gateway.calls) == 1


def test_semantic_planner_create_requirement_does_not_override_into_direct_create(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="create_requirement",
            action="create_requirement",
            target={"organization_id": organization.id},
            draft={
                "title": "Portal de reservas municipales",
                "problem": "El ayuntamiento necesita que los vecinos reserven espacios municipales.",
            },
            confidence=0.96,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Sí, puedo ayudarte a preparar ese requisito y después "
                            "lo guardamos solo si confirmas el borrador."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "¿Puedes crear un requisito para esto: portal de reservas municipales?"
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Portal de reservas municipales")
    )
    assert requirement is None
    assert len(gateway.calls) == 1


def test_semantic_planner_create_requirement_missing_fields_delegates_to_gateway(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="create_requirement",
            action="create_requirement",
            target={"organization_id": organization.id},
            draft={"title": "Control de llaves municipales"},
            confidence=0.93,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Vamos a aterrizar esa idea. Cuéntame qué problema "
                            "quiere resolver el control de llaves."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Guárdame esta idea para trabajarla."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"] == []
    assert assistant_message["routing"]["source"] == "disabled"
    assert "problema" in assistant_message["content"].lower()

    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_action" not in state
    assert len(gateway.calls) == 1


def test_planned_requirement_with_title_and_problem_stays_conversational(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")

    def planned_turn(**kwargs):
        text = kwargs["user_text"]
        if "registrar una nueva necesidad" in text.lower():
            return assistant_planner.SemanticTurnPlan(
                intent="capture_requirement_intro",
                action="none",
                confidence=0.91,
                source="planner",
            )
        return assistant_planner.SemanticTurnPlan(
            intent="create_requirement",
            action="create_requirement",
            target={"organization_id": organization.id},
            draft=assistant_service.extract_requirement_draft_from_text(text),
            confidence=0.94,
            source="planner",
        )

    monkeypatch.setattr(assistant_service, "plan_turn", planned_turn)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Cuéntame la idea y vemos cómo darle forma.")],
                ),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Entiendo: queréis una zona del dashboard para planos "
                            "digitalizados de instalaciones municipales. Podemos "
                            "perfilar alcance, usuarios y revisión antes de guardarlo."
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    intro = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola, quiero registrar una nueva necesidad."},
        headers=headers_for(user),
    )
    proposal = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "El título quiero que sea plano de instalaciones del ayuntamiento "
                "y el problema que quiero resolver es que quiero aportar "
                "digitalizado un plano del municipio y en este caso habrá que "
                "hacer en el dashboard una parte que se puede incluir planos "
                "donde se vean los distintos planos de instalaciones del municipio."
            )
        },
        headers=headers_for(user),
    )

    assert intro.status_code == 200
    intro_message = intro.json()["messages"][-1]
    assert intro_message["content"] == "Cuéntame la idea y vemos cómo darle forma."
    assert intro_message["actions"] == []
    assert proposal.status_code == 200
    assistant_message = proposal.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "dashboard" in normalized_content
    assert "guardarlo" in normalized_content
    assert "¿quieres que lo guarde como borrador" not in normalized_content
    assert (
        db.scalar(
            select(Requirement).where(
                Requirement.title == "plano de instalaciones del ayuntamiento"
            )
        )
        is None
    )
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_work" not in state
    assert len(gateway.calls) == 2


def test_semantic_planner_create_requirement_followup_merges_pending_draft(
    client,
    assistant_user,
    db,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")

    def planned_turn(**kwargs):
        text = kwargs["user_text"]
        if text.lower().strip() in {"sí", "si"}:
            return assistant_planner.SemanticTurnPlan(
                intent="confirm_pending_work",
                action="confirm_pending_work",
                confidence=0.93,
                source="planner",
            )
        return assistant_planner.SemanticTurnPlan(
            intent="unknown",
            action="none",
            confidence=0.93,
            source="planner",
        )

    monkeypatch.setattr(assistant_service, "plan_turn", planned_turn)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Anoto el título. Para completarlo, cuéntame el problema."
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Tengo este posible borrador:\n\n"
                            "Título: Control de llaves municipales\n"
                            "Problema: No hay un registro común de quién tiene cada copia.\n\n"
                            "¿Quieres que lo guarde o lo seguimos matizando?"
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "El título sería Control de llaves municipales."},
        headers=headers_for(user),
    )
    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "El problema es que no hay un registro común de quién tiene cada copia."
        },
        headers=headers_for(user),
    )
    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert proposed.status_code == 200
    assert "Título: Control de llaves municipales" in proposed.json()["messages"][-1]["content"]
    assert second.status_code == 200
    assistant_message = second.json()["messages"][-1]
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Control de llaves municipales")
    )
    assert requirement is not None
    assert requirement.problem == "No hay un registro común de quién tiene cada copia."
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_action" not in state
    assert "pending_work" not in state
    assert len(gateway.calls) == 2


def test_direct_empty_requirements_followup_uses_last_result(
    client,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    listed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué requisitos tenemos registrados?"},
        headers=headers_for(user),
    )
    followup = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "puede ser por qué no hay ningún requisito?"},
        headers=headers_for(user),
    )

    assert listed.status_code == 200
    assert followup.status_code == 200
    assistant_message = followup.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["actions"] == []
    assert f"0 requisitos visibles en {organization.name}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_empty_needs_followup_uses_last_result(
    client,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    listed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué necesidades tenemos registradas?"},
        headers=headers_for(user),
    )
    followup = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "puede ser por qué no hay ninguna necesidad?"},
        headers=headers_for(user),
    )

    assert listed.status_code == 200
    assert followup.status_code == 200
    assistant_message = followup.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["actions"] == []
    assert f"0 necesidades visibles en {organization.name}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_create_test_requirement_confirmed(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    ask_content = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea un requisito de prueba"},
        headers=headers_for(user),
    )
    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "tú decides"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert ask_content.status_code == 200
    assert "necesito al menos confirmar" in ask_content.json()["messages"][-1]["content"]
    assert proposed.status_code == 200
    assert "¿Confirmas que lo cree como borrador?" in proposed.json()["messages"][-1]["content"]
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Requisito de prueba")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.status == "draft"
    assert requirement.source_type == "conversation"
    assert requirement.created_by_id == user.id
    assert f"borrador #{requirement.id}" in assistant_message["content"]
    assert gateway.calls == []


def test_direct_create_test_requirement_avoids_duplicate(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    db.add(
        Requirement(
            organization_id=organization.id,
            title="Requisito de prueba",
            status="draft",
            source_type="conversation",
            created_by_id=user.id,
        )
    )
    db.commit()
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea un requisito de prueba"},
        headers=headers_for(user),
    )
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "tú decides"},
        headers=headers_for(user),
    )
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert "No he creado un duplicado" in assistant_message["content"]
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements"
    ]
    assert (
        db.scalar(
            select(func.count()).select_from(Requirement).where(
                Requirement.title == "Requisito de prueba"
            )
        )
        == 1
    )
    assert gateway.calls == []


def test_create_another_requirement_delegates_without_form_shortcut(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Cuéntame qué quieres conseguir con ese requisito "
                            "y en qué organización lo trabajamos."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea otro requisito"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "cuéntame" in normalized_content
    assert "título breve" not in normalized_content
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "prueba2")
    )
    assert requirement is None
    assert len(gateway.calls) == 1


def test_create_another_need_delegates_without_form_shortcut(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Vale, empecemos por entender la necesidad y luego "
                            "vemos si conviene guardarla como borrador."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea otra necesidad"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["agent_key"] == "requirements_intake"
    assert assistant_message["routing"]["source"] == "disabled"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "necesidad" in normalized_content
    assert "título breve" not in normalized_content
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "prueba2")
    )
    assert requirement is None
    assert len(gateway.calls) == 1


def test_confirming_exact_generic_map_proposal_creates_draft_deterministically(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Claro. ¿Para qué organización quieres plantearlo?\n\n"
                            "Y para entenderlo bien: ¿qué tipo de información queréis guardar en el mapa?"
                        )
                    ],
                ),
                fake_response(
                    "stop",
                    [
                        text_block(
                            "De acuerdo, lo planteamos en “Default organization” salvo que luego me digas otra.\n\n"
                            "Para poder guardarlo como requisito necesito concretar un poco:\n"
                            "¿Qué problema queréis resolver hoy con ese mapa?"
                        )
                    ],
                ),
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Puedo prepararlo como requisito genérico, pero necesito tu OK sobre este enfoque:\n\n"
                            "Título: “Gestión de información municipal desde un mapa”\n"
                            "Problema: “El ayuntamiento necesita registrar, consultar y actualizar información geolocalizada en un mapa para facilitar su gestión diaria.”\n\n"
                            "¿Te vale así como borrador para la organización “Default organization”?"
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "quiero que podamos guardar y gestionar desde un mapa información"},
        headers=headers_for(user),
    )
    organization_reply = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "cualquiera"},
        headers=headers_for(user),
    )
    proposal = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "cualquiera"},
        headers=headers_for(user),
    )
    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert organization_reply.status_code == 200
    assert proposal.status_code == 200
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(
            Requirement.title == "Gestión de información municipal desde un mapa"
        )
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.status == "draft"
    assert "información geolocalizada" in requirement.problem
    assert len(gateway.calls) == 3



def test_confirming_gateway_proposed_need_creates_draft_deterministically(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Perfecto. ¿Para qué organización quieres registrarlo?"
                        )
                    ],
                ),
                fake_response(
                    "stop",
                    [
                        text_block(
                            "Para guardarlo necesito concretar un poco más el borrador.\n\n"
                            "Te propongo este enfoque:\n"
                            "Título: “Carga y visualización de datos municipales en mapa”\n"
                            "Problema: “El ayuntamiento necesita centralizar en un mapa del municipio distintos datos útiles para consulta y gestión.”\n\n"
                            "¿Te encaja así?"
                        )
                    ],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "quiero que el sistema pueda cargar datos en un mapa del pueblo"},
        headers=headers_for(user),
    )
    proposal = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "default. todo tipo de datos que puedan ser útiles"},
        headers=headers_for(user),
    )

    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert state["pending_work"] == {
        "type": "create_requirement",
        "status": "awaiting_confirmation",
        "organization_id": organization.id,
        "draft": {
            "title": "Carga y visualización de datos municipales en mapa",
            "problem": "El ayuntamiento necesita centralizar en un mapa del municipio distintos datos útiles para consulta y gestión.",
        },
    }

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "me encaja y se cargarán de ambos"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert proposal.status_code == 200
    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(
            Requirement.title == "Carga y visualización de datos municipales en mapa"
        )
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.status == "draft"
    assert "centralizar en un mapa" in requirement.problem
    assert len(gateway.calls) == 2


def test_affirmative_after_legacy_unpersisted_proposal_recovers_and_creates_draft(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(FakeGateway([]))
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {"selected_organization_id": organization.id},
        ensure_ascii=False,
    )
    db.add(
        AssistantMessage(
            conversation_id=stored_conversation.id,
            role="assistant",
            agent_key="requirements_intake",
            content=(
                "Puedo prepararlo como requisito genérico, pero necesito tu OK sobre este enfoque:\n\n"
                "Título: “Gestión de información municipal desde un mapa”\n"
                "Problema: “El ayuntamiento necesita registrar, consultar y actualizar información geolocalizada en un mapa para facilitar su gestión diaria.”\n\n"
                "¿Te vale así como borrador para la organización “Default organization”?"
            ),
        )
    )
    db.commit()

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "legacy_proposal_create_requirement_confirmed"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(
            Requirement.title == "Gestión de información municipal desde un mapa"
        )
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert "información geolocalizada" in requirement.problem
    assert gateway.calls == []



def test_confirming_pending_work_creates_need_without_reparsing_assistant_text(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Default organization")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(FakeGateway([]))
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "selected_organization_id": organization.id,
            "pending_work": {
                "type": "create_requirement",
                "status": "awaiting_confirmation",
                "organization_id": organization.id,
                "draft": {
                    "title": "Mapa municipal de datos",
                    "problem": "Centralizar datos municipales útiles sobre un mapa del pueblo.",
                },
            },
        },
        ensure_ascii=False,
    )
    db.add(
        AssistantMessage(
            conversation_id=stored_conversation.id,
            role="assistant",
            content="Tengo una propuesta pendiente. ¿La guardo?",
        )
    )
    db.commit()

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "me encaja"},
        headers=headers_for(user),
    )

    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Mapa municipal de datos")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert requirement.problem == "Centralizar datos municipales útiles sobre un mapa del pueblo."
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    updated_state = json.loads(updated_conversation.state or "{}")
    assert "pending_work" not in updated_state
    assert gateway.calls == []


def test_semantic_planner_confirm_pending_work_creates_need_without_phrase_match(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Ayuntamiento Confirmación Semántica")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="confirm_pending_work",
            action="confirm_pending_work",
            confidence=0.96,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "selected_organization_id": organization.id,
            "pending_work": {
                "type": "create_requirement",
                "status": "awaiting_confirmation",
                "organization_id": organization.id,
                "draft": {
                    "title": "Inventario de señales viarias",
                    "problem": "Centralizar las señales viarias pendientes de revisión.",
                },
            },
        },
        ensure_ascii=False,
    )
    db.add(stored_conversation)
    db.commit()

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "proceda conforme a lo previsto"},
        headers=headers_for(user),
    )

    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_confirm_pending_work"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Inventario de señales viarias")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert "señales viarias" in requirement.problem
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    updated_state = json.loads(updated_conversation.state or "{}")
    assert "pending_work" not in updated_state
    assert gateway.calls == []


def test_natural_confirmation_of_pending_work_creates_need(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Ayuntamiento de Fuentelcésped")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.create", "requirements.view"],
    )
    gateway = use_gateway(FakeGateway([]))
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "selected_organization_id": organization.id,
            "pending_work": {
                "type": "create_requirement",
                "status": "awaiting_confirmation",
                "organization_id": organization.id,
                "draft": {
                    "title": "Control de personal municipal",
                    "problem": "Tener fichas de trabajadores con labores, competencias, diagrama de actividad, calendarios de trabajo y prioridades.",
                },
            },
        },
        ensure_ascii=False,
    )
    db.commit()

    created = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "Te confirmo que quiero que registres esa, la que hemos comentado antes, la que por título tenía control de personal municipal."
        },
        headers=headers_for(user),
    )

    assert created.status_code == 200
    assistant_message = created.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "pending_work_create_requirement_confirmed"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Control de personal municipal")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    assert gateway.calls == []


def test_model_requirement_create_failure_does_not_start_retry_shortcut(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Ayuntamiento de Fuentelcésped")
    grant_permissions(user, organization, ["assistant.use", "requirements.view"])
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "control de personal municipal",
                                "problem": "tener una ficha de cada trabajador municipal.",
                            },
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "No he podido guardarlo con tus permisos actuales; "
                            "lo podemos dejar preparado para revisión."
                        )
                    ],
                ),
            ]
        )
    )
    conversation_response = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    conversation = conversation_response.json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {"selected_organization_id": organization.id},
        ensure_ascii=False,
    )
    db.add(
        AssistantMessage(
            conversation_id=stored_conversation.id,
            role="assistant",
            agent_key="requirements_intake",
            content="Para empezar necesito dos datos: título de la necesidad y problema que queréis resolver.",
        )
    )
    db.commit()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "Regístralo. El título sería control de personal municipal "
                "y el problema que quiero resolver es tener una ficha de cada "
                "trabajador municipal."
            )
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"][-1]["tool"] == "create_requirement"
    assert assistant_message["actions"][-1]["ok"] is False
    assert "requirements.create" in assistant_message["actions"][-1]["result"]
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    state = json.loads(updated_conversation.state or "{}")
    assert "pending_action" not in state
    assert len(gateway.calls) == 2


def test_semantic_planner_retries_pending_create_requirement_without_phrase_match(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcalde Test")
    organization = make_organization(name="Ayuntamiento Reintento Semántico")
    grant_permissions(
        user,
        organization,
        ["assistant.use", "requirements.view", "requirements.create"],
    )
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="retry_pending_action",
            action="retry_pending_action",
            confidence=0.94,
            source="planner",
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    stored_conversation.state = json.dumps(
        {
            "selected_organization_id": organization.id,
            "pending_action": {
                "type": "create_requirement_retry",
                "organization_id": organization.id,
                "draft": {
                    "title": "Control de personal municipal",
                    "problem": "Tener una ficha de cada trabajador municipal.",
                },
            },
        },
        ensure_ascii=False,
    )
    db.add(stored_conversation)
    db.commit()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "proceda de nuevo con lo anterior"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_retry_pending_action"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "list_requirements",
        "create_requirement",
    ]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Control de personal municipal")
    )
    assert requirement is not None
    assert requirement.organization_id == organization.id
    db.expire_all()
    updated_conversation = db.get(AssistantConversation, conversation["id"])
    assert updated_conversation is not None
    assert "pending_action" not in json.loads(updated_conversation.state or "{}")
    assert gateway.calls == []


def test_create_capability_question_answers_without_starting_intake(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "y puedes crear un requisito en default desde aquí?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "claro. la creo" not in normalized_content
    assert "título breve" not in normalized_content
    assert "crear o actualizar necesidades/requisitos como borrador" in normalized_content
    assert gateway.calls == []


def test_direct_create_agent_reference_switches_to_intake_without_internal_copy(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    default_organization = make_organization(name="Default organization")
    other_organization = make_organization(name="Otra organización")
    for organization in (default_organization, other_organization):
        grant_permissions(
            user,
            organization,
            ["assistant.use", "requirements.create", "requirements.view"],
        )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Qué requisitos tenemos registrados?"},
        headers=headers_for(user),
    )
    client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "DEFAULT"},
        headers=headers_for(user),
    )

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "bueno pero puedes llamar al agente de crear requisitos no?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "global_capabilities"
    assert assistant_message["routing"]["intent"] == "global_capabilities"
    assert assistant_message["actions"] == []
    normalized_content = assistant_message["content"].lower()
    assert "solo lectura" not in normalized_content
    assert "copia" not in normalized_content
    assert "agente" not in normalized_content
    assert "crear o actualizar necesidades/requisitos como borrador" in normalized_content
    assert gateway.calls == []


def test_create_another_requirement_with_labeled_content_waits_for_model(
    client,
    db,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    monkeypatch.setattr(settings, "assistant_runtime", "anthropic")
    monkeypatch.setattr(settings, "assistant_planner_runtime", "disabled")
    user, organization = assistant_user
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "Cuéntame el requisito y preparo una propuesta antes de guardarlo."
                        )
                    ],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    ask_content = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "crea otro requisito"},
        headers=headers_for(user),
    )

    assert ask_content.status_code == 200
    assistant_message = ask_content.json()["messages"][-1]
    assert assistant_message["actions"] == []
    assert "preparo una propuesta" in assistant_message["content"].lower()
    assert "título breve" not in assistant_message["content"].lower()
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "prueba 2")
    )
    assert requirement is None
    assert len(gateway.calls) == 1


def test_conversations_are_private_to_their_creator(
    client,
    assistant_user,
    make_user,
    make_organization,
    grant_permissions,
):
    user, _ = assistant_user
    other = make_user()
    other_org = make_organization()
    grant_permissions(other, other_org, ["assistant.use"])

    created = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    own_list = client.get("/assistant/conversations", headers=headers_for(user))
    assert [c["id"] for c in own_list.json()] == [conversation_id]

    foreign_list = client.get("/assistant/conversations", headers=headers_for(other))
    assert foreign_list.json() == []

    foreign_get = client.get(
        f"/assistant/conversations/{conversation_id}",
        headers=headers_for(other),
    )
    assert foreign_get.status_code == 404


def test_send_message_when_gateway_disabled_returns_503(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([], enabled=False))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Assistant is not configured"


def test_agent_turn_creates_requirement_draft_with_audit_trail(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        text_block("Voy a registrar el requisito."),
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "Cita previa para padrón",
                                "problem": "Colas en el registro",
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("He creado el borrador del requisito.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "Crea un requisito. El título es Cita previa para padrón "
                "y el problema es Colas en el registro"
            )
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    detail = response.json()

    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Cita previa para padrón")
    )
    assert requirement is not None
    assert requirement.status == "draft"
    assert requirement.source_type == "conversation"
    assert requirement.created_by_id == user.id
    assert requirement.problem == "Colas en el registro"

    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["user", "assistant"]
    assistant_message = detail["messages"][1]
    assert assistant_message["content"] == "He creado el borrador del requisito."
    assert len(assistant_message["actions"]) == 1
    action = assistant_message["actions"][0]
    assert action["tool"] == "create_requirement"
    assert action["ok"] is True
    assert json.loads(action["result"])["id"] == requirement.id

    # Conversation title is taken from the first user message.
    assert detail["title"] == (
        "Crea un requisito. El título es Cita previa para padrón "
        "y el problema es Colas en el registro"
    )
    # Two API round-trips: tool call + final reply.
    assert len(gateway.calls) == 2


def test_agent_create_requirement_tool_requires_explicit_confirmation(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "Plano de instalaciones",
                                "problem": "Digitalizar planos municipales.",
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Lo preparo como propuesta antes de guardarlo.")],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "El título es Plano de instalaciones y el problema es "
                "Digitalizar planos municipales."
            )
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    action = assistant_message["actions"][0]
    assert action["tool"] == "create_requirement"
    assert action["ok"] is False
    assert "confirmación explícita" in action["result"]
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Plano de instalaciones")
    )
    assert requirement is None
    assert len(gateway.calls) == 2


def test_agent_tool_respects_rbac_of_current_user(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    organization = make_organization()
    # assistant.use but NOT requirements.create
    grant_permissions(user, organization, ["assistant.use"])

    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "create_requirement",
                            {
                                "organization_id": organization.id,
                                "title": "Requisito sin permiso",
                                "problem": "Comprobar control de permisos.",
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No tienes permiso para crear requisitos.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": (
                "Crea un requisito. El título es Requisito sin permiso "
                "y el problema es Comprobar control de permisos."
            )
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    requirement = db.scalar(
        select(Requirement).where(Requirement.title == "Requisito sin permiso")
    )
    assert requirement is None

    action = response.json()["messages"][1]["actions"][0]
    assert action["ok"] is False
    assert "requirements.create" in action["result"]


def test_agent_web_search_uses_controlled_hermes_web_tool(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    monkeypatch.setattr(settings, "hermes_web_api_key", "test-key")
    calls = []

    def fake_search(*, query: str, limit: int):
        calls.append({"query": query, "limit": limit})
        return [
            {
                "title": "Normativa ejemplo",
                "url": "https://example.test/normativa",
                "snippet": "Resumen público de la fuente.",
                "published_at": None,
            }
        ]

    monkeypatch.setattr(assistant_tools.hermes_web_client, "search", fake_search)
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "web_search",
                            {
                                "query": "normativa municipal 2026",
                                "limit": 20,
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("He encontrado una fuente pública.")],
                ),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca si hay novedades de normativa municipal"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert calls == [{"query": "normativa municipal 2026", "limit": 5}]

    action = response.json()["messages"][1]["actions"][0]
    assert action["tool"] == "web_search"
    assert action["ok"] is True
    result = json.loads(action["result"])
    assert result["query"] == "normativa municipal 2026"
    assert result["limit"] == 5
    assert result["results"][0]["url"] == "https://example.test/normativa"


def test_agent_web_search_is_unavailable_without_permission(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use"])

    gateway = FakeGateway([])
    use_gateway(gateway)

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca en internet"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][1]
    assert assistant_message["content"] == assistant_service.WEB_SEARCH_UNAVAILABLE_REPLY
    assert assistant_message["actions"] == []
    assert gateway.calls == []


def test_agent_web_search_is_unavailable_when_server_is_not_configured(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "assistant.web.search"])
    monkeypatch.setattr(settings, "hermes_web_api_key", None)
    gateway = FakeGateway([])
    use_gateway(gateway)

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca en internet"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][1]
    assert assistant_message["content"] == assistant_service.WEB_SEARCH_UNAVAILABLE_REPLY
    assert assistant_message["actions"] == []
    assert gateway.calls == []


def test_consultation_agent_can_search_approved_ordinance_chunks(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["ordinances.compare"])
    municipality = Municipality(
        name="Villarcayo",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add(municipality)
    db.flush()
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title="Ordenanza municipal de residuos",
        topic="residuos",
        ordinance_type="ordinance",
        source_url="https://bopbur.diputaciondeburgos.es/anuncio/residuos.pdf",
        curation_status="approved",
        status="active",
    )
    db.add(ordinance)
    db.flush()
    embedding, model, status = embed_text("recogida de residuos")
    db.add(
        OrdinanceLegalChunk(
            ordinance_id=ordinance.id,
            chunk_index=0,
            citation="Artículo 1",
            text="La recogida de residuos se realizará en los horarios establecidos.",
            source_url=ordinance.source_url,
            review_status="approved",
            embedding=embedding,
            embedding_model=model,
            embedding_status=status,
        )
    )
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "semantic_search_ordinances",
        {"query": "recogida de residuos", "municipality_id": municipality.id},
    )

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["query"] == "recogida de residuos"
    assert payload["results"][0]["title"] == "Ordenanza municipal de residuos"
    assert payload["results"][0]["municipality_name"] == "Villarcayo"
    assert payload["results"][0]["citation"] == "Artículo 1"
    assert payload["results"][0]["source_url"] == ordinance.source_url


def test_agent_turn_searches_ordinances_with_structured_filters(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "ordinances.compare"])
    municipality = Municipality(
        name="Miranda de Ebro",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add(municipality)
    db.flush()
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title="Modificación de varias ordenanzas fiscales",
        topic="ordenanzas fiscales",
        subtopic="IBI, impuestos y tasas municipales",
        ordinance_type="tax_ordinance",
        source_url="https://bopbur.diputaciondeburgos.es/anuncio/miranda.pdf",
        curation_status="approved",
        status="active",
    )
    db.add(ordinance)
    db.flush()
    embedding, model, status = embed_text(
        "Modificación del impuesto sobre bienes inmuebles y tasas municipales."
    )
    db.add(
        OrdinanceLegalChunk(
            ordinance_id=ordinance.id,
            chunk_index=0,
            citation="Artículo 1",
            text="Modificación del impuesto sobre bienes inmuebles y tasas municipales.",
            source_url=ordinance.source_url,
            review_status="approved",
            embedding=embedding,
            embedding_model=model,
            embedding_status=status,
        )
    )
    db.commit()

    monkeypatch.setattr(
        assistant_service,
        "choose_agent",
        lambda **kwargs: SimpleNamespace(
            agent=AGENT_REGISTRY["consultation"],
            routing={"chosen": "consultation", "source": "test"},
        ),
    )
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Qué dice Miranda de Ebro sobre el IBI?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["agent_key"] == "consultation"
    assert assistant_message["routing"]["source"] == "deterministic"
    assert assistant_message["routing"]["reason"] == "action_policy_read_ordinances"
    assert assistant_message["routing"]["intent"] == "read_ordinances"
    assert "Miranda de Ebro" in assistant_message["content"]
    action = assistant_message["actions"][0]
    assert action["tool"] == "semantic_search_ordinances"
    assert action["ok"] is True
    assert action["input"]["municipality_name"] == "Miranda de Ebro"
    assert action["input"]["topic"] == "ordenanzas fiscales"
    assert '"municipality_name": "Miranda de Ebro"' in action["result"]
    assert '"topic": "ordenanzas fiscales"' in action["result"]
    assert gateway.calls == []


def test_broad_ordinance_question_uses_ordinance_policy_not_needs_listing(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcalde Test")
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add(municipality)
    db.commit()
    organization = make_organization(
        name="Ayuntamiento de Fuentelcésped",
        municipality_id=municipality.id,
    )
    grant_permissions(user, organization, ["assistant.use", "ordinances.compare"])
    gateway = use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "¿Dispones de ordenanzas municipales que se puedan contrastar de unos municipios y otros para poder verificar cuál sería más adecuada a las necesidades de mi municipio?"
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["reason"] == "action_policy_read_ordinances"
    assert assistant_message["routing"]["intent"] == "read_ordinances"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "semantic_search_ordinances"
    ]
    assert assistant_message["actions"][0]["input"]["municipality_name"] == "Fuentelcésped"
    assert "necesidades visibles" not in assistant_message["content"].lower()
    assert gateway.calls == []


def test_semantic_planner_ordinance_intent_executes_grounded_action(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcalde Test")
    municipality = Municipality(
        name="Fuentelcésped",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add(municipality)
    db.commit()
    organization = make_organization(
        name="Ayuntamiento de Fuentelcésped",
        municipality_id=municipality.id,
    )
    grant_permissions(user, organization, ["assistant.use", "ordinances.compare"])
    gateway = use_gateway(FakeGateway([]))
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="read_ordinances",
            action="semantic_search_ordinances",
            query="normas comparables para adaptar al municipio",
            target={"municipality_name": "Fuentelcésped"},
            confidence=0.92,
            source="planner",
        ),
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={
            "content": "¿Qué normas de otros pueblos me sirven para adaptar las de aquí?"
        },
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["routing"]["intent"] == "read_ordinances"
    assert assistant_message["routing"]["reason"] == "action_policy_read_ordinances"
    assert assistant_message["routing"]["semantic_plan"]["source"] == "planner"
    assert [action["tool"] for action in assistant_message["actions"]] == [
        "semantic_search_ordinances"
    ]
    assert assistant_message["actions"][0]["input"] == {
        "query": "normas comparables para adaptar al municipio",
        "municipality_name": "Fuentelcésped",
    }
    assert gateway.calls == []


def test_semantic_planner_unknown_vetoes_keyword_ordinance_route(
    client,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user = make_user(full_name="Alcaldesa Sin Ruta")
    organization = make_organization(name="Ayuntamiento Sin Ruta")
    grant_permissions(user, organization, ["assistant.use", "ordinances.compare"])
    monkeypatch.setattr(
        assistant_service,
        "plan_turn",
        lambda **kwargs: assistant_planner.SemanticTurnPlan(
            intent="unknown",
            action="none",
            confidence=0.91,
            source="planner",
        ),
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Lo reviso contigo sin ejecutar una búsqueda normativa.")],
                )
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "La palabra ordenanza aparece aquí, pero no es una consulta."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["content"] == "Lo reviso contigo sin ejecutar una búsqueda normativa."
    assert assistant_message["actions"] == []
    assert len(gateway.calls) == 1


def test_ordinance_semantic_search_tool_filters_by_municipality_name_and_topic(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["ordinances.compare"])
    miranda = Municipality(
        name="Miranda de Ebro",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    cascajares = Municipality(
        name="Cascajares de la Sierra",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add_all([miranda, cascajares])
    db.flush()
    miranda_ordinance = Ordinance(
        municipality_id=miranda.id,
        title="Modificación de varias ordenanzas fiscales",
        topic="ordenanzas fiscales",
        subtopic="IBI, impuestos y tasas municipales",
        ordinance_type="tax_ordinance",
        source_url="https://bopbur.diputaciondeburgos.es/anuncio/miranda.pdf",
        curation_status="approved",
        status="active",
    )
    other_ordinance = Ordinance(
        municipality_id=cascajares.id,
        title="Ordenanza de leñas de hogar",
        topic="montes municipales",
        ordinance_type="ordinance",
        source_url="https://bopbur.diputaciondeburgos.es/anuncio/lenas.pdf",
        curation_status="approved",
        status="active",
    )
    db.add_all([miranda_ordinance, other_ordinance])
    db.flush()
    for ordinance, text in (
        (
            miranda_ordinance,
            "Modificación del impuesto sobre bienes inmuebles y tasas municipales.",
        ),
        (other_ordinance, "Aprovechamiento de leñas de hogar en montes municipales."),
    ):
        embedding, model, status = embed_text(text)
        db.add(
            OrdinanceLegalChunk(
                ordinance_id=ordinance.id,
                chunk_index=0,
                citation="Artículo 1",
                text=text,
                source_url=ordinance.source_url,
                review_status="approved",
                embedding=embedding,
                embedding_model=model,
                embedding_status=status,
            )
        )
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "semantic_search_ordinances",
        {
            "query": "Modificación del impuesto sobre bienes inmuebles y tasas municipales",
            "municipality_name": "Miranda de Ebro",
            "topic": "ordenanzas fiscales",
        },
    )

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["municipality_name"] == "Miranda de Ebro"
    assert payload["topic"] == "ordenanzas fiscales"
    assert [row["municipality_name"] for row in payload["results"]] == [
        "Miranda de Ebro"
    ]
    assert payload["results"][0]["topic"] == "ordenanzas fiscales"


def test_ordinance_semantic_search_tool_requires_compare_permission(
    db,
    make_user,
):
    user = make_user()

    result = assistant_tools.execute_tool(
        db,
        user,
        "semantic_search_ordinances",
        {"query": "recogida de residuos"},
    )

    assert result.ok is False
    assert "ordinances.compare" in result.content


def test_ordinance_semantic_search_tool_returns_empty_without_approved_coverage(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["ordinances.compare"])
    municipality = Municipality(
        name="Municipio sin cobertura aprobada",
        province="Burgos",
        autonomous_community="Castilla y León",
    )
    db.add(municipality)
    db.flush()
    ordinance = Ordinance(
        municipality_id=municipality.id,
        title="Ordenanza pendiente de residuos",
        topic="residuos",
        ordinance_type="ordinance",
        source_url="https://bopbur.diputaciondeburgos.es/anuncio/pendiente.pdf",
        curation_status="pending_review",
        status="active",
    )
    db.add(ordinance)
    db.flush()
    embedding, model, status = embed_text("recogida de residuos")
    db.add(
        OrdinanceLegalChunk(
            ordinance_id=ordinance.id,
            chunk_index=0,
            citation="Artículo pendiente",
            text="La recogida de residuos está pendiente de revisión.",
            source_url=ordinance.source_url,
            review_status="pending_review",
            embedding=embedding,
            embedding_model=model,
            embedding_status=status,
        )
    )
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "semantic_search_ordinances",
        {"query": "recogida de residuos", "municipality_id": municipality.id},
    )

    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["results"] == []


def test_consultation_agent_exposes_ordinance_search_as_read_only_tool():
    consultation = AGENT_REGISTRY["consultation"]

    assert "semantic_search_ordinances" in consultation.tool_names
    assert assistant_tools.TOOL_CATALOG["semantic_search_ordinances"].read_only
    assert (
        assistant_tools.TOOL_CATALOG["semantic_search_ordinances"].domain
        == "ordinances"
    )


def test_web_search_tool_rejects_empty_query(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.web.search"])

    result = assistant_tools.execute_tool(
        db,
        user,
        "web_search",
        {"query": "   "},
    )

    assert result.ok is False
    assert "query no puede estar vacío" in result.content


def test_web_search_tool_rejects_personal_data_query(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.web.search"])

    result = assistant_tools.execute_tool(
        db,
        user,
        "web_search",
        {"query": "buscar expediente de vecino@example.com"},
    )

    assert result.ok is False
    assert "datos personales" in result.content


def test_agent_can_only_propose_memory_until_human_approval(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Secretario Test")
    organization = make_organization(name="Ayuntamiento Memoria")
    grant_permissions(
        user,
        organization,
        [
            "assistant.use",
            "assistant.memory.propose",
            "assistant.memory.review",
            "assistant.memory.view",
        ],
    )
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "propose_memory_entry",
                            {
                                "organization_id": organization.id,
                                "category": "protocol",
                                "content": (
                                    "En empadronamiento incompleto se pide primero "
                                    "el justificante de domicilio."
                                ),
                            },
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("Lo dejo propuesto.")]),
                fake_response("end_turn", [text_block("Sigo sin usarlo.")]),
                fake_response("end_turn", [text_block("Ahora puedo tenerlo en cuenta.")]),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    first = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Ese trámite lo hacemos siempre así"},
        headers=headers_for(user),
    )
    assert first.status_code == 200

    entry = db.scalar(select(AssistantMemoryEntry))
    assert entry is not None
    assert entry.status == "proposed"
    assert entry.source_conversation_id == conversation["id"]
    assert entry.source_message_id is not None

    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Qué recuerdas del empadronamiento?"},
        headers=headers_for(user),
    )
    assert second.status_code == 200
    assert "justificante de domicilio" not in gateway.calls[-1]["system"]

    approved = client.patch(
        f"/assistant/memory/{entry.id}",
        json={"status": "approved"},
        headers=headers_for(user),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    third = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Y ahora?"},
        headers=headers_for(user),
    )
    assert third.status_code == 200
    assert "justificante de domicilio" in gateway.calls[-1]["system"]


def test_memory_review_requires_review_permission(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "assistant.memory.view"])
    entry = AssistantMemoryEntry(
        organization_id=organization.id,
        category="context",
        content="Dato pendiente",
        status="proposed",
        proposed_by_id=user.id,
    )
    db.add(entry)
    db.commit()

    response = client.patch(
        f"/assistant/memory/{entry.id}",
        json={"status": "approved"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.memory.review"


def test_approved_memory_is_scoped_by_organization_permissions(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    visible_org = make_organization(name="Org Visible")
    hidden_org = make_organization(name="Org Oculta")
    grant_permissions(user, visible_org, ["assistant.use", "assistant.memory.view"])
    db.add_all(
        [
            AssistantMemoryEntry(
                organization_id=visible_org.id,
                category="context",
                content="Contexto visible",
                status="approved",
                proposed_by_id=user.id,
            ),
            AssistantMemoryEntry(
                organization_id=hidden_org.id,
                category="context",
                content="Contexto oculto",
                status="approved",
                proposed_by_id=user.id,
            ),
        ]
    )
    db.commit()
    gateway = use_gateway(FakeGateway([fake_response("end_turn", [text_block("Hola")])]))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert "Contexto visible" in gateway.calls[0]["system"]
    assert "Contexto oculto" not in gateway.calls[0]["system"]


def test_agent_can_propose_transversal_feature_from_visible_requirement(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user(full_name="Alcaldesa Test")
    organization = make_organization(name="Ayuntamiento Origen")
    grant_permissions(user, organization, ["assistant.use", "requirements.create"])
    requirement = Requirement(
        organization_id=organization.id,
        title="Avisos de vencimiento",
        summary="Avisar antes de que venza documentación de expedientes.",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "propose_transversal_feature",
                            {
                                "source_requirement_id": requirement.id,
                                "title": "Avisos de vencimiento documental",
                                "summary": (
                                    "Alertas configurables antes de que venza "
                                    "documentación asociada a expedientes."
                                ),
                                "rationale": (
                                    "Es un patrón común en trámites municipales "
                                    "con plazos y documentación recurrente."
                                ),
                                "category": "automation",
                            },
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("Lo dejo propuesto.")]),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Esto podría servir a otros ayuntamientos."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    feature = db.scalar(select(AssistantTransversalFeature))
    assert feature is not None
    assert feature.status == "proposed"
    assert feature.source_requirement_id == requirement.id
    assert feature.source_organization_id == organization.id
    assert feature.source_conversation_id == conversation["id"]
    assert feature.source_message_id is not None
    action = response.json()["messages"][-1]["actions"][0]
    assert action["tool"] == "propose_transversal_feature"
    assert action["ok"] is True
    assert gateway.calls[0]["tools"]


def test_transversal_feature_proposal_rejects_personal_data(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    organization = make_organization()
    grant_permissions(user, organization, ["assistant.use", "requirements.create"])
    requirement = Requirement(
        organization_id=organization.id,
        title="Trámite sensible",
        status="draft",
        source_type="conversation",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "propose_transversal_feature",
        {
            "source_requirement_id": requirement.id,
            "title": "Avisos a vecino@example.com",
            "summary": "Enviar avisos a vecino@example.com",
            "rationale": "Podría ahorrar llamadas.",
            "category": "automation",
        },
    )

    assert result.ok is False
    assert "datos personales" in result.content
    assert db.scalar(select(AssistantTransversalFeature)) is None


def test_transversal_feature_review_is_superuser_only(
    client,
    db,
    make_user,
    make_organization,
    superuser,
):
    user = make_user()
    organization = make_organization()
    feature = AssistantTransversalFeature(
        source_organization_id=organization.id,
        title="Bandeja de avisos",
        summary="Avisos reutilizables para trámites con plazos.",
        rationale="Los plazos administrativos se repiten en varios municipios.",
        category="process",
        status="proposed",
        proposed_by_id=user.id,
    )
    db.add(feature)
    db.commit()

    denied = client.get(
        "/assistant/transversal-features",
        headers=headers_for(user),
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Superuser privileges required"

    listed = client.get(
        "/assistant/transversal-features",
        headers=headers_for(superuser),
    )
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == feature.id

    approved = client.patch(
        f"/assistant/transversal-features/{feature.id}",
        json={"status": "available", "auto_activatable": True},
        headers=headers_for(superuser),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "available"
    assert approved.json()["auto_activatable"] is True
    assert approved.json()["reviewed_by_id"] == superuser.id


def test_available_transversal_features_tool_hides_source_data(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    available = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Avisos de vencimiento",
        summary="Alertas reutilizables para documentación con plazo.",
        rationale="Patrón común en expedientes municipales.",
        category="automation",
        status="available",
    )
    proposed = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Funcionalidad no revisada",
        summary="No debe sugerirse todavía.",
        rationale="Aún no está revisada.",
        category="other",
        status="proposed",
    )
    db.add_all([available, proposed])
    db.commit()

    result = assistant_tools.execute_tool(
        db,
        user,
        "list_available_transversal_features",
        {"organization_id": target_org.id},
    )

    assert result.ok is True
    data = json.loads(result.content)
    assert [feature["id"] for feature in data] == [available.id]
    assert "source_organization_id" not in data[0]
    assert "source_requirement_id" not in data[0]


def test_record_transversal_feature_acceptance_creates_activation_state(
    db,
    make_user,
    make_organization,
    grant_permissions,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    pending_feature = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Plantilla configurable",
        summary="Plantilla que requiere configuración humana.",
        rationale="Cada ayuntamiento debe ajustar algunos parámetros.",
        category="documents",
        status="available",
        auto_activatable=False,
    )
    automatic_feature = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Aviso automático",
        summary="Aviso que puede activarse sin configuración adicional.",
        rationale="No depende de datos locales.",
        category="automation",
        status="available",
        auto_activatable=True,
    )
    db.add_all([pending_feature, automatic_feature])
    db.commit()
    conversation = AssistantConversation(
        title="Activación transversal",
        created_by_id=user.id,
    )
    db.add(conversation)
    db.flush()
    pending_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Sí, queremos aplicar la plantilla.",
    )
    automatic_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content="Activad también el aviso.",
    )
    db.add_all([pending_message, automatic_message])
    db.commit()

    pending = assistant_tools.execute_tool(
        db,
        user,
        "record_transversal_feature_acceptance",
        {
            "feature_id": pending_feature.id,
            "organization_id": target_org.id,
            "notes": "OK confirmado en conversación.",
        },
        assistant_tools.ToolContext(
            conversation_id=conversation.id,
            user_message_id=pending_message.id,
        ),
    )
    automatic = assistant_tools.execute_tool(
        db,
        user,
        "record_transversal_feature_acceptance",
        {
            "feature_id": automatic_feature.id,
            "organization_id": target_org.id,
        },
        assistant_tools.ToolContext(
            conversation_id=conversation.id,
            user_message_id=automatic_message.id,
        ),
    )

    assert pending.ok is True
    assert automatic.ok is True
    adoptions = list(
        db.scalars(
            select(AssistantTransversalFeatureAdoption).order_by(
                AssistantTransversalFeatureAdoption.feature_id
            )
        )
    )
    adoption_by_feature = {adoption.feature_id: adoption for adoption in adoptions}
    assert adoption_by_feature[pending_feature.id].status == "activation_pending"
    assert adoption_by_feature[pending_feature.id].activated_at is None
    assert adoption_by_feature[pending_feature.id].source_message_id == pending_message.id
    assert adoption_by_feature[automatic_feature.id].status == "active"
    assert adoption_by_feature[automatic_feature.id].activated_at is not None
    assert adoption_by_feature[automatic_feature.id].approved_by_id == user.id


def test_agent_records_transversal_feature_acceptance_as_audited_action(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    feature = AssistantTransversalFeature(
        source_organization_id=source_org.id,
        title="Aviso automático",
        summary="Aviso que puede activarse sin configuración adicional.",
        rationale="No depende de datos locales.",
        category="automation",
        status="available",
        auto_activatable=True,
    )
    db.add(feature)
    db.commit()
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "toolu_1",
                            "record_transversal_feature_acceptance",
                            {
                                "feature_id": feature.id,
                                "organization_id": target_org.id,
                            },
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("Queda activada.")]),
            ]
        )
    )

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, activadlo para nuestro ayuntamiento."},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    adoption = db.scalar(select(AssistantTransversalFeatureAdoption))
    assert adoption is not None
    assert adoption.status == "active"
    assert adoption.source_conversation_id == conversation["id"]
    assert adoption.source_message_id is not None
    action = response.json()["messages"][-1]["actions"][0]
    assert action["tool"] == "record_transversal_feature_acceptance"
    assert action["ok"] is True


def test_available_transversal_features_are_not_injected_in_system_prompt(
    client,
    db,
    make_user,
    make_organization,
    grant_permissions,
    use_gateway,
):
    user = make_user()
    source_org = make_organization(name="Org Origen")
    target_org = make_organization(name="Org Destino")
    grant_permissions(user, target_org, ["assistant.use"])
    db.add(
        AssistantTransversalFeature(
            source_organization_id=source_org.id,
            title="No debe aparecer en el prompt",
            summary="Solo debe consultarse mediante herramienta.",
            rationale="Evita inyectar backlog global en todos los chats.",
            category="other",
            status="available",
        )
    )
    db.commit()
    gateway = use_gateway(FakeGateway([fake_response("end_turn", [text_block("Hola")])]))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert "No debe aparecer en el prompt" not in gateway.calls[0]["system"]


def test_archived_conversation_rejects_messages(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([fake_response("end_turn", [text_block("Hola")])]))

    conversation = client.post(
        "/assistant/conversations",
        json={"title": "Vieja"},
        headers=headers_for(user),
    ).json()
    archived = client.patch(
        f"/assistant/conversations/{conversation['id']}",
        json={"status": "archived"},
        headers=headers_for(user),
    )
    assert archived.status_code == 200

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Conversation is archived"


def test_refusal_stop_reason_returns_polite_message(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([fake_response("refusal", [])]))

    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][1]
    assert "No puedo ayudarte" in assistant_message["content"]
