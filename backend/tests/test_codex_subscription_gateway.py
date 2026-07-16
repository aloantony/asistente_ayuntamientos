import io
import stat
import sys
from collections import deque
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.assistant.codex_app_server import (
    CodexAppServerClient,
    CodexProtocolError,
    CodexRuntimeCompletion,
    CodexRuntimeToolCall,
    CodexSubscriptionRuntime,
    CodexTimeoutError,
)
from app.assistant.gateway import (
    AIGateway,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from app.core.config import Settings
from app.core.config import settings


class ScriptedCodexClient:
    def __init__(self, events: list[dict]) -> None:
        self.events = deque(events)
        self.requests: list[tuple[str, dict]] = []
        self.notifications: list[tuple[str, dict]] = []
        self.responses: list[tuple[object, dict]] = []
        self.closed = False

    def request(self, method: str, params: dict, *, timeout: float) -> dict:
        assert timeout > 0
        self.requests.append((method, params))
        if method == "initialize":
            return {"userAgent": "codex-test"}
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "planType": "plus"},
                "requiresOpenaiAuth": True,
            }
        if method == "thread/start":
            return {
                "thread": {"id": "thread-test"},
                "model": params.get("model") or "subscription-default",
            }
        if method == "turn/start":
            return {"turn": {"id": "turn-test", "status": "inProgress"}}
        raise AssertionError(f"unexpected request: {method}")

    def notify(self, method: str, params: dict | None = None) -> None:
        self.notifications.append((method, params or {}))

    def respond(self, request_id: object, result: dict) -> None:
        self.responses.append((request_id, result))
        self.events.extend(
            [
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "item": {
                            "id": "message-final",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "",
                        },
                    },
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "itemId": "message-final",
                        "delta": "Resultado comprobado.",
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "item": {
                            "id": "message-final",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Resultado comprobado.",
                        },
                    },
                },
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "tokenUsage": {
                            "total": {"inputTokens": 42, "outputTokens": 7}
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turn": {
                            "id": "turn-test",
                            "status": "completed",
                            "error": None,
                        },
                    },
                },
            ]
        )

    def take_event(self, *, timeout: float) -> dict | None:
        assert timeout >= 0
        if self.events:
            return self.events.popleft()
        return None

    def close(self) -> None:
        self.closed = True


def collect_runtime_stream(stream):
    deltas = []
    while True:
        try:
            deltas.append(next(stream))
        except StopIteration as stop:
            return deltas, stop.value


def runtime_with_client(tmp_path: Path, client: ScriptedCodexClient):
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    return CodexSubscriptionRuntime(
        command="codex",
        codex_home=str(home),
        model="gpt-test",
        reasoning_effort="medium",
        session_ttl_seconds=30,
        max_sessions=2,
        client_factory=lambda **_kwargs: client,
    )


def test_codex_subscription_is_rejected_outside_development(tmp_path):
    home = tmp_path / "codex-home"
    for environment in ("production", "staging", "test", "Development", " development "):
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                environment=environment,
                assistant_runtime="codex_subscription",
                codex_subscription_home=str(home),
            )


def test_personal_codex_home_is_rejected():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            assistant_runtime="codex_subscription",
            codex_subscription_home="~/.codex",
        )


