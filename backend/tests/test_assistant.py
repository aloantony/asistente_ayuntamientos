import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.assistant import tools as assistant_tools
from app.assistant.gateway import (
    AITextDelta,
    AssistantUnavailableError,
    _from_openai_response,
    _hermes_agent_url,
)
from app.assistant.models import (
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessage,
)
from app.assistant.speech import SpeechTranscriptionError
from app.assistant.routes import get_gateway
from app.assistant.turn import ERROR_REPLY, build_history
from app.core.config import settings
from app.main import app
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


def fake_response(stop_reason: str, content: list, deltas: list[str] | None = None):
    return SimpleNamespace(
        model="fake-model",
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        deltas=deltas or [],
    )


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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def complete_stream(self, *, system, messages, tools):
        response = self.complete(system=system, messages=messages, tools=tools)
        for delta in response.deltas:
            yield AITextDelta(text=delta)
        return response


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
        [
            "assistant.use",
            "requirements.create",
            "requirements.view",
            "assistant.memory.view",
        ],
    )
    return user, organization


def test_assistant_requires_permission(client, make_user):
    user = make_user()

    response = client.get("/assistant/conversations", headers=headers_for(user))

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_status_exposes_single_assistant_contract_and_filtered_tools(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([], runtime_healthy=True))

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["runtime_healthy"] is True
    assert "planner" not in body
    assert "agents" not in body
    tool_names = {tool["name"] for tool in body["tools"]}
    assert "create_requirement" in tool_names
    assert "list_requirements" in tool_names
    assert "web_search" not in tool_names


