import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant import guards as assistant_guards
from app.assistant import realtime as assistant_realtime
from app.assistant import tools as assistant_tools
from app.assistant import turn as assistant_turn
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
from app.assistant.speech import SpeechTranscriptionError, build_azure_ssml
from app.assistant.routes import get_gateway
from app.assistant.schemas import AssistantRealtimeTurnStartCreate
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


def parse_sse_events(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in payload.strip().split("\n\n"):
        if not frame.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        data = json.loads("\n".join(data_lines)) if data_lines else {}
        events.append((event_name, data))
    return events


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sí, créalo.", "confirmed"),
        ("Sí, créalo por favor", "confirmed"),
        ("Confirmo el borrador", "confirmed"),
        ("sí", "ambiguous"),
        ("vale", "ambiguous"),
        ("de acuerdo", "ambiguous"),
        ("No, cancela la creación", "cancelled"),
        ("No lo guardes", "cancelled"),
        ("Olvídalo", "cancelled"),
        ("Sí, créalo, no cambies nada", "ambiguous"),
        ("¿Qué datos se van a guardar?", "ambiguous"),
    ],
)
def test_confirmation_response_classification(text, expected):
    assert (
        assistant_guards.classify_confirmation_response(
            text,
            tool_name="create_requirement",
        )
        == expected
    )


@pytest.mark.parametrize(
    ("tool_name", "text"),
    [
        ("create_requirement", "Sí, envíalo"),
        ("send_admin_feedback", "Sí, créalo"),
        ("send_admin_feedback", "Confirmo el borrador"),
    ],
)
def test_action_specific_confirmation_does_not_authorize_another_tool(
    tool_name,
    text,
):
    assert (
        assistant_guards.classify_confirmation_response(
            text,
            tool_name=tool_name,
        )
        == "ambiguous"
    )


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


def test_web_search_is_hidden_when_permission_exists_but_runtime_is_incomplete(
    client,
    db,
    assistant_user,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    grant_permissions(user, organization, ["assistant.web.search"])
    use_gateway(FakeGateway([]))
    monkeypatch.setattr(settings, "hermes_web_base_url", "http://web.test/v1")
    monkeypatch.setattr(settings, "hermes_web_api_key", None)
    monkeypatch.setattr(settings, "hermes_web_model", "hermes-agent")

    specs = assistant_tools.get_available_tool_specs(db, user)
    response = client.get("/assistant/status", headers=headers_for(user))

    assert "web_search" not in {spec.name for spec in specs}
    assert response.status_code == 200
    assert "web_search" not in {
        tool["name"] for tool in response.json()["tools"]
    }


def test_web_search_is_available_with_permission_and_complete_runtime_config(
    client,
    db,
    assistant_user,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    grant_permissions(user, organization, ["assistant.web.search"])
    use_gateway(FakeGateway([]))
    monkeypatch.setattr(settings, "hermes_web_base_url", "http://web.test/v1")
    monkeypatch.setattr(settings, "hermes_web_api_key", "web-secret")
    monkeypatch.setattr(settings, "hermes_web_model", "hermes-agent")

    specs = assistant_tools.get_available_tool_specs(db, user)
    response = client.get("/assistant/status", headers=headers_for(user))

    assert "web_search" in {spec.name for spec in specs}
    assert response.status_code == 200
    assert "web_search" in {
        tool["name"] for tool in response.json()["tools"]
    }


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


def test_status_reports_realtime_voice_flags(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "assistant_realtime_enabled", True)
    monkeypatch.setattr(settings, "assistant_realtime_model", "gpt-realtime-2.1")

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["realtime_voice_enabled"] is True
    assert body["realtime_voice_provider"] == "openai"
    assert body["realtime_voice_model"] == "gpt-realtime-2.1"


def test_realtime_voice_requires_server_transcription(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "assistant_realtime_enabled", True)
    monkeypatch.setattr(settings, "assistant_realtime_transcription_model", "")

    response = client.get("/assistant/status", headers=headers_for(user))

    assert response.status_code == 200
    body = response.json()
    assert body["realtime_voice_enabled"] is False
    assert body["realtime_voice_provider"] is None
    assert body["realtime_voice_model"] is None


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


def test_voice_turn_stream_transcribes_runs_agent_and_persists_reply(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "end_turn",
                    [text_block("Claro, te ayudo con la consulta.")],
                    deltas=["Claro, ", "te ayudo con la consulta."],
                )
            ]
        )
    )
    monkeypatch.setattr(
        "app.assistant.voice.transcribe_audio_bytes",
        lambda audio, *, language_code=None: "Hola por voz",
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/voice-turns/stream",
        files={"file": ("audio.webm", b"audio", "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    events = parse_sse_events(response.text)
    event_names = [name for name, _ in events]
    assert event_names[:3] == [
        "voice_state",
        "transcript_final",
        "voice_state",
    ]
    assert events[0][1] == {"state": "transcribing"}
    assert events[1][1] == {"text": "Hola por voz"}
    assert events[2][1] == {"state": "thinking"}
    assert ("text_delta", {"text": "Claro, "}) in events
    assert ("text_delta", {"text": "te ayudo con la consulta."}) in events
    assert events[-2] == ("voice_state", {"state": "done"})
    assert events[-1][0] == "done"
    done = next(data for name, data in events if name == "done")
    assert done["message"]["content"] == "Claro, te ayudo con la consulta."
    assert done["message"]["role"] == "assistant"
    assert gateway.calls[0]["messages"][-1] == {
        "role": "user",
        "content": "Hola por voz",
    }
    assert "escuchará tu respuesta en voz alta" in gateway.calls[0]["system"]


def test_voice_turn_stream_rejects_large_audio(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    monkeypatch.setattr(settings, "speech_transcription_max_bytes", 10)
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/voice-turns/stream",
        files={"file": ("audio.webm", b"x" * 11, "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Audio file is too large"


def test_voice_turn_stream_reports_transcription_unavailable(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))

    def raise_unavailable(audio: bytes, *, language_code=None) -> str:
        raise SpeechTranscriptionError("Speech transcription is disabled")

    monkeypatch.setattr(
        "app.assistant.voice.transcribe_audio_bytes",
        raise_unavailable,
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/voice-turns/stream",
        files={"file": ("audio.webm", b"audio", "audio/webm")},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert parse_sse_events(response.text) == [
        ("voice_state", {"state": "transcribing"}),
        ("error", {"detail": "Audio transcription is not available"}),
    ]


def test_realtime_session_creates_openai_client_secret(
    client,
    db,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    captured = {}
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "assistant_realtime_enabled", True)
    monkeypatch.setattr(settings, "assistant_realtime_model", "gpt-realtime-2.1")
    monkeypatch.setattr(settings, "assistant_realtime_voice", "marin")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse({"value": "ek_test", "expires_at": 123})

    monkeypatch.setattr("app.assistant.realtime.urlrequest.urlopen", fake_urlopen)
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    active_turn_id = str(uuid.uuid4())
    active_turn = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=active_turn_id,
        user_text="Turno pendiente antes de reconectar",
    )
    assert active_turn.status_code == 200

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/realtime/session",
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert response.json() == {
        "client_secret": "ek_test",
        "client_secret_expires_at": 123,
        "provider": "openai",
        "model": "gpt-realtime-2.1",
        "voice": "marin",
        "realtime_url": settings.assistant_realtime_url,
    }
    assert captured["url"] == settings.assistant_realtime_client_secret_url
    session = captured["payload"]["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1"
    assert session["audio"]["output"]["voice"] == "marin"
    assert (
        session["audio"]["input"]["turn_detection"]["interrupt_response"]
        is True
    )
    assert session["audio"]["input"]["turn_detection"]["create_response"] is False
    assert "conversación hablada" in session["instructions"]
    tool_names = {tool["name"] for tool in session["tools"]}
    assert "list_requirements" in tool_names
    assert "create_requirement" in tool_names
    state = get_conversation_state(db, conversation["id"])
    assert state["realtime_voice"]["active_turn"] is None
    abandoned = next(
        turn
        for turn in state["realtime_voice"]["recent_turns"]
        if turn["turn_id"] == active_turn_id
    )
    assert abandoned["status"] == "abandoned"
    assert abandoned["assistant_message_id"]
    assert "Respuesta de voz interrumpida" in session["instructions"]


def test_realtime_session_hides_web_search_when_runtime_is_incomplete(
    client,
    db,
    assistant_user,
    grant_permissions,
    monkeypatch,
):
    user, organization = assistant_user
    grant_permissions(user, organization, ["assistant.web.search"])
    monkeypatch.setattr(settings, "hermes_web_base_url", "http://web.test/v1")
    monkeypatch.setattr(settings, "hermes_web_api_key", "")
    monkeypatch.setattr(settings, "hermes_web_model", "hermes-agent")
    conversation_data = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    conversation = db.get(AssistantConversation, conversation_data["id"])
    assert conversation is not None

    payload = assistant_realtime.build_realtime_client_secret_payload(
        db,
        user,
        conversation,
    )

    assert "web_search" not in {
        tool["name"] for tool in payload["session"]["tools"]
    }


def test_realtime_session_history_marks_finished_actions_as_already_processed(
    client,
    db,
    assistant_user,
    monkeypatch,
):
    user, organization = assistant_user
    requirement = Requirement(
        organization_id=organization.id,
        title="Expediente con nota realtime",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Anade la nota acordada al expediente",
    )
    assert started.status_code == 200
    private_note = "DETALLE_INTERNO_QUE_NO_DEBE_ENTRAR_EN_EL_PROMPT"
    tool_response = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        turn_id,
        call_id="call_add_note_before_reconnect",
        name="add_requirement_message",
        arguments={"requirement_id": requirement.id, "body": private_note},
    )
    assert tool_response.status_code == 200
    assert tool_response.json()["ok"] is True
    cancelled = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id="response_cancelled_before_reconnect",
        assistant_text="",
        response_status="cancelled",
        interrupted=True,
    )
    assert cancelled.status_code == 200
    assert "No las repitas automáticamente" in cancelled.json()[
        "assistant_message"
    ]["content"]

    captured = {}
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "assistant_realtime_enabled", True)

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse({"value": "ek_test", "expires_at": 123})

    monkeypatch.setattr("app.assistant.realtime.urlrequest.urlopen", fake_urlopen)
    session_response = client.post(
        f"/assistant/conversations/{conversation['id']}/realtime/session",
        headers=headers_for(user),
    )

    assert session_response.status_code == 200
    instructions = captured["payload"]["session"]["instructions"]
    assert "No las repitas automáticamente" in instructions
    assert "correctas: add_requirement_message (1)" in instructions
    assert private_note not in instructions
    state = get_conversation_state(db, conversation["id"])
    assert state["realtime_voice"]["active_turn"] is None
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert [message.role for message in stored_messages] == ["user", "assistant"]
    assert stored_messages[-1].content.startswith("Respuesta interrumpida.")
    assert json.loads(stored_messages[-1].actions or "[]") == [
        tool_response.json()["action"]
    ]


def post_realtime_turn_start(
    client,
    user,
    conversation_id: int,
    *,
    turn_id: str,
    user_text: str,
):
    return client.post(
        f"/assistant/conversations/{conversation_id}/realtime/turns/start",
        json={"turn_id": turn_id, "user_text": user_text},
        headers=headers_for(user),
    )


def post_realtime_tool_call(
    client,
    user,
    conversation_id: int,
    turn_id: str,
    *,
    call_id: str,
    name: str,
    arguments: dict,
):
    return client.post(
        (
            f"/assistant/conversations/{conversation_id}/realtime/turns/"
            f"{turn_id}/tool-calls"
        ),
        json={"call_id": call_id, "name": name, "arguments": arguments},
        headers=headers_for(user),
    )


def post_realtime_turn_complete(
    client,
    user,
    conversation_id: int,
    turn_id: str,
    *,
    response_id: str,
    assistant_text: str,
    response_status: str = "completed",
    interrupted: bool = False,
):
    return client.post(
        (
            f"/assistant/conversations/{conversation_id}/realtime/turns/"
            f"{turn_id}/complete"
        ),
        json={
            "response_id": response_id,
            "response_status": response_status,
            "assistant_text": assistant_text,
            "interrupted": interrupted,
        },
        headers=headers_for(user),
    )


def create_realtime_requirement_proposal(
    client,
    user,
    conversation_id: int,
    tool_input: dict,
) -> tuple[str, dict, dict]:
    turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation_id,
        turn_id=turn_id,
        user_text="Prepara esta necesidad como borrador",
    )
    assert started.status_code == 200
    tool_response = post_realtime_tool_call(
        client,
        user,
        conversation_id,
        turn_id,
        call_id="call_proposal",
        name="create_requirement",
        arguments=tool_input,
    )
    assert tool_response.status_code == 200
    return turn_id, started.json(), tool_response.json()


def arm_realtime_requirement_proposal(
    client,
    user,
    conversation_id: int,
    tool_input: dict,
) -> tuple[dict, dict]:
    turn_id, _, tool_body = create_realtime_requirement_proposal(
        client,
        user,
        conversation_id,
        tool_input,
    )
    prompt = tool_body["confirmation_prompt"]
    completed = post_realtime_turn_complete(
        client,
        user,
        conversation_id,
        turn_id,
        response_id=f"response-{turn_id}",
        assistant_text=prompt,
    )
    assert completed.status_code == 200
    return tool_body, completed.json()


def test_realtime_turn_start_is_idempotent_and_user_text_is_immutable(
    client,
    db,
    assistant_user,
):
    user, _ = assistant_user
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id = str(uuid.uuid4())

    first = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Lista las necesidades abiertas",
    )
    replay = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Lista las necesidades abiertas",
    )
    mismatch = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Texto alterado después de iniciar el turno",
    )

    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert first.json()["turn_id"] == turn_id
    assert first.json()["user_message"]["content"] == "Lista las necesidades abiertas"
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["user_message"]["id"] == first.json()["user_message"]["id"]
    assert mismatch.status_code == 409
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert len(stored_messages) == 1
    assert stored_messages[0].role == "user"
    assert stored_messages[0].content == "Lista las necesidades abiertas"


