import io
import queue
import stat
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.assistant import codex_app_server
from app.assistant.codex_app_server import (
    CodexAppServerClient,
    CodexCapacityError,
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

    def notify(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float = 1,
    ) -> None:
        assert timeout > 0
        self.notifications.append((method, params or {}))

    def respond(
        self,
        request_id: object,
        result: dict,
        *,
        timeout: float = 1,
    ) -> None:
        assert timeout > 0
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
                codex_subscription_enabled=True,
                codex_subscription_real_data_allowed=True,
                codex_subscription_home=str(home),
            )


def test_settings_accept_codex_subscription_only_in_development(tmp_path):
    home = tmp_path / "codex-home"
    configured = Settings(
        _env_file=None,
        environment="development",
        assistant_runtime=" CODEX_SUBSCRIPTION ",
        codex_subscription_enabled=True,
        codex_subscription_real_data_allowed=True,
        codex_subscription_home=str(home),
        codex_subscription_model=" gpt-test ",
    )

    assert configured.assistant_runtime == "codex_subscription"
    assert configured.codex_subscription_home == str(home.resolve())
    assert configured.codex_subscription_model == "gpt-test"


@pytest.mark.parametrize(
    ("enabled", "real_data_allowed"),
    [(False, False), (False, True), (True, False)],
)
def test_codex_subscription_requires_explicit_opt_ins(
    tmp_path,
    enabled,
    real_data_allowed,
):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="development",
            assistant_runtime="codex_subscription",
            codex_subscription_enabled=enabled,
            codex_subscription_real_data_allowed=real_data_allowed,
            codex_subscription_home=str(tmp_path / "codex-home"),
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
        "computerToolCall",
        "browserToolCall",
        "appToolCall",
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


def test_text_delta_without_started_item_fails_closed(tmp_path):
    client = ScriptedCodexClient(
        [
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "itemId": "unknown-message",
                    "delta": "contenido no clasificado",
                },
            }
        ]
    )
    runtime = runtime_with_client(tmp_path, client)

    with pytest.raises(CodexProtocolError, match="before starting"):
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


def test_assistant_output_has_a_cumulative_size_budget(monkeypatch, tmp_path):
    client = ScriptedCodexClient(
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
                    "delta": "12345",
                },
            },
        ]
    )
    runtime = runtime_with_client(tmp_path, client)
    monkeypatch.setattr(codex_app_server, "MAX_ASSISTANT_OUTPUT_BYTES", 4)

    with pytest.raises(CodexProtocolError, match="size limit"):
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
        self.stdin = tempfile.TemporaryFile()
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
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-secret@example")
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
    assert "HTTPS_PROXY" not in kwargs["env"]


def test_app_server_write_obeys_deadline(monkeypatch):
    client = object.__new__(CodexAppServerClient)
    client._closed = False
    client._send_lock = threading.Lock()
    client._process = SimpleNamespace(
        stdin=SimpleNamespace(fileno=lambda: 12345),
    )

    def blocked_write(_file_descriptor, _payload):
        raise BlockingIOError

    monkeypatch.setattr(codex_app_server.os, "write", blocked_write)
    monkeypatch.setattr(
        codex_app_server.select,
        "select",
        lambda *_args: ([], [], []),
    )

    with pytest.raises(CodexTimeoutError, match="write timed out"):
        client._send({"method": "test"}, timeout=0.01)


def test_event_queue_applies_backpressure_without_dropping_events():
    client = object.__new__(CodexAppServerClient)
    client._closed = False
    client._reader_failed = threading.Event()
    client._transport_closed = threading.Event()
    client._events = queue.Queue(maxsize=1)
    first = {"method": "first", "params": {}}
    second = {"method": "second", "params": {}}
    client._events.put_nowait(first)

    writer = threading.Thread(target=client._dispatch, args=(second,))
    writer.start()
    writer.join(timeout=0.02)
    assert writer.is_alive()

    assert client.take_event(timeout=0) == first
    writer.join(timeout=1)
    assert not writer.is_alive()
    assert client.take_event(timeout=0) == second


