import io
import json
import logging
from http import client as http_client
from types import SimpleNamespace
from urllib import error as urlerror

import pytest
from pydantic import ValidationError

from app.assistant import gateway as gateway_module
from app.assistant.gateway import (
    AIGateway,
    AssistantTimeoutError,
    AssistantUnavailableError,
    _RejectOpenAIRedirects,
    _from_openai_responses_response,
)
from app.assistant.safety import build_assistant_safety_identifier
from app.core.config import Settings, settings


class FakeJSONHTTPResponse:
    def __init__(self, body: dict | bytes):
        self.body = (
            json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class FakeSSEHTTPResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.position = 0
        self.read_timeouts: list[float] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read1(self, size: int) -> bytes:
        chunk = self.body[self.position : self.position + size]
        self.position += len(chunk)
        return chunk

    def set_read_timeout(self, timeout: float) -> None:
        self.read_timeouts.append(timeout)


def response_payload(
    output: list[dict],
    *,
    status: str = "completed",
    incomplete_reason: str | None = None,
) -> dict:
    return {
        "id": "resp_test",
        "status": status,
        "error": None,
        "incomplete_details": (
            {"reason": incomplete_reason} if incomplete_reason else None
        ),
        "model": "gpt-5.6-sol",
        "output": output,
        "usage": {"input_tokens": 13, "output_tokens": 7},
    }


def output_text(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
        ],
    }


def function_call(
    call_id: str,
    name: str,
    arguments: dict | str,
    *,
    item_id: str,
) -> dict:
    return {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": (
            json.dumps(arguments, separators=(",", ":"))
            if isinstance(arguments, dict)
            else arguments
        ),
    }


def sse_event(event_type: str, payload: dict) -> bytes:
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def drain_stream(stream) -> tuple[list[str], object]:
    deltas: list[str] = []
    while True:
        try:
            deltas.append(next(stream).text)
        except StopIteration as stop:
            return deltas, stop.value


@pytest.fixture()
def openai_runtime(monkeypatch):
    monkeypatch.setattr(settings, "assistant_runtime", "openai_responses")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-secret")
    monkeypatch.setattr(
        settings,
        "openai_responses_base_url",
        "https://api.openai.com/v1",
    )
    monkeypatch.setattr(settings, "openai_responses_model", "gpt-5.6")
    monkeypatch.setattr(settings, "openai_responses_reasoning_effort", "medium")
    monkeypatch.setattr(settings, "openai_responses_max_output_tokens", 25000)
    monkeypatch.setattr(settings, "assistant_gateway_timeout_seconds", 30.0)


def install_fake_responses(monkeypatch, responses: list[object]):
    pending = list(responses)
    captured: list[tuple[object, float]] = []

    def fake_urlopen(request, *, timeout):
        captured.append((request, timeout))
        if not pending:
            raise AssertionError("No fake OpenAI response remains")
        response = pending.pop(0)
        if isinstance(response, Exception):
            raise response
        if hasattr(response, "__enter__") and (
            hasattr(response, "read") or hasattr(response, "read1")
        ):
            return response
        if isinstance(response, bytes):
            return FakeJSONHTTPResponse(response)
        return FakeJSONHTTPResponse(response)

    monkeypatch.setattr(
        gateway_module,
        "_openai_responses_urlopen",
        fake_urlopen,
    )
    return captured


def test_settings_accept_openai_responses_and_official_regional_base():
    configured = Settings(
        _env_file=None,
        assistant_runtime=" OPENAI_RESPONSES ",
        openai_responses_base_url="https://eu.api.openai.com/v1/",
    )

    assert configured.assistant_runtime == "openai_responses"
    assert configured.openai_responses_base_url == "https://eu.api.openai.com/v1"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.openai.com/v1",
        "https://api.openai.com.evil.test/v1",
        "https://api.openai.com:443/v1",
        "https://user@api.openai.com/v1",
        "https://api.openai.com/v1/responses",
        "https://api.openai.com/v1?target=evil",
    ],
)
def test_settings_reject_noncanonical_openai_base_urls(base_url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openai_responses_base_url=base_url)