def test_realtime_new_speech_supersedes_open_turn_without_reusing_it(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    requirement = Requirement(
        organization_id=organization.id,
        title="Acción realtime auditable",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    first_turn_id = str(uuid.uuid4())
    second_turn_id = str(uuid.uuid4())

    first = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=first_turn_id,
        user_text="Primera intervención",
    )
    first_call = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        first_turn_id,
        call_id="call_first_turn",
        name="list_requirements",
        arguments={"organization_id": organization.id},
    )
    second = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=second_turn_id,
        user_text="Intervención que sustituye la anterior",
    )
    old_call = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        first_turn_id,
        call_id="call_old_turn",
        name="list_requirements",
        arguments={},
    )

    assert first.status_code == 200
    assert first_call.status_code == 200
    assert second.status_code == 200
    assert old_call.status_code == 409
    state = get_conversation_state(db, conversation["id"])["realtime_voice"]
    assert state["active_turn"]["turn_id"] == second_turn_id
    archived = next(
        turn
        for turn in state["recent_turns"]
        if turn["turn_id"] == first_turn_id
    )
    assert archived["status"] == "superseded"
    assert archived["assistant_message_id"]
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert [message.role for message in stored_messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert stored_messages[1].content.startswith("Respuesta de voz interrumpida.")
    assert "No las repitas automáticamente" in stored_messages[1].content
    assert "correctas: list_requirements (1)" in stored_messages[1].content
    assert json.loads(stored_messages[1].actions or "[]") == [
        first_call.json()["action"]
    ]


def test_realtime_new_turn_rejects_tool_call_still_in_progress(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    first_turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=first_turn_id,
        user_text="Ejecuta una consulta",
    )
    assert started.status_code == 200
    state = get_conversation_state(db, conversation["id"])
    active_turn = state["realtime_voice"]["active_turn"]
    active_turn["call_order"].append("call_in_progress")
    active_turn["calls"]["call_in_progress"] = {
        "request_digest": "reserved",
        "status": "started",
        "name": "list_requirements",
        "input": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    stored_conversation.state = json.dumps(state, ensure_ascii=False)
    db.commit()

    superseding = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=str(uuid.uuid4()),
        user_text="No debe sustituir el turno en ejecución",
    )

    assert superseding.status_code == 409
    normal_turn = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Tampoco debe insertar un mensaje de texto"},
        headers=headers_for(user),
    )
    assert normal_turn.status_code == 409
    state = get_conversation_state(db, conversation["id"])
    assert state["realtime_voice"]["active_turn"]["turn_id"] == first_turn_id
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert [message.role for message in stored_messages] == ["user"]