def test_gateway_initializes_one_codex_runtime_under_concurrency(monkeypatch):
    created = []

    class SlowRuntime:
        def __init__(self, **_kwargs):
            time.sleep(0.05)
            created.append(self)

    monkeypatch.setattr(
        codex_app_server,
        "CodexSubscriptionRuntime",
        SlowRuntime,
    )
    gateway = AIGateway()

    with ThreadPoolExecutor(max_workers=2) as executor:
        runtimes = list(
            executor.map(
                lambda _index: gateway._get_codex_subscription_runtime(),
                range(2),
            )
        )

    assert len(created) == 1
    assert runtimes[0] is runtimes[1]


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


def test_default_runtime_is_not_configured_without_private_auth_file(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(
        "app.assistant.codex_app_server.shutil.which",
        lambda _command: sys.executable,
    )
    runtime = CodexSubscriptionRuntime(
        command="codex",
        codex_home=str(home),
        model="",
        reasoning_effort="medium",
        session_ttl_seconds=30,
        max_sessions=1,
    )

    assert runtime.configured is False
    auth_file = home / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o600)
    assert runtime.configured is True
    auth_file.chmod(0o644)
    assert runtime.configured is False


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "Logged in using ChatGPT\n", "", True),
        (
            0,
            "",
            "WARNING: could not create PATH aliases\nLogged in using ChatGPT\n",
            True,
        ),
        (0, "", "Logged in using an API key\n", False),
        (
            0,
            "Logged in using an API key\n",
            "WARNING: ChatGPT login is also supported\n",
            False,
        ),
        (1, "", "Not logged in\n", False),
    ],
)
def test_runtime_health_accepts_chatgpt_status_from_either_stream(
    monkeypatch,
    tmp_path,
    returncode,
    stdout,
    stderr,
    expected,
):
    client = ScriptedCodexClient([])
    runtime = runtime_with_client(tmp_path, client)
    monkeypatch.setattr(
        codex_app_server.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )

    assert runtime.healthy(timeout=1) is expected


def test_runtime_generator_close_cleans_pending_process(tmp_path):
    client = ScriptedCodexClient(
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
                    "delta": "parcial",
                },
            },
        ]
    )
    runtime = runtime_with_client(tmp_path, client)
    stream = runtime.complete_stream(
        system="system",
        messages=[{"role": "user", "content": "hola"}],
        tools=[],
        timeout=1,
        safety_identifier="user-hash",
    )

    assert next(stream) == "parcial"
    stream.close()

    assert client.closed is True
    assert runtime._sessions == {}


def test_paused_session_ttl_cannot_expire_while_session_is_in_use(tmp_path):
    client = ScriptedCodexClient(
        [
            {
                "id": 73,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "callId": "call-1",
                    "namespace": None,
                    "tool": "municipal_web_search",
                    "arguments": {"query": "Burgos"},
                },
            }
        ]
    )
    runtime = runtime_with_client(tmp_path, client)
    _, completion = collect_runtime_stream(
        runtime.complete_stream(
            system="system",
            messages=[{"role": "user", "content": "busca"}],
            tools=[
                {
                    "name": "web_search",
                    "description": "Busca.",
                    "input_schema": {"type": "object"},
                }
            ],
            timeout=1,
            safety_identifier="owner",
        )
    )
    session = runtime._sessions[completion.state_handle]
    stale_generation = session.expiry_generation
    assert session.in_use is False
    assert session.timer is not None

    acquired = runtime._acquire_session(completion.state_handle, "owner")
    assert acquired.in_use is True
    assert acquired.timer is None
    runtime._expire_session(completion.state_handle, stale_generation)
    assert runtime._sessions[completion.state_handle] is acquired

    runtime._release_session(acquired)
    runtime.close()
    assert client.closed is True