def test_settings_reject_models_outside_the_supported_responses_family():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, openai_responses_model="gpt-4.1")


@pytest.mark.parametrize("max_chars", [0, 20001])
def test_settings_reject_speech_limits_outside_request_contract(max_chars):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, speech_synthesis_max_chars=max_chars)


def test_openai_gateway_enabled_and_model_follow_runtime(openai_runtime, monkeypatch):
    gateway = AIGateway()

    assert gateway.enabled is True
    assert gateway.model == "gpt-5.6"
    assert gateway.runtime_healthy is None

    monkeypatch.setattr(settings, "openai_api_key", None)
    assert gateway.enabled is False


def test_sync_request_uses_responses_contract_and_parses_text(
    openai_runtime,
    monkeypatch,
):
    payload = response_payload(
        [
            {
                **output_text("Primera parte."),
                "content": [
                    {"type": "output_text", "text": "Primera parte.", "annotations": []},
                    {"type": "output_text", "text": "Segunda parte.", "annotations": []},
                ],
            }
        ]
    )
    captured = install_fake_responses(monkeypatch, [payload])

    completion = AIGateway().complete(
        system="Instrucciones privadas",
        messages=[{"role": "user", "content": "Hola"}],
        tools=[
            {
                "name": "list_requirements",
                "description": "Lista necesidades visibles",
                "input_schema": {
                    "type": "object",
                    "properties": {"organization_id": {"type": "integer"}},
                },
            }
        ],
        timeout_seconds=2.5,
        safety_identifier="a" * 64,
    )

    request, timeout = captured[0]
    request_payload = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.get_header("Authorization") == "Bearer sk-test-secret"
    assert request.get_header("Accept") == "application/json"
    assert timeout == pytest.approx(2.5)
    assert request_payload == {
        "model": "gpt-5.6",
        "instructions": "Instrucciones privadas",
        "input": [{"role": "user", "content": "Hola"}],
        "max_output_tokens": 25000,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "medium"},
        "parallel_tool_calls": True,
        "truncation": "auto",
        "tools": [
            {
                "type": "function",
                "name": "list_requirements",
                "description": "Lista necesidades visibles",
                "parameters": {
                    "type": "object",
                    "properties": {"organization_id": {"type": "integer"}},
                },
                "strict": False,
            }
        ],
        "tool_choice": "auto",
        "safety_identifier": "a" * 64,
    }
    assert completion.model == "gpt-5.6-sol"
    assert completion.stop_reason == "end_turn"
    assert [block.text for block in completion.content] == [
        "Primera parte.",
        "Segunda parte.",
    ]
    assert completion.usage.input_tokens == 13
    assert completion.usage.output_tokens == 7


def test_tool_round_replays_reasoning_and_uses_call_id(
    openai_runtime,
    monkeypatch,
):
    reasoning = {
        "id": "rs_internal",
        "type": "reasoning",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    first_payload = response_payload(
        [
            reasoning,
            function_call(
                "call_public_1",
                "list_requirements",
                {"organization_id": 7},
                item_id="fc_internal_1",
            ),
            function_call(
                "call_public_2",
                "list_projects",
                {"organization_id": 7},
                item_id="fc_internal_2",
            ),
        ]
    )
    captured = install_fake_responses(
        monkeypatch,
        [first_payload, response_payload([output_text("Resultado final")])],
    )
    gateway = AIGateway()

    first = gateway.complete(
        system="system",
        messages=[{"role": "user", "content": "Consulta"}],
        tools=[],
        safety_identifier="b" * 64,
    )

    assert first.stop_reason == "tool_use"
    assert [block.id for block in first.content] == [
        "call_public_1",
        "call_public_2",
    ]
    assert [block.name for block in first.content] == [
        "list_requirements",
        "list_projects",
    ]

    second = gateway.complete(
        system="system",
        messages=[
            {"role": "user", "content": "Consulta"},
            {
                "role": "assistant",
                "content": first.content,
                "provider_state": first.provider_state,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_public_1",
                        "content": "resultado uno",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_public_2",
                        "content": "resultado dos",
                    },
                ],
            },
        ],
        tools=[],
        safety_identifier="b" * 64,
    )

    second_request = json.loads(captured[1][0].data.decode("utf-8"))
    assert second_request["input"] == [
        {"role": "user", "content": "Consulta"},
        reasoning,
        first_payload["output"][1],
        first_payload["output"][2],
        {
            "type": "function_call_output",
            "call_id": "call_public_1",
            "output": "resultado uno",
        },
        {
            "type": "function_call_output",
            "call_id": "call_public_2",
            "output": "resultado dos",
        },
    ]
    assert second.content[0].text == "Resultado final"