def test_realtime_expired_tool_lease_is_sealed_as_indeterminate(
    client,
    db,
    assistant_user,
):
    user, _ = assistant_user
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    first_turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=first_turn_id,
        user_text="Turno cuyo worker dejó de responder",
    )
    assert started.status_code == 200
    state = get_conversation_state(db, conversation["id"])
    active_turn = state["realtime_voice"]["active_turn"]
    active_turn["call_order"].append("call_expired")
    active_turn["calls"]["call_expired"] = {
        "request_digest": "reserved",
        "status": "started",
        "name": "list_requirements",
        "input": {},
        "started_at": "2000-01-01T00:00:00+00:00",
    }
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    stored_conversation.state = json.dumps(state, ensure_ascii=False)
    db.commit()

    next_turn = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=str(uuid.uuid4()),
        user_text="Continúa sin repetir la acción incierta",
    )

    assert next_turn.status_code == 200
    state = get_conversation_state(db, conversation["id"])
    archived = next(
        turn
        for turn in state["realtime_voice"]["recent_turns"]
        if turn["turn_id"] == first_turn_id
    )
    expired_call = archived["calls"]["call_expired"]
    assert archived["status"] == "superseded"
    assert expired_call["status"] == "indeterminate"
    assert expired_call["action"]["ok"] is False
    assert "Revisa el sistema" in expired_call["action"]["result"]
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert json.loads(stored_messages[1].actions or "[]") == [
        expired_call["action"]
    ]


def test_realtime_read_tool_replays_calls_and_persists_server_actions(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    requirement = Requirement(
        organization_id=organization.id,
        title="Actualizar inventario",
        summary="Inventario de luminarias",
        created_by_id=user.id,
    )
    db.add(requirement)
    db.commit()
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Lista las necesidades abiertas",
    )
    assert started.status_code == 200
    arguments = {"organization_id": organization.id}

    tool_response = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        turn_id,
        call_id="call_read",
        name="list_requirements",
        arguments=arguments,
    )
    replay = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        turn_id,
        call_id="call_read",
        name="list_requirements",
        arguments=arguments,
    )
    mismatch = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        turn_id,
        call_id="call_read",
        name="list_requirements",
        arguments={"organization_id": organization.id + 1},
    )

    assert tool_response.status_code == 200
    tool_body = tool_response.json()
    assert tool_body["call_id"] == "call_read"
    assert tool_body["ok"] is True
    assert tool_body["replayed"] is False
    assert tool_body["action"]["tool"] == "list_requirements"
    assert "Actualizar inventario" in tool_body["output"]
    assert tool_body["confirmation_prompt"] is None
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["action"] == tool_body["action"]
    assert replay.json()["output"] == tool_body["output"]
    assert mismatch.status_code == 409

    complete_payload = {
        "response_id": f"response-{turn_id}",
        "assistant_text": "Hay una necesidad abierta: actualizar inventario.",
    }
    completed = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        **complete_payload,
    )
    complete_replay = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        **complete_payload,
    )

    assert completed.status_code == 200
    complete_body = completed.json()
    assert complete_body["replayed"] is False
    assert complete_body["confirmation_prompt"] is None
    assert complete_body["confirmation_delivery_required"] is False
    assert complete_body["assistant_message"]["actions"] == [tool_body["action"]]
    assert complete_replay.status_code == 200
    assert complete_replay.json()["replayed"] is True
    assert (
        complete_replay.json()["assistant_message"]["id"]
        == complete_body["assistant_message"]["id"]
    )
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert [message.role for message in stored_messages] == ["user", "assistant"]
    assert json.loads(stored_messages[-1].actions or "[]") == [tool_body["action"]]


def test_realtime_tool_exception_is_cached_as_indeterminate(
    client,
    db,
    assistant_user,
    monkeypatch,
):
    user, organization = assistant_user
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Ejecuta una consulta que falla de forma inesperada",
    )
    assert started.status_code == 200

    def fail_unexpectedly(*args, **kwargs):
        raise RuntimeError("unexpected tool failure")

    monkeypatch.setattr(
        "app.assistant.realtime.execute_tool",
        fail_unexpectedly,
    )
    payload = {
        "call_id": "call_indeterminate",
        "name": "list_requirements",
        "arguments": {"organization_id": organization.id},
    }
    first = client.post(
        (
            f"/assistant/conversations/{conversation['id']}/realtime/turns/"
            f"{turn_id}/tool-calls"
        ),
        json=payload,
        headers=headers_for(user),
    )
    replay = client.post(
        (
            f"/assistant/conversations/{conversation['id']}/realtime/turns/"
            f"{turn_id}/tool-calls"
        ),
        json=payload,
        headers=headers_for(user),
    )

    assert first.status_code == 200
    assert first.json()["ok"] is False
    assert "Revisa el sistema" in first.json()["output"]
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["action"] == first.json()["action"]
    followup_call = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        turn_id,
        call_id="call_after_indeterminate",
        name="list_requirements",
        arguments={"organization_id": organization.id},
    )
    assert followup_call.status_code == 409
    completed = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id=f"response-{turn_id}",
        assistant_text="No se pudo confirmar el resultado de la consulta.",
    )
    assert completed.status_code == 200
    assert completed.json()["assistant_message"]["actions"] == [
        first.json()["action"]
    ]
    state = get_conversation_state(db, conversation["id"])
    archived = next(
        turn
        for turn in state["realtime_voice"]["recent_turns"]
        if turn["turn_id"] == turn_id
    )
    assert archived["calls"]["call_indeterminate"]["status"] == "indeterminate"


def test_realtime_tool_output_is_bounded(
    client,
    assistant_user,
    monkeypatch,
):
    user, organization = assistant_user
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id = str(uuid.uuid4())
    post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=turn_id,
        user_text="Devuelve un resultado grande",
    )
    monkeypatch.setattr(
        "app.assistant.realtime.execute_tool",
        lambda *args, **kwargs: assistant_tools.ToolResult(
            content="x" * 5000,
            ok=True,
        ),
    )

    response = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        turn_id,
        call_id="call_large_output",
        name="list_requirements",
        arguments={"organization_id": organization.id},
    )

    assert response.status_code == 200
    assert len(response.json()["output"]) == 4000
    assert len(response.json()["action"]["result"]) == 4000


def test_realtime_requirement_confirmation_arms_only_after_exact_completed_prompt(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Portal ciudadano realtime",
        "problem": "Las solicitudes se reciben por correo.",
        "summary": "Centralizar solicitudes ciudadanas.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    turn_id, _, tool_body = create_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )

    assert tool_body["ok"] is False
    assert tool_body["replayed"] is False
    prompt = tool_body["confirmation_prompt"]
    assert isinstance(prompt, str) and prompt
    assert "Borrador pendiente de confirmación" in prompt
    assert tool_input["title"] in prompt
    assert tool_input["problem"] in prompt
    assert "prioridad: medium" in prompt
    assert "estado al guardar: draft" in prompt
    assert "origen: conversation" in prompt
    assert "###" not in prompt
    assert "**" not in prompt
    assert "`" not in prompt
    assert db.scalar(select(Requirement)) is None
    pending_before_completion = get_pending_confirmation(db, conversation["id"])
    assert "prompted_at_assistant_message_id" not in pending_before_completion

    completed = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id=f"response-{turn_id}",
        assistant_text=prompt,
        response_status="completed",
        interrupted=False,
    )

    assert completed.status_code == 200
    complete_body = completed.json()
    assert complete_body["replayed"] is False
    assert complete_body["confirmation_prompt"] == prompt
    assert complete_body["confirmation_delivery_required"] is False
    assert complete_body["assistant_message"]["content"] == prompt
    pending = get_pending_confirmation(db, conversation["id"])
    assert (
        pending["prompted_at_assistant_message_id"]
        == complete_body["assistant_message"]["id"]
    )


def test_realtime_interrupted_prompt_does_not_arm_confirmation(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Propuesta interrumpida",
        "problem": "Debe oírse el contenido exacto antes de confirmar.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id, _, tool_body = create_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )
    prompt = tool_body["confirmation_prompt"]

    completed = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id=f"response-{turn_id}",
        assistant_text=prompt,
        response_status="completed",
        interrupted=True,
    )

    assert completed.status_code == 200
    assert completed.json()["confirmation_prompt"] == prompt
    assert completed.json()["confirmation_delivery_required"] is False
    interrupted_content = completed.json()["assistant_message"]["content"]
    assert interrupted_content.startswith(prompt)
    assert "No las repitas automáticamente" in interrupted_content
    pending = get_pending_confirmation(db, conversation["id"])
    assert "prompted_at_assistant_message_id" not in pending

    confirmation_turn_id = str(uuid.uuid4())
    confirmation_started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=confirmation_turn_id,
        user_text="Sí, créalo",
    )
    assert confirmation_started.status_code == 200
    attempted_confirmation = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        confirmation_turn_id,
        call_id="call_hidden_confirmation",
        name="create_requirement",
        arguments=tool_input,
    )

    assert attempted_confirmation.status_code == 200
    assert attempted_confirmation.json()["ok"] is False
    assert attempted_confirmation.json()["confirmation_prompt"] == prompt
    assert db.scalar(select(Requirement)) is None