def test_session_handle_is_bound_to_safety_identifier(tmp_path):
    client = ScriptedCodexClient(
        [
            {
                "id": 73,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "callId": "call-1",
                    "namespace": None,
                    "tool": "municipal_web_search",
                    "arguments": {"query": "Burgos"},
                },
            }
        ]
    )
    runtime = runtime_with_client(tmp_path, client)
    _, completion = collect_runtime_stream(
        runtime.complete_stream(
            system="system",
            messages=[{"role": "user", "content": "busca"}],
            tools=[
                {
                    "name": "web_search",
                    "description": "Busca.",
                    "input_schema": {"type": "object"},
                }
            ],
            timeout=1,
            safety_identifier="owner-a",
        )
    )

    with pytest.raises(CodexProtocolError, match="owner mismatch"):
        runtime._acquire_session(completion.state_handle, "owner-b")

    runtime.close()


def test_session_capacity_counts_paused_sessions(tmp_path):
    client = ScriptedCodexClient(
        [
            {
                "id": 73,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-test",
                    "turnId": "turn-test",
                    "callId": "call-1",
                    "namespace": None,
                    "tool": "municipal_web_search",
                    "arguments": {"query": "Burgos"},
                },
            }
        ]
    )
    home = tmp_path / "codex-home"
    home.mkdir(mode=0o700)
    runtime = CodexSubscriptionRuntime(
        command="codex",
        codex_home=str(home),
        model="",
        reasoning_effort="medium",
        session_ttl_seconds=30,
        max_sessions=1,
        client_factory=lambda **_kwargs: client,
    )
    tools = [
        {
            "name": "web_search",
            "description": "Busca.",
            "input_schema": {"type": "object"},
        }
    ]
    collect_runtime_stream(
        runtime.complete_stream(
            system="system",
            messages=[{"role": "user", "content": "primera"}],
            tools=tools,
            timeout=1,
            safety_identifier="owner",
        )
    )

    with pytest.raises(CodexCapacityError, match="session limit"):
        collect_runtime_stream(
            runtime.complete_stream(
                system="system",
                messages=[{"role": "user", "content": "segunda"}],
                tools=tools,
                timeout=1,
                safety_identifier="owner",
            )
        )

    runtime.close()


def test_workspace_creation_failure_releases_capacity(monkeypatch, tmp_path):
    client = ScriptedCodexClient([])
    runtime = runtime_with_client(tmp_path, client)

    def fail_workspace(*_args, **_kwargs):
        raise OSError("no temporary space")

    monkeypatch.setattr(
        codex_app_server.tempfile,
        "TemporaryDirectory",
        fail_workspace,
    )

    with pytest.raises(OSError, match="temporary space"):
        runtime.complete(
            system="system",
            messages=[{"role": "user", "content": "hola"}],
            tools=[],
            timeout=1,
            safety_identifier="owner",
        )
    assert runtime._starting_sessions == 0


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
    monkeypatch.setattr(settings, "codex_subscription_enabled", True)
    monkeypatch.setattr(settings, "codex_subscription_real_data_allowed", True)

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
    monkeypatch.setattr(settings, "codex_subscription_enabled", True)
    monkeypatch.setattr(settings, "codex_subscription_real_data_allowed", True)

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


@pytest.mark.parametrize(
    ("enabled", "real_data_allowed"),
    [(False, False), (False, True), (True, False)],
)
def test_gateway_never_initializes_codex_without_both_opt_ins(
    monkeypatch,
    enabled,
    real_data_allowed,
):
    gateway = AIGateway()
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "assistant_runtime", "codex_subscription")
    monkeypatch.setattr(settings, "codex_subscription_enabled", enabled)
    monkeypatch.setattr(
        settings,
        "codex_subscription_real_data_allowed",
        real_data_allowed,
    )
    monkeypatch.setattr(
        gateway,
        "_get_codex_subscription_runtime",
        lambda: pytest.fail("Codex initialized without both opt-ins"),
    )

    assert gateway.enabled is False