def test_historical_assistant_messages_keep_final_answer_phase(
    openai_runtime,
    monkeypatch,
):
    captured = install_fake_responses(
        monkeypatch,
        [response_payload([output_text("Nueva respuesta")])],
    )

    AIGateway().complete(
        system="system",
        messages=[
            {"role": "assistant", "content": "Respuesta anterior"},
            {"role": "user", "content": "Continua"},
        ],
        tools=[],
    )

    request_payload = json.loads(captured[0][0].data.decode("utf-8"))
    assert request_payload["input"] == [
        {
            "role": "assistant",
            "content": "Respuesta anterior",
            "phase": "final_answer",
        },
        {"role": "user", "content": "Continua"},
    ]


@pytest.mark.parametrize(
    "call",
    [
        function_call("call_1", "tool", "{bad", item_id="fc_1"),
        function_call("call_1", "tool", "[]", item_id="fc_1"),
        {**function_call("call_1", "tool", {}, item_id="fc_1"), "call_id": ""},
        {**function_call("call_1", "tool", {}, item_id="fc_1"), "name": ""},
    ],
)
def test_invalid_function_calls_fail_closed(call):
    with pytest.raises(ValueError):
        _from_openai_responses_response(response_payload([call]))


def test_incomplete_and_refusal_states_are_mapped():
    incomplete = _from_openai_responses_response(
        response_payload(
            [output_text("Respuesta parcial")],
            status="incomplete",
            incomplete_reason="max_output_tokens",
        )
    )
    refusal = _from_openai_responses_response(
        response_payload(
            [
                {
                    "id": "msg_refusal",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "refusal", "refusal": "No puedo ayudar."}
                    ],
                }
            ]
        )
    )

    assert incomplete.stop_reason == "pause_turn"
    assert refusal.stop_reason == "refusal"


def test_commentary_is_not_mistaken_for_a_final_answer():
    completion = _from_openai_responses_response(
        response_payload(
            [
                {
                    **output_text("Voy a comprobarlo."),
                    "phase": "commentary",
                }
            ]
        )
    )

    assert completion.stop_reason == "pause_turn"
    assert completion.content[0].text == "Voy a comprobarlo."


def test_reasoning_without_a_final_answer_continues_the_turn():
    completion = _from_openai_responses_response(
        response_payload(
            [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "encrypted_content": "encrypted",
                    "summary": [],
                }
            ]
        )
    )

    assert completion.stop_reason == "pause_turn"
    assert completion.content == []


@pytest.mark.parametrize("item_status", ["incomplete", "in_progress", None])
def test_incomplete_function_calls_never_become_tool_use(item_status):
    call = function_call(
        "call_1",
        "create_requirement",
        {"title": "No ejecutar"},
        item_id="fc_1",
    )
    call["status"] = item_status

    with pytest.raises(ValueError, match="incomplete function calls"):
        _from_openai_responses_response(response_payload([call]))


def test_incomplete_response_never_executes_even_completed_function_call():
    call = function_call(
        "call_1",
        "create_requirement",
        {"title": "No ejecutar"},
        item_id="fc_1",
    )

    with pytest.raises(ValueError, match="incomplete function calls"):
        _from_openai_responses_response(
            response_payload(
                [call],
                status="incomplete",
                incomplete_reason="max_output_tokens",
            )
        )