def test_realtime_paraphrased_prompt_requires_exact_delivery_before_arming(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Propuesta parafraseada",
        "problem": "La paráfrasis no debe autorizar una confirmación.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id, _, tool_body = create_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )
    prompt = tool_body["confirmation_prompt"]

    paraphrased = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id=f"paraphrased-{turn_id}",
        assistant_text="He preparado el borrador. ¿Lo confirmas?",
        response_status="completed",
        interrupted=False,
    )

    assert paraphrased.status_code == 200
    assert paraphrased.json()["assistant_message"] is None
    assert paraphrased.json()["confirmation_prompt"] == prompt
    assert paraphrased.json()["confirmation_delivery_required"] is True
    pending = get_pending_confirmation(db, conversation["id"])
    assert "prompted_at_assistant_message_id" not in pending
    superseding_turn_id = str(uuid.uuid4())
    superseding_turn = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=superseding_turn_id,
        user_text="Sí, créalo",
    )
    assert superseding_turn.status_code == 200

    stale_delivery = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id=f"canonical-{turn_id}",
        assistant_text=prompt,
        response_status="completed",
        interrupted=False,
    )

    assert stale_delivery.status_code == 409
    state = get_conversation_state(db, conversation["id"])
    superseded_turn = next(
        turn
        for turn in state["realtime_voice"]["recent_turns"]
        if turn["turn_id"] == turn_id
    )
    assert superseded_turn["status"] == "superseded"
    assert "prompted_at_assistant_message_id" not in state["pending_confirmation"]

    delivered = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        superseding_turn_id,
        response_id=f"canonical-{superseding_turn_id}",
        assistant_text=prompt,
        response_status="completed",
        interrupted=False,
    )

    assert delivered.status_code == 200
    assert delivered.json()["confirmation_delivery_required"] is False
    assert delivered.json()["assistant_message"]["content"].endswith(prompt)
    pending = get_pending_confirmation(db, conversation["id"])
    assert (
        pending["prompted_at_assistant_message_id"]
        == delivered.json()["assistant_message"]["id"]
    )


def test_realtime_confirmation_delivery_responses_are_bounded(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Propuesta con respuestas acotadas",
        "problem": "No debe crecer el estado con paráfrasis ilimitadas.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id, _, _ = create_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )

    for index in range(4):
        response = post_realtime_turn_complete(
            client,
            user,
            conversation["id"],
            turn_id,
            response_id=f"paraphrase-{index}",
            assistant_text=f"Paráfrasis incompleta {index}",
        )
        assert response.status_code == 200
        assert response.json()["confirmation_delivery_required"] is True

    overflow = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id="paraphrase-overflow",
        assistant_text="Una paráfrasis adicional",
    )

    assert overflow.status_code == 409
    state = get_conversation_state(db, conversation["id"])
    assert len(state["realtime_voice"]["active_turn"]["responses"]) == 4


def test_realtime_explicit_confirmation_is_consumed_once(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Confirmación realtime",
        "problem": "La gestión actual requiere duplicar datos.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    _, armed = arm_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )
    prompt_message_id = armed["assistant_message"]["id"]
    confirmation_turn_id = str(uuid.uuid4())
    confirmation_started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=confirmation_turn_id,
        user_text="Sí, créalo",
    )
    assert confirmation_started.status_code == 200

    confirmed = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        confirmation_turn_id,
        call_id="call_confirm",
        name="create_requirement",
        arguments=tool_input,
    )
    replay = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        confirmation_turn_id,
        call_id="call_confirm",
        name="create_requirement",
        arguments=tool_input,
    )
    second_call = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        confirmation_turn_id,
        call_id="call_confirm_again",
        name="create_requirement",
        arguments=tool_input,
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["ok"] is True
    assert confirmed.json()["replayed"] is False
    assert confirmed.json()["confirmation_prompt"] is None
    assert replay.status_code == 200
    assert replay.json()["ok"] is True
    assert replay.json()["replayed"] is True
    assert second_call.status_code == 200
    assert second_call.json()["ok"] is False
    assert "confirmación" in second_call.json()["output"].lower()
    requirements = db.scalars(select(Requirement)).all()
    assert len(requirements) == 1
    assert requirements[0].title == tool_input["title"]
    assert requirements[0].status == "draft"
    assert requirements[0].source_type == "conversation"
    state = get_conversation_state(db, conversation["id"])
    assert "pending_confirmation" not in state
    assert (
        state["last_consumed_confirmation"]["prompted_at_assistant_message_id"]
        == prompt_message_id
    )
    assert (
        state["last_consumed_confirmation"]["consumed_at_user_message_id"]
        == confirmation_started.json()["user_message"]["id"]
    )


def test_realtime_requirement_rejects_feedback_specific_confirmation(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Confirmación cruzada realtime",
        "problem": "La acción debe coincidir con el verbo confirmado.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    proposal, _ = arm_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )
    confirmation_turn_id = str(uuid.uuid4())
    confirmation_started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=confirmation_turn_id,
        user_text="Sí, envíalo",
    )
    assert confirmation_started.status_code == 200

    attempted = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        confirmation_turn_id,
        call_id="call_cross_confirmation",
        name="create_requirement",
        arguments=tool_input,
    )

    assert attempted.status_code == 200
    assert attempted.json()["ok"] is False
    assert attempted.json()["confirmation_prompt"] == proposal["confirmation_prompt"]
    assert db.scalar(select(Requirement)) is None
    pending = get_pending_confirmation(db, conversation["id"])
    assert pending["tool"] == "create_requirement"
    assert "response_user_message_id" not in pending


def test_realtime_confirmation_with_changed_payload_starts_new_proposal(
    client,
    db,
    assistant_user,
):
    user, organization = assistant_user
    original_input = {
        "organization_id": organization.id,
        "title": "Payload realtime",
        "problem": "Problema originalmente confirmado.",
    }
    changed_input = {
        **original_input,
        "problem": "Problema modificado después de pedir confirmación.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    arm_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        original_input,
    )
    original_pending = get_pending_confirmation(db, conversation["id"])
    confirmation_turn_id = str(uuid.uuid4())
    started = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=confirmation_turn_id,
        user_text="Sí, créalo",
    )
    assert started.status_code == 200

    changed = post_realtime_tool_call(
        client,
        user,
        conversation["id"],
        confirmation_turn_id,
        call_id="call_changed_payload",
        name="create_requirement",
        arguments=changed_input,
    )

    assert changed.status_code == 200
    changed_body = changed.json()
    assert changed_body["ok"] is False
    assert changed_input["problem"] in changed_body["confirmation_prompt"]
    assert db.scalar(select(Requirement)) is None
    replacement = get_pending_confirmation(db, conversation["id"])
    assert replacement["confirmation_id"] != original_pending["confirmation_id"]
    assert replacement["input"]["problem"] == changed_input["problem"]