def test_protocol_pauses_for_backend_tool_and_resumes_same_turn(tmp_path):
    client = ScriptedCodexClient(
        [
            {
                "id": 73,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "callId": "call-web-1",
                    "namespace": None,
                    "tool": "municipal_web_search",
                    "arguments": {"query": "ordenanza Burgos"},
                },
            }
        ]
    )
    runtime = runtime_with_client(tmp_path, client)
    tools = [
        {
            "name": "web_search",
            "description": "Busca información web.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]

    _, first = collect_runtime_stream(
        runtime.complete_stream(
            system="Asistente municipal",
            messages=[{"role": "user", "content": "Busca la ordenanza"}],
            tools=tools,
            timeout=1,
            safety_identifier="user-hash",
        )
    )

    assert first.stop_reason == "tool_use"
    assert [(call.id, call.name, call.arguments) for call in first.tool_calls] == [
        ("call-web-1", "web_search", {"query": "ordenanza Burgos"})
    ]
    assert first.state_handle
    assert client.responses == []
    assert [method for method, _ in client.requests] == [
        "initialize",
        "account/read",
        "thread/start",
        "turn/start",
    ]
    initialize = client.requests[0][1]
    assert initialize["capabilities"]["experimentalApi"] is True
    thread_start = client.requests[2][1]
    assert thread_start["ephemeral"] is True
    assert thread_start["approvalPolicy"] == "never"
    assert thread_start["sandbox"] == "read-only"
    assert thread_start["dynamicTools"][0]["name"] == "municipal_web_search"
    assert thread_start["dynamicTools"][0]["inputSchema"] == tools[0][
        "input_schema"
    ]

    resumed_messages = [
        {"role": "user", "content": "Busca la ordenanza"},
        {
            "role": "assistant",
            "content": [],
            "provider_state": (
                {"type": "codex_subscription_session", "handle": first.state_handle},
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-web-1",
                    "content": "Fuente oficial: https://burgos.example",
                    "is_error": False,
                }
            ],
        },
    ]
    deltas, final = collect_runtime_stream(
        runtime.complete_stream(
            system="Asistente municipal",
            messages=resumed_messages,
            tools=tools,
            timeout=1,
            safety_identifier="user-hash",
        )
    )

    assert deltas == ["Resultado comprobado."]
    assert final.stop_reason == "end_turn"
    assert final.text == "Resultado comprobado."
    assert final.input_tokens == 42
    assert final.output_tokens == 7
    assert client.responses == [
        (
            73,
            {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "Fuente oficial: https://burgos.example",
                    }
                ],
                "success": True,
            },
        )
    ]
    assert client.closed is True


@pytest.mark.parametrize("account", [None, {"type": "apiKey"}, {"type": "amazonBedrock"}])
def test_only_chatgpt_auth_is_accepted(tmp_path, account):
    client = ScriptedCodexClient([])
    original_request = client.request

    def request(method, params, *, timeout):
        if method == "account/read":
            client.requests.append((method, params))
            return {"account": account, "requiresOpenaiAuth": True}
        return original_request(method, params, timeout=timeout)

    client.request = request
    runtime = runtime_with_client(tmp_path, client)

    with pytest.raises(CodexProtocolError, match="ChatGPT authentication"):
        collect_runtime_stream(
            runtime.complete_stream(
                system="system",
                messages=[{"role": "user", "content": "hola"}],
                tools=[],
                timeout=1,
                safety_identifier="user-hash",
            )
        )
    assert client.closed is True


@pytest.mark.parametrize(
    "dangerous_type",
    [
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "webSearch",
        "imageView",
        "imageGeneration",
    ],
)
def test_builtin_codex_tools_fail_closed(tmp_path, dangerous_type):
    client = ScriptedCodexClient(
        [
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "item": {"id": "danger", "type": dangerous_type},
                },
            }
        ]
    )
    runtime = runtime_with_client(tmp_path, client)

    with pytest.raises(CodexProtocolError, match="built-in Codex tool"):
        collect_runtime_stream(
            runtime.complete_stream(
                system="system",
                messages=[{"role": "user", "content": "hola"}],
                tools=[],
                timeout=1,
                safety_identifier="user-hash",
            )
        )
    assert client.closed is True


class EmptyProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.pid = 999_999
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


def test_spawn_is_hardened_and_environment_is_allowlisted(monkeypatch, tmp_path):
    captured = {}
    process = EmptyProcess()

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("app.assistant.codex_app_server.subprocess.Popen", fake_popen)
    monkeypatch.setenv("DATABASE_URL", "secret-database")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "secret-brave")
    monkeypatch.setenv("ARBITRARY_SECRET", "secret-value")
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()

    client = CodexAppServerClient.spawn(
        command=sys.executable,
        codex_home=str(home),
        workspace=str(workspace),
    )
    client.close()

    argv = captured["argv"]
    assert argv[:3] == [sys.executable, "app-server", "--stdio"]
    assert "--strict-config" in argv
    assert 'forced_login_method="chatgpt"' in argv
    assert 'history.persistence="none"' in argv
    assert 'web_search="disabled"' in argv
    for feature in (
        "shell_tool",
        "unified_exec",
        "apps",
        "plugins",
        "multi_agent",
        "browser_use",
        "computer_use",
        "image_generation",
    ):
        assert ["--disable", feature] == argv[
            argv.index(feature) - 1 : argv.index(feature) + 1
        ]
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == str(workspace)
    assert kwargs["env"]["CODEX_HOME"] == str(home)
    assert kwargs["env"]["RUST_LOG"] == "warn"
    assert "DATABASE_URL" not in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "BRAVE_SEARCH_API_KEY" not in kwargs["env"]
    assert "ARBITRARY_SECRET" not in kwargs["env"]