@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "in_progress"])
def test_nonterminal_or_failed_sync_states_are_rejected(status):
    with pytest.raises(ValueError):
        _from_openai_responses_response(
            {
                **response_payload([]),
                "status": status,
            }
        )


@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 503])
def test_sync_http_errors_are_normalized(
    openai_runtime,
    monkeypatch,
    status_code,
):
    error = urlerror.HTTPError(
        "https://api.openai.com/v1/responses",
        status_code,
        "private upstream message",
        {},
        io.BytesIO(b'{"error":{"message":"private body"}}'),
    )
    install_fake_responses(monkeypatch, [error])

    with pytest.raises(AssistantUnavailableError, match="request failed"):
        AIGateway().complete(system="secret prompt", messages=[], tools=[])


@pytest.mark.parametrize(
    "error",
    [TimeoutError("deadline"), urlerror.URLError(TimeoutError("socket"))],
)
def test_sync_timeouts_are_classified(openai_runtime, monkeypatch, error):
    install_fake_responses(monkeypatch, [error])

    with pytest.raises(AssistantTimeoutError):
        AIGateway().complete(system="system", messages=[], tools=[])


def test_sync_rejects_oversized_or_invalid_json(openai_runtime, monkeypatch):
    monkeypatch.setattr(gateway_module, "MAX_OPENAI_RESPONSES_BYTES", 16)
    captured = install_fake_responses(monkeypatch, [b"x" * 17, b"not-json"])
    gateway = AIGateway()

    with pytest.raises(AssistantUnavailableError, match="invalid response"):
        gateway.complete(system="system", messages=[], tools=[])
    with pytest.raises(AssistantUnavailableError, match="invalid response"):
        gateway.complete(system="system", messages=[], tools=[])

    assert len(captured) == 2


@pytest.mark.parametrize(
    "read_error",
    [
        http_client.IncompleteRead(b"partial"),
        http_client.RemoteDisconnected("closed"),
        ConnectionResetError("reset"),
    ],
)
def test_sync_read_failures_are_normalized(
    openai_runtime,
    monkeypatch,
    read_error,
):
    class BrokenResponse(FakeJSONHTTPResponse):
        def read(self, size: int = -1) -> bytes:
            raise read_error

    install_fake_responses(monkeypatch, [BrokenResponse(b"")])

    with pytest.raises(AssistantUnavailableError, match="connection failed"):
        AIGateway().complete(system="system", messages=[], tools=[])


def test_stream_emits_only_text_deltas_and_returns_terminal_completion(
    openai_runtime,
    monkeypatch,
):
    terminal = response_payload([output_text("Hola mundo")])
    body = b"".join(
        [
            sse_event(
                "response.created",
                {"type": "response.created", "response": {"status": "in_progress"}},
            ),
            sse_event(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "Hola "},
            ),
            sse_event(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "delta": '{"private":',
                },
            ),
            sse_event(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "mundo"},
            ),
            sse_event(
                "response.completed",
                {"type": "response.completed", "response": terminal},
            ),
        ]
    )
    fake_response = FakeSSEHTTPResponse(body)
    captured = install_fake_responses(monkeypatch, [fake_response])

    deltas, completion = drain_stream(
        AIGateway().complete_stream(
            system="system",
            messages=[{"role": "user", "content": "saluda"}],
            tools=[],
            timeout_seconds=5.0,
        )
    )

    assert deltas == ["Hola ", "mundo"]
    assert completion.content[0].text == "Hola mundo"
    assert captured[0][0].get_header("Accept") == "text/event-stream"
    assert json.loads(captured[0][0].data.decode("utf-8"))["stream"] is True
    assert fake_response.read_timeouts