def test_realtime_complete_rejects_turn_superseded_by_normal_user_message(
    client,
    db,
    assistant_user,
    use_gateway,
):
    user, organization = assistant_user
    tool_input = {
        "organization_id": organization.id,
        "title": "Turno realtime obsoleto",
        "problem": "Un mensaje posterior no debe permitir completar este turno.",
    }
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()
    turn_id, _, tool_body = create_realtime_requirement_proposal(
        client,
        user,
        conversation["id"],
        tool_input,
    )
    prompt = tool_body["confirmation_prompt"]
    pending = get_pending_confirmation(db, conversation["id"])
    assert "prompted_at_assistant_message_id" not in pending
    use_gateway(FakeGateway([AssistantUnavailableError("gateway unavailable")]))

    normal_turn = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Continúa por texto con otra cuestión"},
        headers=headers_for(user),
    )
    stale_completion = post_realtime_turn_complete(
        client,
        user,
        conversation["id"],
        turn_id,
        response_id=f"response-{turn_id}",
        assistant_text=prompt,
        response_status="completed",
        interrupted=False,
    )

    assert normal_turn.status_code == 200
    assert normal_turn.json()["messages"][-1]["content"] == ERROR_REPLY
    assert stale_completion.status_code == 409
    assert "superseded" in stale_completion.json()["detail"].lower()
    state = get_conversation_state(db, conversation["id"])
    pending = state["pending_confirmation"]
    assert "prompted_at_assistant_message_id" not in pending
    realtime_state = state["realtime_voice"]
    assert realtime_state["active_turn"] is None
    archived_turn = next(
        turn
        for turn in realtime_state["recent_turns"]
        if turn["turn_id"] == turn_id
    )
    assert archived_turn["status"] == "superseded"
    assert archived_turn["assistant_message_id"]
    db.expire_all()
    stored_messages = db.scalars(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation["id"]
        )
    ).all()
    assert [message.role for message in stored_messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert all(message.content != prompt for message in stored_messages)
    assert db.scalar(select(Requirement)) is None
    next_turn = post_realtime_turn_start(
        client,
        user,
        conversation["id"],
        turn_id=str(uuid.uuid4()),
        user_text="Nuevo turno realtime",
    )
    assert next_turn.status_code == 200


def test_stale_normal_turn_cannot_mutate_after_realtime_user_message(
    engine,
    monkeypatch,
):
    suffix = uuid.uuid4().hex
    with Session(engine, expire_on_commit=False) as seed_db:
        user = User(
            email=f"normal-realtime-race-{suffix}@example.com",
            hashed_password="not-used",
            full_name="Normal Realtime Race Test",
        )
        conversation = AssistantConversation(
            title="Carrera normal y realtime",
            status="active",
            channel="web",
            created_by=user,
        )
        seed_db.add_all([user, conversation])
        seed_db.commit()
        user_id = user.id
        conversation_id = conversation.id

    normal_waiting = threading.Event()
    release_normal = threading.Event()
    mutation_calls: list[str] = []
    turn_id = str(uuid.uuid4())

    class BlockingGateway:
        def __init__(self):
            self.call_count = 0

        def complete(self, *, system, messages, tools):
            self.call_count += 1
            if self.call_count == 1:
                normal_waiting.set()
                assert release_normal.wait(timeout=15)
                return fake_response(
                    "tool_use",
                    [tool_use_block("stale-mutation", "test_mutation", {})],
                )
            return fake_response(
                "end_turn",
                [text_block("El turno anterior ya no puede modificar datos.")],
            )

    mutating_tool = assistant_tools.ToolSpec(
        name="test_mutation",
        label="Mutación de prueba",
        description="Herramienta mutante para probar el orden de turnos.",
        input_schema={"type": "object", "properties": {}},
        executor=lambda *args, **kwargs: {},
        read_only=False,
        domain="test",
    )
    monkeypatch.setattr(
        assistant_turn,
        "get_available_tool_specs",
        lambda db, current_user: [mutating_tool],
    )

    def execute_test_mutation(
        db,
        current_user,
        name,
        tool_input,
        context=None,
        allowed=None,
    ):
        mutation_calls.append(name)
        db.commit()
        return assistant_tools.ToolResult(content='{"mutated": true}', ok=True)

    monkeypatch.setattr(assistant_turn, "execute_tool", execute_test_mutation)

    def run_normal_turn():
        with Session(engine, expire_on_commit=False) as normal_db:
            normal_user = normal_db.get(User, user_id)
            normal_conversation = normal_db.get(
                AssistantConversation,
                conversation_id,
            )
            assert normal_user is not None
            assert normal_conversation is not None
            return assistant_turn.run_agent_turn(
                normal_db,
                normal_user,
                normal_conversation,
                "Ejecuta una mutación desde el turno normal",
                BlockingGateway(),
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            normal_future = executor.submit(run_normal_turn)
            assert normal_waiting.wait(timeout=15)
            try:
                with Session(engine, expire_on_commit=False) as realtime_db:
                    realtime_user = realtime_db.get(User, user_id)
                    realtime_conversation = realtime_db.get(
                        AssistantConversation,
                        conversation_id,
                    )
                    assert realtime_user is not None
                    assert realtime_conversation is not None
                    assistant_realtime.start_realtime_turn(
                        realtime_db,
                        realtime_user,
                        realtime_conversation,
                        AssistantRealtimeTurnStartCreate(
                            turn_id=turn_id,
                            user_text="Este mensaje realtime sustituye al normal",
                        ),
                    )
            finally:
                release_normal.set()
            normal_reply = normal_future.result(timeout=15)

        assert mutation_calls == []
        assert json.loads(normal_reply.actions or "[]") == [
            {
                "tool": "test_mutation",
                "ok": False,
                "input": {},
                "result": assistant_turn.STALE_MUTATING_TOOL_RESULT,
            }
        ]
        with Session(engine) as verification_db:
            stored_conversation = verification_db.get(
                AssistantConversation,
                conversation_id,
            )
            assert stored_conversation is not None
            state = assistant_guards.load_conversation_state(stored_conversation)
            assert state["realtime_voice"]["active_turn"]["turn_id"] == turn_id
    finally:
        release_normal.set()
        with Session(engine) as cleanup_db:
            stored_user = cleanup_db.get(User, user_id)
            if stored_user is not None:
                cleanup_db.delete(stored_user)
                cleanup_db.commit()


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


def test_azure_ssml_supports_optional_prosody_rate():
    ssml = build_azure_ssml("hola", "es-ES-DarioNeural", "es-ES", "+12%")

    assert "<prosody rate='+12%'>hola</prosody>" in ssml


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


def test_normal_turn_hides_web_search_when_runtime_is_incomplete(
    client,
    assistant_user,
    grant_permissions,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    grant_permissions(user, organization, ["assistant.web.search"])
    monkeypatch.setattr(settings, "hermes_web_base_url", "http://web.test/v1")
    monkeypatch.setattr(settings, "hermes_web_api_key", None)
    monkeypatch.setattr(settings, "hermes_web_model", "hermes-agent")
    gateway = use_gateway(
        FakeGateway([fake_response("end_turn", [text_block("Respuesta final.")])])
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Busca información pública"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assert "web_search" not in {
        tool["name"] for tool in gateway.calls[0]["tools"]
    }


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


def test_tool_call_budget_skips_excess_calls_and_forces_tool_free_synthesis(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_max_tool_iterations", 8)
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 2)
    executed_inputs: list[dict] = []

    def record_execution(
        db,
        current_user,
        tool_name,
        tool_input,
        context,
        *,
        allowed,
    ):
        executed_inputs.append(tool_input)
        return assistant_tools.ToolResult(content="[]", ok=True)

    monkeypatch.setattr(assistant_turn, "execute_tool", record_execution)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        text_block("Voy a consultar varias veces antes de responder."),
                        tool_use_block(
                            "call_1",
                            "list_requirements",
                            {
                                "organization_id": organization.id,
                                "status": "draft",
                            },
                        ),
                        tool_use_block(
                            "call_2",
                            "list_requirements",
                            {
                                "organization_id": organization.id,
                                "status": "submitted",
                            },
                        ),
                        tool_use_block(
                            "call_3",
                            "list_requirements",
                            {
                                "organization_id": organization.id,
                                "status": "accepted",
                            },
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Consulté dos estados; no pude consultar el tercero.")],
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
        json={"content": "Resume las necesidades por estado"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["content"] == (
        "Consulté dos estados; no pude consultar el tercero."
    )
    assert "Voy a consultar" not in assistant_message["content"]
    assert len(executed_inputs) == 2
    assert [action["ok"] for action in assistant_message["actions"]] == [
        True,
        True,
        False,
    ]
    assert "presupuesto" in assistant_message["actions"][-1]["result"].lower()
    assert len(gateway.calls) == 2
    assert gateway.calls[-1]["tools"] == []


def test_repeated_equivalent_read_call_is_suppressed_and_forces_synthesis(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_max_tool_iterations", 8)
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 8)
    execution_count = 0

    def record_execution(
        db,
        current_user,
        tool_name,
        tool_input,
        context,
        *,
        allowed,
    ):
        nonlocal execution_count
        execution_count += 1
        return assistant_tools.ToolResult(content="[]", ok=True)

    monkeypatch.setattr(assistant_turn, "execute_tool", record_execution)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_1",
                            "list_requirements",
                            {
                                "organization_id": organization.id,
                                "status": "draft",
                            },
                        )
                    ],
                ),
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_2",
                            "list_requirements",
                            {
                                "status": "  DRAFT ",
                                "organization_id": organization.id,
                            },
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("No hay necesidades en borrador.")],
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
        json={"content": "Lista las necesidades en borrador"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["content"] == "No hay necesidades en borrador."
    assert execution_count == 1
    assert [action["ok"] for action in assistant_message["actions"]] == [True, False]
    assert "equivalente" in assistant_message["actions"][-1]["result"].lower()
    assert len(gateway.calls) == 3
    assert gateway.calls[-1]["tools"] == []


def test_non_retryable_failed_read_tool_is_not_retried_with_new_arguments(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_max_tool_iterations", 8)
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 8)
    execution_count = 0

    def fail_execution(
        db,
        current_user,
        tool_name,
        tool_input,
        context,
        *,
        allowed,
    ):
        nonlocal execution_count
        execution_count += 1
        return assistant_tools.ToolResult(
            content="Error (503): servicio no configurado",
            ok=False,
        )

    monkeypatch.setattr(assistant_turn, "execute_tool", fail_execution)
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
                    "tool_use",
                    [
                        tool_use_block(
                            "call_2",
                            "list_requirements",
                            {
                                "organization_id": organization.id,
                                "status": "draft",
                            },
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "No pude consultar las necesidades porque el servicio "
                            "no está configurado."
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

    response = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Lista las necesidades"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert execution_count == 1
    assert "no está configurado" in assistant_message["content"]
    assert [action["ok"] for action in assistant_message["actions"]] == [False, False]
    assert gateway.calls[-1]["tools"] == []


def test_tool_round_limit_forces_tool_free_completion_instead_of_fallback(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_max_tool_iterations", 1)
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 8)
    gateway = use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        text_block("Voy a revisar los datos."),
                        tool_use_block(
                            "call_1",
                            "list_requirements",
                            {"organization_id": organization.id},
                        ),
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("La consulta terminó sin necesidades visibles.")],
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
        json={"content": "Consulta las necesidades"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["content"] == (
        "La consulta terminó sin necesidades visibles."
    )
    assert "Voy a revisar" not in assistant_message["content"]
    assert assistant_message["content"] != assistant_turn.FALLBACK_REPLY
    assert len(gateway.calls) == 2
    assert gateway.calls[-1]["tools"] == []


def test_invalid_forced_synthesis_uses_explicit_loop_limit_reply(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
):
    user, organization = assistant_user
    monkeypatch.setattr(settings, "assistant_max_tool_iterations", 1)
    monkeypatch.setattr(settings, "assistant_max_tool_calls", 8)
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
                    "tool_use",
                    [
                        text_block("Voy a consultar otra vez."),
                        tool_use_block(
                            "call_2",
                            "list_requirements",
                            {"organization_id": organization.id},
                        ),
                    ],
                    deltas=["Voy a consultar otra vez."],
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
        json={"content": "Consulta las necesidades"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    assistant_message = response.json()["messages"][-1]
    assert assistant_message["content"] == assistant_turn.TOOL_LOOP_LIMIT_REPLY
    assert assistant_message["content"] != assistant_turn.FALLBACK_REPLY
    assert "Voy a consultar" not in assistant_message["content"]
    assert len(gateway.calls) == 2
    assert gateway.calls[-1]["tools"] == []


def get_conversation_state(db, conversation_id: int) -> dict:
    db.expire_all()
    conversation = db.get(AssistantConversation, conversation_id)
    assert conversation is not None
    return json.loads(conversation.state or "{}")


def get_pending_confirmation(db, conversation_id: int) -> dict:
    return get_conversation_state(db, conversation_id)["pending_confirmation"]


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
    prompt_message = first.json()["messages"][-1]
    prompt_message_id = prompt_message["id"]
    assert "Borrador pendiente de confirmación" in prompt_message["content"]
    assert tool_input["title"] in prompt_message["content"]
    assert tool_input["problem"] in prompt_message["content"]
    assert tool_input["summary"] in prompt_message["content"]
    assert tool_input["acceptance_criteria"] in prompt_message["content"]
    pending = get_pending_confirmation(db, conversation["id"])
    assert pending["tool"] == "create_requirement"
    assert pending["confirmation_id"]
    assert pending["confirmation_id"] in prompt_message["content"]
    assert '"prioridad": "medium"' in prompt_message["content"]
    assert '"estado_al_guardar": "draft"' in prompt_message["content"]
    assert '"origen": "conversation"' in prompt_message["content"]
    assert pending["prompted_at_assistant_message_id"] == prompt_message_id

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
    assert requirement.problem == tool_input["problem"]
    assert requirement.summary == tool_input["summary"]
    assert requirement.acceptance_criteria == tool_input["acceptance_criteria"]
    assert requirement.status == "draft"
    assert requirement.source_type == "conversation"
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    state = json.loads(stored_conversation.state or "{}")
    assert "pending_confirmation" not in state
    assert (
        state["last_consumed_confirmation"][
            "prompted_at_assistant_message_id"
        ]
        == prompt_message_id
    )
    assert len(gateway.calls) == 4


def test_create_requirement_confirms_normalized_effective_payload(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    stored_title = "P" * 255
    proposed_input = {
        "organization_id": organization.id,
        "title": f"  {stored_title}{'P' * 5}  ",
        "problem": "El alta se hace por correo.",
    }
    confirmed_input = {
        "organization_id": organization.id,
        "title": stored_title,
        "problem": "El alta se hace por correo.",
        "priority": "medium",
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_1",
                            "create_requirement",
                            proposed_input,
                        )
                    ],
                ),
                fake_response("end_turn", [text_block("Propuesta preparada.")]),
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_2",
                            "create_requirement",
                            confirmed_input,
                        )
                    ],
                ),
                fake_response("end_turn", [text_block("Borrador creado.")]),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    proposed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Prepara esta necesidad"},
        headers=headers_for(user),
    )
    pending = get_pending_confirmation(db, conversation["id"])
    confirmed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert proposed.status_code == 200
    assert pending["input"]["title"] == stored_title
    assert pending["input"]["priority"] == "medium"
    proposal_content = proposed.json()["messages"][-1]["content"]
    assert stored_title in proposal_content
    assert proposed_input["title"] not in proposal_content
    assert confirmed.status_code == 200
    assert confirmed.json()["messages"][-1]["actions"][0]["ok"] is True
    requirement = db.scalar(select(Requirement))
    assert requirement is not None
    assert requirement.title == stored_title
    assert requirement.priority == "medium"


