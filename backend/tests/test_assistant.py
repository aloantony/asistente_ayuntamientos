import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant import guards as assistant_guards
from app.assistant import tools as assistant_tools
from app.assistant.gateway import (
    AIGateway,
    AITextDelta,
    AssistantUnavailableError,
    _from_openai_response,
    _hermes_agent_url,
    _openai_compatible_url,
)
from app.assistant.models import (
    AssistantConversation,
    AssistantMemoryEntry,
    AssistantMessage,
)
from app.assistant.speech import SpeechTranscriptionError, build_azure_ssml
from app.assistant.routes import get_gateway
from app.assistant.turn import ERROR_REPLY, build_history
from app.core.config import settings
from app.main import app
from app.requirements.models import Requirement
from app.users.models import User
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


def test_status_reports_speech_flags(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    monkeypatch.setattr(settings, "speech_transcription_runtime", "nvidia_nim")
    monkeypatch.setattr(settings, "speech_synthesis_runtime", "azure")

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["speech_transcription_enabled"] is True
    assert body["speech_synthesis_enabled"] is True


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


def test_speech_synthesis_returns_audio(client, assistant_user, monkeypatch):
    user, _ = assistant_user

    monkeypatch.setattr(
        "app.assistant.routes.synthesize_speech_bytes",
        lambda text: b"mp3-bytes",
    )

    response = client.post(
        "/assistant/speech",
        json={"text": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"mp3-bytes"


def test_speech_synthesis_requires_assistant_use(client, make_user, monkeypatch):
    user = make_user()

    monkeypatch.setattr(
        "app.assistant.routes.synthesize_speech_bytes",
        lambda text: b"mp3-bytes",
    )

    response = client.post(
        "/assistant/speech",
        json={"text": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_speech_synthesis_rejects_long_text(
    client,
    assistant_user,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "speech_synthesis_max_chars", 5)

    response = client.post(
        "/assistant/speech",
        json={"text": "demasiado largo"},
        headers=headers_for(user),
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Speech text is too long"


def test_speech_synthesis_unavailable_when_disabled(
    client,
    assistant_user,
    monkeypatch,
):
    user, _ = assistant_user
    monkeypatch.setattr(settings, "speech_synthesis_runtime", "disabled")

    response = client.post(
        "/assistant/speech",
        json={"text": "Hola"},
        headers=headers_for(user),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Speech synthesis is not available"


def test_azure_ssml_escapes_markup():
    ssml = build_azure_ssml("<hola & adiós>", "es-ES-ElviraNeural", "es-ES")

    assert "&lt;hola &amp; adiós&gt;" in ssml
    assert "xml:lang='es-ES'" in ssml
    assert "name='es-ES-ElviraNeural'" in ssml


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


def test_voice_input_mode_adds_oral_style_prompt(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    gateway = use_gateway(
        FakeGateway([fake_response("end_turn", [text_block("Te contesto breve.")])])
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Hola", "input_mode": "voice"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert "escuchará tu respuesta en voz alta" in gateway.calls[0]["system"]


def test_text_input_mode_keeps_prompt_clean(
    client,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    gateway = use_gateway(
        FakeGateway([fake_response("end_turn", [text_block("Te contesto.")])])
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
    assert "escuchará tu respuesta en voz alta" not in gateway.calls[0]["system"]


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


def get_pending_confirmation(db, conversation_id: int) -> dict:
    db.expire_all()
    conversation = db.get(AssistantConversation, conversation_id)
    assert conversation is not None
    state = json.loads(conversation.state or "{}")
    return state["pending_confirmation"]


def test_create_requirement_requires_matching_explicit_confirmation(
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
    pending = get_pending_confirmation(db, conversation["id"])
    assert pending["tool"] == "create_requirement"
    assert pending["confirmation_id"]

    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
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


def test_create_requirement_cancellation_does_not_authorize_tool(
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
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Confirma el borrador.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("De acuerdo, no lo creo.")]),
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
        json={"content": "No, cancela la creación"},
        headers=headers_for(user),
    )

    assert second.status_code == 200
    action = second.json()["messages"][-1]["actions"][0]
    assert action["ok"] is False
    assert "cancel" in action["result"].lower()
    assert db.scalar(select(Requirement)) is None


def test_create_requirement_ambiguous_response_does_not_authorize_tool(
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
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Confirma el borrador.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Necesito confirmación explícita.")],
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
        json={"content": "Propón una necesidad"},
        headers=headers_for(user),
    )
    original = get_pending_confirmation(db, conversation["id"])
    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Qué datos se van a guardar?"},
        headers=headers_for(user),
    )

    assert second.status_code == 200
    action = second.json()["messages"][-1]["actions"][0]
    assert action["ok"] is False
    assert "confirmación" in action["result"].lower()
    assert db.scalar(select(Requirement)) is None
    pending = get_pending_confirmation(db, conversation["id"])
    assert pending["confirmation_id"] == original["confirmation_id"]


def test_create_requirement_any_payload_change_restarts_confirmation(
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
        "title": "Portal ciudadano",
        "problem": "El alta de solicitudes ahora se hace por teléfono.",
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
    pending = get_pending_confirmation(db, conversation["id"])
    second = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
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
    assert pending["input"]["title"] == "Portal ciudadano"
    assert pending["input"]["problem"] == changed_input["problem"]
    assert pending["confirmation_id"]


def test_create_requirement_confirmation_cannot_be_replayed(
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
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Confirma el borrador.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Borrador creado.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_3", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Necesito otra confirmación.")]),
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
    pending = get_pending_confirmation(db, conversation["id"])
    confirmation_text = "Sí, créalo"

    confirmed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": confirmation_text},
        headers=headers_for(user),
    )
    replayed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": confirmation_text},
        headers=headers_for(user),
    )

    assert confirmed.json()["messages"][-1]["actions"][0]["ok"] is True
    replay_action = replayed.json()["messages"][-1]["actions"][0]
    assert replay_action["ok"] is False
    assert "confirmación" in replay_action["result"].lower()
    assert len(list(db.scalars(select(Requirement)))) == 1
    replacement = get_pending_confirmation(db, conversation["id"])
    assert replacement["confirmation_id"] != pending["confirmation_id"]


def test_create_requirement_confirmation_stays_consumed_after_tool_rollback(
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
        "priority": "imposible",
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Confirma el borrador.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("No se pudo crear.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_3", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Confirma de nuevo.")]),
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
    pending = get_pending_confirmation(db, conversation["id"])

    failed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert failed.status_code == 200
    failed_action = failed.json()["messages"][-1]["actions"][0]
    assert failed_action["ok"] is False
    assert "priority inválida" in failed_action["result"]
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_confirmation" not in state
    assert (
        state["last_consumed_confirmation"]["confirmation_id"]
        == pending["confirmation_id"]
    )

    replayed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    replay_action = replayed.json()["messages"][-1]["actions"][0]
    assert replay_action["ok"] is False
    assert "confirmación" in replay_action["result"].lower()
    replacement = get_pending_confirmation(db, conversation["id"])
    assert replacement["confirmation_id"] != pending["confirmation_id"]


def test_create_requirement_confirmation_has_single_concurrent_consumer(engine):
    suffix = uuid.uuid4().hex
    tool_input = {
        "organization_id": 1,
        "title": "Portal ciudadano",
        "problem": "El alta de solicitudes se hace por correo.",
    }
    with Session(engine, expire_on_commit=False) as seed_db:
        user = User(
            email=f"confirmation-{suffix}@example.com",
            hashed_password="not-used",
            full_name="Confirmation Concurrency Test",
        )
        conversation = AssistantConversation(
            title="Confirmación concurrente",
            status="active",
            channel="web",
            created_by=user,
        )
        proposed_message = AssistantMessage(
            conversation=conversation,
            role="user",
            content="Propón una necesidad",
        )
        confirmation_message = AssistantMessage(
            conversation=conversation,
            role="user",
            content="Sí, créalo",
        )
        seed_db.add_all([user, conversation, proposed_message, confirmation_message])
        seed_db.flush()
        assistant_guards.record_pending_confirmation(
            conversation,
            proposed_message,
            "create_requirement",
            tool_input,
        )
        seed_db.flush()
        assistant_guards.process_pending_confirmation_response(
            seed_db,
            conversation,
            confirmation_message,
        )
        pending = assistant_guards.load_conversation_state(conversation)[
            "pending_confirmation"
        ]
        conversation_id = conversation.id
        confirmation_message_id = confirmation_message.id
        user_id = user.id
        seed_db.commit()

    barrier = threading.Barrier(2)

    def attempt_consumption() -> bool:
        with Session(engine, expire_on_commit=False) as candidate_db:
            candidate_conversation = candidate_db.get(
                AssistantConversation,
                conversation_id,
            )
            candidate_message = candidate_db.get(
                AssistantMessage,
                confirmation_message_id,
            )
            assert candidate_conversation is not None
            assert candidate_message is not None
            barrier.wait(timeout=5)
            result = assistant_guards.check_tool_confirmation(
                candidate_db,
                candidate_conversation,
                candidate_message,
                "create_requirement",
                tool_input,
            )
            return result is None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt_consumption(), range(2)))

        assert results.count(True) == 1
        assert results.count(False) == 1
        with Session(engine) as verification_db:
            stored_conversation = verification_db.get(
                AssistantConversation,
                conversation_id,
            )
            assert stored_conversation is not None
            state = assistant_guards.load_conversation_state(stored_conversation)
            assert (
                state["last_consumed_confirmation"]["confirmation_id"]
                == pending["confirmation_id"]
            )
    finally:
        with Session(engine) as cleanup_db:
            stored_user = cleanup_db.get(User, user_id)
            if stored_user is not None:
                cleanup_db.delete(stored_user)
                cleanup_db.commit()


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


def test_assistant_pending_ordinance_search_requires_review_permission(
    db,
    assistant_user,
    grant_permissions,
):
    user, organization = assistant_user
    grant_permissions(user, organization, ["ordinances.compare"])

    result = assistant_tools.execute_tool(
        db,
        user,
        "semantic_search_ordinances",
        {"query": "residuos", "include_pending": True},
    )

    assert result.ok is False
    assert "Permission required: ordinances.review" in result.content


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


def test_self_hosted_gateway_uses_configured_openai_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "model": "municipal-model-v1",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Hola"},
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }
            ).encode("utf-8")

    class FakeOpener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "assistant_runtime", "self_hosted")
    monkeypatch.setattr(
        settings,
        "self_hosted_ai_base_url",
        "http://127.0.0.1:8655/v1",
    )
    monkeypatch.setattr(settings, "self_hosted_ai_api_key", "private-key")
    monkeypatch.setattr(settings, "self_hosted_ai_model", "municipal-model-v1")
    monkeypatch.setattr(settings, "self_hosted_ai_timeout_seconds", 17.0)
    monkeypatch.setattr(
        "app.assistant.gateway._OPENAI_COMPATIBLE_OPENER",
        FakeOpener(),
    )

    completion = AIGateway().complete(
        system="Solo datos autorizados.",
        messages=[{"role": "user", "content": "Hola"}],
        tools=[
            {
                "name": "list_requirements",
                "description": "Lista requisitos",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )

    assert captured == {
        "url": "http://127.0.0.1:8655/v1/chat/completions",
        "authorization": "Bearer private-key",
        "payload": {
            "model": "municipal-model-v1",
            "messages": [
                {"role": "system", "content": "Solo datos autorizados."},
                {"role": "user", "content": "Hola"},
            ],
            "max_tokens": settings.assistant_max_tokens,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "list_requirements",
                        "description": "Lista requisitos",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
        },
        "timeout": 17.0,
    }
    assert completion.model == "municipal-model-v1"
    assert completion.content[0].text == "Hola"


def test_self_hosted_gateway_streams_from_same_runtime(monkeypatch):
    class FakeStreamingResponse:
        status = 200

        def __init__(self):
            self.lines = iter(
                [
                    b'data: {"model":"own-model","choices":[{"delta":{"content":"Hola"},"finish_reason":null}]}\n',
                    b'data: {"choices":[{"delta":{"content":" mundo"},"finish_reason":"stop"}]}\n',
                    b"data: [DONE]\n",
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self):
            return next(self.lines, b"")

    class FakeOpener:
        def open(self, request, timeout):
            assert json.loads(request.data.decode("utf-8"))["stream"] is True
            assert timeout == 9.0
            return FakeStreamingResponse()

    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "assistant_runtime", "self_hosted")
    monkeypatch.setattr(settings, "self_hosted_ai_base_url", "http://runtime/v1")
    monkeypatch.setattr(settings, "self_hosted_ai_api_key", None)
    monkeypatch.setattr(settings, "self_hosted_ai_model", "own-model")
    monkeypatch.setattr(settings, "self_hosted_ai_timeout_seconds", 9.0)
    monkeypatch.setattr(
        "app.assistant.gateway._OPENAI_COMPATIBLE_OPENER",
        FakeOpener(),
    )

    stream = AIGateway().complete_stream(
        system="Sistema",
        messages=[{"role": "user", "content": "Hola"}],
        tools=[],
    )
    deltas = []
    while True:
        try:
            deltas.append(next(stream).text)
        except StopIteration as stopped:
            completion = stopped.value
            break

    assert deltas == ["Hola", " mundo"]
    assert completion.model == "own-model"
    assert completion.content[0].text == "Hola mundo"


def test_openai_compatible_url_normalizes_version_and_health():
    assert _openai_compatible_url("https://runtime.internal/v1", "models") == (
        "https://runtime.internal/v1/models"
    )
    assert _openai_compatible_url("https://runtime.internal/v1", "health") == (
        "https://runtime.internal/health"
    )


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