@pytest.mark.parametrize(
    "body",
    [
        sse_event(
            "error",
            {
                "type": "error",
                "error": {"message": "private stream error"},
            },
        ),
        sse_event(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "parcial"},
        ),
        b"event: response.completed\ndata: not-json\n\n",
    ],
)
def test_stream_failures_end_without_hanging(openai_runtime, monkeypatch, body):
    install_fake_responses(monkeypatch, [FakeSSEHTTPResponse(body)])

    with pytest.raises(AssistantUnavailableError):
        drain_stream(
            AIGateway().complete_stream(system="system", messages=[], tools=[])
        )


def test_stream_enforces_absolute_deadline_between_chunks(
    openai_runtime,
    monkeypatch,
):
    class SlowByteStream(FakeSSEHTTPResponse):
        def read1(self, size: int) -> bytes:
            return super().read1(1)

    fake_response = SlowByteStream(
        sse_event(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "parcial"},
        )
    )
    install_fake_responses(monkeypatch, [fake_response])
    clock = iter([0.0, 0.2, 0.4, 1.0])
    monkeypatch.setattr(gateway_module, "monotonic", lambda: next(clock))

    with pytest.raises(AssistantTimeoutError):
        drain_stream(
            AIGateway().complete_stream(
                system="system",
                messages=[],
                tools=[],
                timeout_seconds=1.0,
            )
        )

    assert fake_response.read_timeouts == [pytest.approx(0.8)]


def test_stream_rejects_oversized_body(openai_runtime, monkeypatch):
    monkeypatch.setattr(gateway_module, "MAX_OPENAI_RESPONSES_STREAM_BYTES", 8)
    install_fake_responses(
        monkeypatch,
        [FakeSSEHTTPResponse(b"data: 123")],
    )

    with pytest.raises(AssistantUnavailableError, match="invalid response"):
        drain_stream(
            AIGateway().complete_stream(system="system", messages=[], tools=[])
        )


@pytest.mark.parametrize(
    "read_error",
    [
        http_client.IncompleteRead(b"partial"),
        http_client.RemoteDisconnected("closed"),
        ConnectionResetError("reset"),
    ],
)
def test_stream_read_failures_are_normalized(
    openai_runtime,
    monkeypatch,
    read_error,
):
    class BrokenStream(FakeSSEHTTPResponse):
        def read1(self, size: int) -> bytes:
            raise read_error

    install_fake_responses(monkeypatch, [BrokenStream(b"")])

    with pytest.raises(AssistantUnavailableError, match="connection failed"):
        drain_stream(
            AIGateway().complete_stream(system="system", messages=[], tools=[])
        )


def test_openai_redirect_handler_rejects_redirects():
    handler = _RejectOpenAIRedirects()

    assert (
        handler.redirect_request(
            SimpleNamespace(),
            None,
            302,
            "Found",
            {},
            "https://evil.test/steal",
        )
        is None
    )


def test_errors_and_logs_do_not_expose_payloads(
    openai_runtime,
    monkeypatch,
    caplog,
):
    sentinel = "DO-NOT-LOG-THIS-SENTINEL"
    error = urlerror.HTTPError(
        "https://api.openai.com/v1/responses",
        500,
        sentinel,
        {},
        io.BytesIO(sentinel.encode("utf-8")),
    )
    install_fake_responses(monkeypatch, [error])
    caplog.set_level(logging.INFO)

    with pytest.raises(AssistantUnavailableError) as caught:
        AIGateway().complete(
            system=sentinel,
            messages=[{"role": "user", "content": sentinel}],
            tools=[],
        )

    assert sentinel not in caplog.text
    assert sentinel not in str(caught.value)
    assert "status=500" in caplog.text


def test_safety_identifier_is_stable_secret_keyed_and_shared(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "test-safety-secret")

    first = build_assistant_safety_identifier(123)
    repeated = build_assistant_safety_identifier(123)
    other = build_assistant_safety_identifier(124)
    monkeypatch.setattr(settings, "secret_key", "another-safety-secret")
    with_other_secret = build_assistant_safety_identifier(123)

    assert first == repeated
    assert first != other
    assert first != with_other_secret
    assert len(first) == 64