def test_transcribe_audio_returns_text(client, assistant_user, monkeypatch):
    user, _ = assistant_user

    monkeypatch.setattr(
        "app.assistant.routes.transcribe_audio_bytes",
        lambda audio, *, language_code=None: "hola",
    )

    response = client.post(
        "/assistant/audio-transcriptions",
        files={"file": ("audio.webm", b"audio", "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json() == {"text": "hola"}


def test_transcribe_audio_requires_assistant_use(client, make_user, monkeypatch):
    user = make_user()

    monkeypatch.setattr(
        "app.assistant.routes.transcribe_audio_bytes",
        lambda audio, *, language_code=None: "hola",
    )

    response = client.post(
        "/assistant/audio-transcriptions",
        files={"file": ("audio.webm", b"audio", "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_transcribe_audio_rejects_large_file(
    client,
    assistant_user,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "speech_transcription_max_bytes", 10)

    response = client.post(
        "/assistant/audio-transcriptions",
        files={"file": ("audio.webm", b"x" * 11, "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Audio file is too large"


def test_transcribe_audio_unavailable_when_disabled(
    client,
    assistant_user,
    monkeypatch,
):
    user, _ = assistant_user

    def raise_unavailable(audio: bytes, *, language_code=None) -> str:
        raise SpeechTranscriptionError("Speech transcription is disabled")

    monkeypatch.setattr(
        "app.assistant.routes.transcribe_audio_bytes",
        raise_unavailable,
    )

    response = client.post(
        "/assistant/audio-transcriptions",
        files={"file": ("audio.webm", b"audio", "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Audio transcription is not available"


def test_model_first_turn_persists_reply_and_calls_gateway_for_capabilities(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, _ = assistant_user
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Puedo ayudarte a preparar borradores y consultas.")],
                    deltas=["Puedo ayudarte", " a preparar borradores y consultas."],
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
        json={"content": "¿qué puedes hacer?"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert messages[-2]["role"] == "user"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["agent_key"] == "anacleto"
    assert messages[-1]["routing"] is None
    assert messages[-1]["content"] == "Puedo ayudarte a preparar borradores y consultas."
    assert len(gateway.calls) == 1
    assert "Eres Anacleto" in gateway.calls[0]["system"]
    assert "HERRAMIENTAS DISPONIBLES" in gateway.calls[0]["system"]
    assert "COBERTURA DE ORDENANZAS" in gateway.calls[0]["system"]
    assert gateway.calls[0]["messages"][-1] == {
        "role": "user",
        "content": "¿qué puedes hacer?",
    }
    stored = db.get(AssistantConversation, conversation["id"])
    assert stored is not None
    assert stored.title == "¿qué puedes hacer?"


def test_tool_loop_executes_available_tool_and_persists_action(
    client,
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
                            "call_1",
                            "list_requirements",
                            {"organization_id": organization.id},
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No hay necesidades registradas.")],
                    deltas=["No hay necesidades registradas."],
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
        json={"content": "Lista las necesidades"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["content"] == "No hay necesidades registradas."
    assert assistant_message["actions"][0]["tool"] == "list_requirements"
    assert assistant_message["actions"][0]["ok"] is True
    assert len(gateway.calls) == 2
    assert gateway.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_create_requirement_is_blocked_until_a_later_user_confirmation(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Portal ciudadano",
        "problem": "El alta de solicitudes se hace por correo.",
        "summary": "Crear un portal de solicitudes.",
        "acceptance_criteria": "Permite registrar solicitudes y revisarlas.",
    }
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", tool_input)],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Te propongo este borrador. ¿Lo confirmas?")],
                ),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Listo, queda como borrador.")],
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
        json={"content": "Crea esta necesidad directamente"},
        headers=headers_for(user),
    )

    assert first.status_code == 200
    first_action = first.json()["messages"][-1]["actions"][0]
    assert first_action["tool"] == "create_requirement"
    assert first_action["ok"] is False
    assert "confirmación" in first_action["result"].lower()
    assert db.scalar(select(Requirement)) is None
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert state["pending_confirmation"]["tool"] == "create_requirement"

    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí, créalo"},
        headers=headers_for(user),
    )

    assert second.status_code == 200
    second_action = second.json()["messages"][-1]["actions"][0]
    assert second_action["tool"] == "create_requirement"
    assert second_action["ok"] is True
    requirement = db.scalar(select(Requirement))
    assert requirement is not None
    assert requirement.title == "Portal ciudadano"
    assert requirement.status == "draft"
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    assert "pending_confirmation" not in json.loads(stored_conversation.state or "{}")
    assert len(gateway.calls) == 4


def test_create_requirement_material_change_restarts_confirmation(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    first_input = {
        "organization_id": organization.id,
        "title": "Portal ciudadano",
        "problem": "El alta de solicitudes se hace por correo.",
    }
    changed_input = {
        "organization_id": organization.id,
        "title": "Portal tributario",
        "problem": "El alta de solicitudes se hace por correo.",
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", first_input)],
                ),
                fake_response("end_turn", [text_block("Confirma el borrador.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", changed_input)],
                ),
                fake_response("end_turn", [text_block("Confirma este otro borrador.")]),
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
        json={"content": "Propón una necesidad"},
        headers=headers_for(user),
    )
    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "sí"},
        headers=headers_for(user),
    )

    assert second.status_code == 200
    action = second.json()["messages"][-1]["actions"][0]
    assert action["ok"] is False
    assert db.scalar(select(Requirement)) is None
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    pending = json.loads(stored_conversation.state or "{}")["pending_confirmation"]
    assert pending["input"]["title"] == "Portal tributario"


def test_system_prompt_includes_approved_memory(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    memory = AssistantMemoryEntry(
        organization_id=organization.id,
        category="preference",
        content="Prefiere respuestas con resumen ejecutivo.",
        status="approved",
        sensitivity="normal",
    )
    db.add(memory)
    db.commit()
    gateway = use_gateway(
        FakeGateway([fake_response("end_turn", [text_block("De acuerdo.")])])
    )
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
    assert "NOTAS INTERNAS APROBADAS" in gateway.calls[0]["system"]
    assert "Prefiere respuestas con resumen ejecutivo." in gateway.calls[0]["system"]


def test_history_is_capped(monkeypatch, db, assistant_user):
    user, _ = assistant_user
    conversation = AssistantConversation(title="Conversación", created_by_id=user.id)
    db.add(conversation)
    db.flush()
    for index in range(10):
        db.add(
            AssistantMessage(
                conversation_id=conversation.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"mensaje {index}",
            )
        )
    db.commit()
    db.refresh(conversation)
    monkeypatch.setattr(settings, "assistant_history_max_messages", 4)

    history = build_history(conversation)

    assert [message["content"] for message in history] == [
        "mensaje 6",
        "mensaje 7",
        "mensaje 8",
        "mensaje 9",
    ]


def test_sse_stream_emits_deltas_tool_activity_and_done(
    client,
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
                            "call_1",
                            "list_requirements",
                            {"organization_id": organization.id},
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Respuesta final.")],
                    deltas=["Respuesta ", "final."],
                ),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    with client.stream(
        "POST",
        f"/assistant/conversations/{conversation['id']}/messages/stream",
        json={"content": "Lista necesidades"},
        headers=headers_for(user),
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = parse_sse(body)
    assert [event["event"] for event in events] == [
        "message_start",
        "tool_activity",
        "tool_activity",
        "text_delta",
        "text_delta",
        "done",
    ]
    assert events[1]["data"]["status"] == "started"
    assert events[2]["data"]["status"] == "finished"
    assert events[2]["data"]["ok"] is True
    assert events[3]["data"]["text"] == "Respuesta "
    assert events[-1]["data"]["message"]["content"] == "Respuesta final."
    assert events[-1]["data"]["message"]["agent_key"] == "anacleto"
    assert len(gateway.calls) == 2


def test_sse_precondition_errors_are_http(client, assistant_user, use_gateway):
    user, _ = assistant_user
    use_gateway(FakeGateway([], enabled=False))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages/stream",
        json={"content": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Assistant is not configured"


def test_gateway_failure_mid_turn_persists_error_reply(client, assistant_user, use_gateway):
    user, _ = assistant_user
    use_gateway(FakeGateway([AssistantUnavailableError("boom")]))
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
    assert response.json()["messages"][-1]["content"] == ERROR_REPLY


def test_textual_read_tool_call_recovery_executes_matching_read_tool(
    client,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block(json.dumps({"organization_id": organization.id}))],
                ),
                fake_response("end_turn", [text_block("He consultado la lista.")]),
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
        json={"content": "Lista los requisitos visibles"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["actions"][0]["tool"] == "list_requirements"
    assert assistant_message["content"] == "He consultado la lista."


def test_execute_tool_still_rejects_tools_outside_allowed_set(db, assistant_user):
    user, _ = assistant_user

    result = assistant_tools.execute_tool(
        db,
        user,
        "list_requirements",
        {},
        allowed=frozenset({"list_projects"}),
    )

    assert result.ok is False
    assert "Herramienta no disponible" in result.content


def test_openai_response_parses_inline_tool_call():
    response = _from_openai_response(
        {
            "model": "hermes-agent",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '<tool_call>{"name":"list_requirements","arguments":{"organization_id":1}}</tool_call>',
                    },
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    )

    assert response.stop_reason == "tool_use"
    assert response.content[0].type == "tool_use"
    assert response.content[0].name == "list_requirements"
    assert response.content[0].input == {"organization_id": 1}


def test_hermes_agent_url_normalizes_v1(monkeypatch):
    monkeypatch.setattr(settings, "hermes_agent_base_url", "http://127.0.0.1:8642/v1")

    assert _hermes_agent_url("chat/completions") == (
        "http://127.0.0.1:8642/v1/chat/completions"
    )
    assert _hermes_agent_url("health") == "http://127.0.0.1:8642/health"


def parse_sse(body: str) -> list[dict]:
    events = []
    for frame in body.strip().split("\n\n"):
        event = "message"
        data_lines = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        events.append(
            {
                "event": event,
                "data": json.loads("\n".join(data_lines)) if data_lines else {},
            }
        )
    return events