def test_create_requirement_rejects_non_integer_identifier_before_confirmation(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    invalid_input = {
        "organization_id": organization.id + 0.9,
        "title": "Portal ciudadano",
        "problem": "El alta se hace por correo.",
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_1",
                            "create_requirement",
                            invalid_input,
                        )
                    ],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Necesito un identificador válido.")],
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
        json={"content": "Prepara esta necesidad"},
        headers=headers_for(user),
    )

    assert response.status_code == 200
    action = response.json()["messages"][-1]["actions"][0]
    assert action["ok"] is False
    assert "entero positivo" in action["result"]
    assert db.scalar(select(Requirement)) is None
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    assert "pending_confirmation" not in json.loads(
        stored_conversation.state or "{}"
    )


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
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    assert "pending_confirmation" not in json.loads(
        stored_conversation.state or "{}"
    )


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
    assert (
        pending["prompted_at_assistant_message_id"]
        == second.json()["messages"][-1]["id"]
    )


def test_create_requirement_confirmation_without_tool_call_requires_fresh_prompt(
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
                fake_response("end_turn", [text_block("Entendido.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response(
                    "end_turn",
                    [text_block("Necesito una confirmación nueva.")],
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
    pending = get_pending_confirmation(db, conversation["id"])
    confirmed_without_action = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )
    repeated_prompt = confirmed_without_action.json()["messages"][-1]
    current = get_pending_confirmation(db, conversation["id"])
    later_attempt = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert confirmed_without_action.status_code == 200
    assert "Borrador pendiente de confirmación" in repeated_prompt["content"]
    assert current["confirmation_id"] == pending["confirmation_id"]
    assert current["prompted_at_assistant_message_id"] == repeated_prompt["id"]
    assert later_attempt.status_code == 200
    action = later_attempt.json()["messages"][-1]["actions"][0]
    assert action["ok"] is True
    assert db.scalar(select(Requirement)) is not None


def test_create_requirement_allows_dialogue_before_fresh_confirmation(
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
        "priority": "high",
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [tool_use_block("call_1", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("He preparado la propuesta.")]),
                fake_response(
                    "end_turn",
                    [
                        text_block(
                            "La prioridad alta permite revisarla antes, pero sigue "
                            "siendo un borrador."
                        )
                    ],
                ),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Borrador creado.")]),
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
        json={"content": "Prepara una necesidad para el portal"},
        headers=headers_for(user),
    )
    initial = get_pending_confirmation(db, conversation["id"])
    discussed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "¿Por qué propones prioridad alta?"},
        headers=headers_for(user),
    )
    discussion_message = discussed.json()["messages"][-1]
    refreshed = get_pending_confirmation(db, conversation["id"])
    confirmed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert discussed.status_code == 200
    assert discussion_message["actions"] == []
    assert "La prioridad alta" in discussion_message["content"]
    assert "Borrador pendiente de confirmación" in discussion_message["content"]
    assert tool_input["problem"] in discussion_message["content"]
    assert refreshed["confirmation_id"] == initial["confirmation_id"]
    assert refreshed["prompted_at_assistant_message_id"] == discussion_message["id"]
    assert confirmed.status_code == 200
    assert confirmed.json()["messages"][-1]["actions"][0]["ok"] is True
    assert db.scalar(select(Requirement)) is not None


def test_create_requirement_voice_confirmation_prompt_is_plain_text(
    client,
    assistant_user,
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
                fake_response("end_turn", [text_block("He preparado el borrador.")]),
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
        json={"content": "Prepara una necesidad", "input_mode": "voice"},
        headers=headers_for(user),
    ) as response:
        events = parse_sse("".join(response.iter_text()))

    assert response.status_code == 200
    confirmation_deltas = [
        event["data"]["text"]
        for event in events
        if event["event"] == "text_delta"
        and "Borrador pendiente de confirmación" in event["data"]["text"]
    ]
    assert len(confirmation_deltas) == 1
    spoken_confirmation = confirmation_deltas[0]
    content = events[-1]["data"]["message"]["content"]
    assert "Borrador pendiente de confirmación" in content
    assert tool_input["title"] in spoken_confirmation
    assert tool_input["problem"] in spoken_confirmation
    assert "###" not in spoken_confirmation
    assert "**" not in spoken_confirmation
    assert "`" not in spoken_confirmation


def test_create_requirement_gateway_failure_does_not_arm_hidden_proposal(
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
                AssistantUnavailableError("gateway unavailable"),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_2", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Confirma el borrador.")]),
                fake_response(
                    "tool_use",
                    [tool_use_block("call_3", "create_requirement", tool_input)],
                ),
                fake_response("end_turn", [text_block("Borrador creado.")]),
            ]
        )
    )
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    failed_proposal = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Propón una necesidad"},
        headers=headers_for(user),
    )
    hidden_pending = get_pending_confirmation(db, conversation["id"])
    attempted_confirmation = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )
    visible_prompt = attempted_confirmation.json()["messages"][-1]
    armed_pending = get_pending_confirmation(db, conversation["id"])
    confirmed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert failed_proposal.status_code == 200
    assert failed_proposal.json()["messages"][-1]["content"] == ERROR_REPLY
    assert "prompted_at_assistant_message_id" not in hidden_pending
    assert attempted_confirmation.status_code == 200
    action = attempted_confirmation.json()["messages"][-1]["actions"][0]
    assert action["ok"] is False
    assert "confirmación" in action["result"].lower()
    assert "Borrador pendiente de confirmación" in visible_prompt["content"]
    assert (
        armed_pending["prompted_at_assistant_message_id"]
        == visible_prompt["id"]
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["messages"][-1]["actions"][0]["ok"] is True
    assert db.scalar(select(Requirement)) is not None


def test_create_requirement_duplicate_tool_call_uses_confirmation_once(
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
                    [
                        tool_use_block("call_2", "create_requirement", tool_input),
                        tool_use_block("call_3", "create_requirement", tool_input),
                    ],
                ),
                fake_response("end_turn", [text_block("Borrador creado una vez.")]),
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
    confirmed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert confirmed.status_code == 200
    actions = confirmed.json()["messages"][-1]["actions"]
    assert [action["ok"] for action in actions] == [True, False]
    assert "ya se utilizó" in actions[1]["result"]
    assert len(list(db.scalars(select(Requirement)))) == 1
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    assert "pending_confirmation" not in json.loads(
        stored_conversation.state or "{}"
    )


def test_create_requirement_mixed_tool_calls_keep_new_proposal_visible(
    client,
    assistant_user,
    db,
    use_gateway,
):
    user, organization = assistant_user
    confirmed_input = {
        "organization_id": organization.id,
        "title": "Portal ciudadano",
        "problem": "El alta se hace por correo.",
    }
    new_input = {
        "organization_id": organization.id,
        "title": "Archivo electrónico",
        "problem": "Los expedientes se archivan manualmente.",
    }
    use_gateway(
        FakeGateway(
            [
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_1",
                            "create_requirement",
                            confirmed_input,
                        )
                    ],
                ),
                fake_response("end_turn", [text_block("Propuesta preparada.")]),
                fake_response(
                    "tool_use",
                    [
                        tool_use_block(
                            "call_2",
                            "create_requirement",
                            confirmed_input,
                        ),
                        tool_use_block(
                            "call_3",
                            "create_requirement",
                            new_input,
                        ),
                        tool_use_block(
                            "call_4",
                            "create_requirement",
                            confirmed_input,
                        ),
                    ],
                ),
                fake_response("end_turn", [text_block("He procesado las propuestas.")]),
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
        json={"content": "Prepara la primera necesidad"},
        headers=headers_for(user),
    )
    processed = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "Sí, créalo"},
        headers=headers_for(user),
    )

    assert processed.status_code == 200
    message = processed.json()["messages"][-1]
    assert [action["ok"] for action in message["actions"]] == [True, False, False]
    assert new_input["title"] in message["content"]
    assert "Borrador pendiente de confirmación" in message["content"]
    assert len(list(db.scalars(select(Requirement)))) == 1
    pending = get_pending_confirmation(db, conversation["id"])
    assert pending["input"]["title"] == new_input["title"]
    assert pending["prompted_at_assistant_message_id"] == message["id"]