def test_codex_home_must_be_private(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    assert stat.S_IMODE(home.stat().st_mode) == 0o755
    with pytest.raises(CodexProtocolError, match="permissions 0700"):
        CodexSubscriptionRuntime(
            command="codex",
            codex_home=str(home),
            model="",
            reasoning_effort="medium",
            session_ttl_seconds=30,
            max_sessions=1,
            client_factory=lambda **_kwargs: ScriptedCodexClient([]),
        ).complete(
            system="system",
            messages=[{"role": "user", "content": "hola"}],
            tools=[],
            timeout=1,
            safety_identifier="user-hash",
        )


class FakeCodexRuntime:
    configured = True

    def __init__(self, completion=None, error=None):
        self.completion = completion
        self.error = error
        self.calls = []
        self.discarded = []

    def complete_stream(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        yield "Respuesta "
        yield "en curso."
        return self.completion

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.completion

    def healthy(self, *, timeout):
        return timeout > 0

    def discard_provider_state(self, messages):
        self.discarded.append(messages)


def test_gateway_projects_codex_stream_and_opaque_tool_state(monkeypatch):
    runtime = FakeCodexRuntime(
        CodexRuntimeCompletion(
            model="gpt-subscription-test",
            stop_reason="tool_use",
            tool_calls=(
                CodexRuntimeToolCall(
                    id="call-1",
                    name="web_search",
                    arguments={"query": "Burgos"},
                ),
            ),
            input_tokens=10,
            output_tokens=3,
            state_handle="a" * 32,
        )
    )
    gateway = AIGateway()
    gateway._codex_subscription_runtime = runtime
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "assistant_runtime", "codex_subscription")

    deltas, completion = collect_runtime_stream(
        gateway.complete_stream(
            system="system",
            messages=[{"role": "user", "content": "hola"}],
            tools=[],
            timeout_seconds=1,
            safety_identifier="user-hash",
        )
    )

    assert [delta.text for delta in deltas] == ["Respuesta ", "en curso."]
    assert completion.stop_reason == "tool_use"
    assert completion.model == "gpt-subscription-test"
    assert completion.usage.input_tokens == 10
    assert completion.usage.output_tokens == 3
    assert [block.type for block in completion.content] == ["tool_use"]
    assert completion.content[0].name == "web_search"
    assert completion.provider_state == (
        {"type": "codex_subscription_session", "handle": "a" * 32},
    )


def test_gateway_maps_codex_timeout_without_leaking_details(monkeypatch):
    gateway = AIGateway()
    gateway._codex_subscription_runtime = FakeCodexRuntime(
        error=CodexTimeoutError("sensitive prompt sentinel")
    )
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "assistant_runtime", "codex_subscription")

    with pytest.raises(AssistantTimeoutError, match="timed out") as caught:
        collect_runtime_stream(
            gateway.complete_stream(
                system="secret system",
                messages=[{"role": "user", "content": "secret prompt"}],
                tools=[],
                timeout_seconds=1,
                safety_identifier="user-hash",
            )
        )
    assert "sentinel" not in str(caught.value)


def test_production_never_initializes_codex_runtime(monkeypatch):
    gateway = AIGateway()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "assistant_runtime", "codex_subscription")
    monkeypatch.setattr(
        gateway,
        "_get_codex_subscription_runtime",
        lambda: pytest.fail("production attempted to initialize Codex"),
    )

    assert gateway.enabled is False
    with pytest.raises(AssistantUnavailableError, match="not configured"):
        gateway.complete(
            system="system",
            messages=[],
            tools=[],
            timeout_seconds=1,
            safety_identifier="user-hash",
        )