def test_create_requirement_cancellation_without_tool_call_clears_pending(
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
                fake_response("end_turn", [text_block("De acuerdo, cancelado.")]),
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
    cancelled = client.post(
        f"/assistant/conversations/{conversation['id']}/messages",
        json={"content": "No, cancela la creación"},
        headers=headers_for(user),
    )

    assert cancelled.status_code == 200
    assert db.scalar(select(Requirement)) is None
    db.expire_all()
    stored_conversation = db.get(AssistantConversation, conversation["id"])
    assert stored_conversation is not None
    assert "pending_confirmation" not in json.loads(
        stored_conversation.state or "{}"
    )


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
    original = get_pending_confirmation(db, conversation["id"])
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
    replacement = json.loads(stored_conversation.state or "{}")[
        "pending_confirmation"
    ]
    assert replacement["input"]["title"] == "Portal ciudadano"
    assert replacement["input"]["problem"] == changed_input["problem"]
    assert replacement["confirmation_id"] != original["confirmation_id"]


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


def test_confirmation_prompt_binding_ignores_replaced_pending(
    db,
    assistant_user,
):
    user, organization = assistant_user
    conversation = AssistantConversation(
        title="Confirmaciones intercaladas",
        status="active",
        channel="web",
        created_by=user,
    )
    first_user_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Prepara la propuesta A",
    )
    db.add_all([conversation, first_user_message])
    db.flush()
    first_reference = assistant_guards.record_pending_confirmation(
        conversation,
        first_user_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Propuesta A",
            "problem": "Problema A",
        },
    )
    first_prompt = assistant_guards.build_confirmation_prompt(
        conversation,
        first_reference,
        input_mode="text",
        turn_user_message_id=first_user_message.id,
    )
    assert first_prompt is not None

    second_user_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Sustitúyela por la propuesta B",
    )
    db.add(second_user_message)
    db.flush()
    second_reference = assistant_guards.record_pending_confirmation(
        conversation,
        second_user_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Propuesta B",
            "problem": "Problema B",
        },
    )

    late_first_reply = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content=f"Respuesta tardía de la propuesta A\n\n{first_prompt}",
    )
    db.add(late_first_reply)
    db.flush()
    assistant_guards.finalize_confirmation_turn(
        conversation,
        first_user_message,
        late_first_reply,
        first_reference,
        confirmation_prompt=first_prompt,
    )

    state = assistant_guards.load_conversation_state(conversation)
    pending = state["pending_confirmation"]
    assert pending["confirmation_id"] == second_reference.confirmation_id
    assert "prompted_at_assistant_message_id" not in pending
    assert (
        assistant_guards.build_confirmation_prompt(
            conversation,
            first_reference,
            input_mode="text",
            turn_user_message_id=first_user_message.id,
        )
        is None
    )

    second_reply = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content="Respuesta de la propuesta B",
    )
    db.add(second_reply)
    db.flush()
    second_prompt = assistant_guards.build_confirmation_prompt(
        conversation,
        second_reference,
        input_mode="text",
        turn_user_message_id=second_user_message.id,
    )
    assert second_prompt is not None
    second_reply.content = f"Respuesta de la propuesta B\n\n{second_prompt}"
    assistant_guards.finalize_confirmation_turn(
        conversation,
        second_user_message,
        second_reply,
        second_reference,
        confirmation_prompt=second_prompt,
    )

    assert "Propuesta B" in second_prompt
    assert "Propuesta A" not in second_prompt
    pending = assistant_guards.load_conversation_state(conversation)[
        "pending_confirmation"
    ]
    assert pending["prompted_at_assistant_message_id"] == second_reply.id


def test_confirmation_prompt_binding_rejects_hidden_payload(
    db,
    assistant_user,
):
    user, organization = assistant_user
    conversation = AssistantConversation(
        title="Propuesta no visible",
        status="active",
        channel="web",
        created_by=user,
    )
    user_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Prepara una necesidad",
    )
    hidden_reply = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content="He preparado una propuesta.",
    )
    db.add_all([conversation, user_message, hidden_reply])
    db.flush()
    reference = assistant_guards.record_pending_confirmation(
        conversation,
        user_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Portal ciudadano",
            "problem": "El alta se hace por correo.",
        },
    )
    canonical_prompt = assistant_guards.build_confirmation_prompt(
        conversation,
        reference,
        input_mode="text",
        turn_user_message_id=user_message.id,
    )
    assert canonical_prompt is not None

    with pytest.raises(ValueError, match="Confirmation prompt is missing"):
        assistant_guards.finalize_confirmation_turn(
            conversation,
            user_message,
            hidden_reply,
            reference,
            confirmation_prompt=canonical_prompt,
        )

    pending = assistant_guards.load_conversation_state(conversation)[
        "pending_confirmation"
    ]
    assert "prompted_at_assistant_message_id" not in pending


def test_stale_turn_cannot_replace_newer_confirmation_proposal(
    db,
    assistant_user,
):
    user, organization = assistant_user
    conversation = AssistantConversation(
        title="Turnos desordenados",
        status="active",
        channel="web",
        created_by=user,
    )
    slower_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Prepara la propuesta antigua",
    )
    newer_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Prepara la propuesta nueva",
    )
    db.add_all([conversation, slower_message, newer_message])
    db.commit()

    newer_result = assistant_guards.check_tool_confirmation(
        db,
        conversation,
        newer_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Propuesta nueva",
            "problem": "Problema nuevo",
        },
    )
    stale_result = assistant_guards.check_tool_confirmation(
        db,
        conversation,
        slower_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Propuesta antigua",
            "problem": "Problema antiguo",
        },
    )

    assert isinstance(newer_result, assistant_guards.ConfirmationToolResult)
    assert newer_result.status == "required"
    assert isinstance(stale_result, assistant_guards.ConfirmationToolResult)
    assert stale_result.status == "stale"
    pending = assistant_guards.load_conversation_state(conversation)[
        "pending_confirmation"
    ]
    assert pending["input"]["title"] == "Propuesta nueva"
    assert pending["proposed_at_user_message_id"] == newer_message.id


def test_confirmation_claim_cannot_be_rebound_by_slower_turn(
    db,
    assistant_user,
):
    user, organization = assistant_user
    conversation = AssistantConversation(
        title="Confirmación reclamada",
        status="active",
        channel="web",
        created_by=user,
    )
    proposed_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Prepara una necesidad",
    )
    db.add_all([conversation, proposed_message])
    db.flush()
    reference = assistant_guards.record_pending_confirmation(
        conversation,
        proposed_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Portal ciudadano",
            "problem": "El alta se hace por correo.",
        },
    )

    slower_user_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Explícame la prioridad",
    )
    db.add(slower_user_message)
    db.flush()
    canonical_prompt = assistant_guards.build_confirmation_prompt(
        conversation,
        reference,
        input_mode="text",
        turn_user_message_id=proposed_message.id,
    )
    assert canonical_prompt is not None
    prompt_message = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content=canonical_prompt,
    )
    db.add(prompt_message)
    db.flush()
    assistant_guards.finalize_confirmation_turn(
        conversation,
        proposed_message,
        prompt_message,
        reference,
        confirmation_prompt=canonical_prompt,
    )

    confirmation_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content="Sí, créalo",
    )
    db.add(confirmation_message)
    db.flush()
    claimed_reference = assistant_guards.process_pending_confirmation_response(
        db,
        conversation,
        confirmation_message,
    )
    assert claimed_reference == reference
    db.commit()

    assert (
        assistant_guards.build_confirmation_prompt(
            conversation,
            reference,
            input_mode="text",
            turn_user_message_id=slower_user_message.id,
        )
        is None
    )
    competing_result = assistant_guards.check_tool_confirmation(
        db,
        conversation,
        slower_user_message,
        "create_requirement",
        {
            "organization_id": organization.id,
            "title": "Propuesta competidora",
            "problem": "No debe sustituir la propuesta confirmada.",
        },
    )
    assert isinstance(competing_result, assistant_guards.ConfirmationToolResult)
    assert competing_result.status == "stale"
    late_reply = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content="Respuesta tardía sin propuesta confirmable.",
    )
    db.add(late_reply)
    db.flush()
    assistant_guards.finalize_confirmation_turn(
        conversation,
        slower_user_message,
        late_reply,
        reference,
        confirmation_prompt=None,
    )

    pending = assistant_guards.load_conversation_state(conversation)[
        "pending_confirmation"
    ]
    assert pending["prompted_at_assistant_message_id"] == prompt_message.id
    assert (
        pending["response_prompted_at_assistant_message_id"]
        == prompt_message.id
    )
    assert pending["response_user_message_id"] == confirmation_message.id


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
        prompt_message = AssistantMessage(
            conversation=conversation,
            role="assistant",
            content="Confirma el borrador.",
        )
        confirmation_message = AssistantMessage(
            conversation=conversation,
            role="user",
            content="Sí, créalo",
        )
        seed_db.add_all(
            [
                user,
                conversation,
                proposed_message,
                prompt_message,
                confirmation_message,
            ]
        )
        seed_db.flush()
        confirmation_reference = assistant_guards.record_pending_confirmation(
            conversation,
            proposed_message,
            "create_requirement",
            tool_input,
        )
        confirmation_prompt = assistant_guards.build_confirmation_prompt(
            conversation,
            confirmation_reference,
            input_mode="text",
            turn_user_message_id=proposed_message.id,
        )
        assert confirmation_prompt is not None
        prompt_message.content = confirmation_prompt
        assistant_guards.finalize_confirmation_turn(
            conversation,
            proposed_message,
            prompt_message,
            confirmation_reference,
            confirmation_prompt=confirmation_prompt,
        )
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
            barrier.wait(timeout=15)
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
            assert "pending_confirmation" not in state
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


def test_sse_unexpected_exception_emits_terminal_error_without_leaking_detail(
    client,
    assistant_user,
    use_gateway,
    monkeypatch,
    caplog,
):
    user, _ = assistant_user
    use_gateway(FakeGateway([]))
    conversation = client.post(
        "/assistant/conversations",
        json={},
        headers=headers_for(user),
    ).json()

    def fail_after_start(*args, **kwargs):
        yield assistant_turn.TurnEvent(
            "message_start",
            {"conversation_id": conversation["id"], "user_message_id": 999},
        )
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(
        "app.assistant.routes.run_agent_turn_events",
        fail_after_start,
    )

    with client.stream(
        "POST",
        f"/assistant/conversations/{conversation['id']}/messages/stream",
        json={"content": "Hola"},
        headers=headers_for(user),
    ) as response:
        body = "".join(response.iter_text())
        events = parse_sse_events(body)

    assert response.status_code == 200
    assert events == [
        (
            "message_start",
            {"conversation_id": conversation["id"], "user_message_id": 999},
        ),
        ("error", {"detail": "Assistant request failed"}),
    ]
    assert "sensitive internal detail" not in body
    assert "Unexpected assistant stream failure" in caplog.text


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
