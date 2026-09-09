"""Privacy/AI Gateway v1.

Single choke point for every call to external AI APIs or the local Hermes
Agent API server. Principles (README §2, ADR-009): conversation text and
structured requirement fields typed by the user are the only data sent;
original documents and stored municipal files are never read or transmitted by
this module. Logging is metadata-only (runtime, model, token usage, stop
reason) — never message content.
"""

import json
import logging
import re
import threading
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from http import client as http_client
from time import monotonic
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import anthropic

from app.core.config import settings
from app.core.http import urlopen_without_redirects

logger = logging.getLogger(__name__)

MAX_OPENAI_RESPONSES_BYTES = 8 * 1024 * 1024
MAX_OPENAI_RESPONSES_STREAM_BYTES = 16 * 1024 * 1024
OPENAI_RESPONSES_STREAM_CHUNK_BYTES = 64 * 1024

_INLINE_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?P<payload>\{.*?\})\s*</tool_call>",
    flags=re.DOTALL,
)


class AssistantUnavailableError(Exception):
    """The AI gateway is not configured or the upstream API failed."""


class AssistantTimeoutError(AssistantUnavailableError):
    """The AI gateway exceeded the timeout assigned to this request."""


@dataclass(frozen=True)
class AIUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class AITextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class AITextDelta:
    text: str


@dataclass(frozen=True)
class AIToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass(frozen=True)
class AICompletion:
    model: str
    stop_reason: str
    content: list[AITextBlock | AIToolUseBlock]
    usage: AIUsage
    # Responses reasoning and function-call items are required again after a
    # tool executes. They remain in memory for this turn and are never stored
    # in AssistantMessage or exposed to clients.
    provider_state: tuple[dict[str, Any], ...] = ()


class AIGateway:
    def __init__(self) -> None:
        self._anthropic_client: anthropic.Anthropic | None = None
        self._codex_subscription_runtime = None
        self._codex_subscription_runtime_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        if settings.assistant_runtime == "anthropic":
            return bool(settings.anthropic_api_key)
        if settings.assistant_runtime == "hermes_agent":
            return hermes_agent_enabled()
        if settings.assistant_runtime == "openai_responses":
            return bool(settings.openai_api_key)
        if settings.assistant_runtime == "groq_responses":
            return bool(
                settings.groq_api_key
                and settings.groq_zero_data_retention_confirmed
            )
        if settings.assistant_runtime == "codex_subscription":
            if (
                settings.environment != "development"
                or not settings.codex_subscription_enabled
                or not settings.codex_subscription_real_data_allowed
            ):
                return False
            return self._get_codex_subscription_runtime().configured
        return False

    @property
    def model(self) -> str:
        if settings.assistant_runtime == "hermes_agent":
            return settings.hermes_agent_model
        if settings.assistant_runtime == "openai_responses":
            return settings.openai_responses_model
        if settings.assistant_runtime == "groq_responses":
            return settings.groq_responses_model
        if settings.assistant_runtime == "codex_subscription":
            return settings.codex_subscription_model or "codex-subscription-default"
        return settings.assistant_model

    @property
    def runtime_healthy(self) -> bool | None:
        if settings.assistant_runtime == "hermes_agent":
            if not self.enabled:
                return False
            return hermes_agent_healthy(
                timeout=settings.hermes_agent_health_timeout_seconds
            )
        if settings.assistant_runtime == "codex_subscription":
            if not self.enabled:
                return False
            return self._get_codex_subscription_runtime().healthy(
                timeout=settings.codex_subscription_health_timeout_seconds
            )
        return None

    def _get_codex_subscription_runtime(self):
        if self._codex_subscription_runtime is None:
            with self._codex_subscription_runtime_lock:
                if self._codex_subscription_runtime is None:
                    from app.assistant.codex_app_server import (
                        CodexSubscriptionRuntime,
                    )

                    self._codex_subscription_runtime = CodexSubscriptionRuntime(
                        command=settings.codex_subscription_command,
                        codex_home=settings.codex_subscription_home,
                        model=settings.codex_subscription_model,
                        reasoning_effort=(
                            settings.codex_subscription_reasoning_effort
                        ),
                        session_ttl_seconds=(
                            settings.codex_subscription_session_ttl_seconds
                        ),
                        max_sessions=settings.codex_subscription_max_sessions,
                    )
        return self._codex_subscription_runtime

    def discard_provider_state(self, messages: list[dict]) -> None:
        if self._codex_subscription_runtime is not None:
            self._codex_subscription_runtime.discard_provider_state(messages)

    def _get_anthropic_client(self) -> anthropic.Anthropic:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")
        if self._anthropic_client is None:
            self._anthropic_client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key,
                max_retries=0,
            )
        return self._anthropic_client

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None = None,
        safety_identifier: str | None = None,
    ) -> AICompletion:
        if settings.assistant_runtime == "anthropic":
            return self._complete_anthropic(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
            )
        if settings.assistant_runtime == "hermes_agent":
            return self._complete_hermes_agent(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
            )
        if settings.assistant_runtime in {"openai_responses", "groq_responses"}:
            return self._complete_openai_responses(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
                safety_identifier=safety_identifier,
            )
        if settings.assistant_runtime == "codex_subscription":
            return self._complete_codex_subscription(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
                safety_identifier=safety_identifier,
            )
        raise AssistantUnavailableError("Assistant runtime is not supported")

    def complete_stream(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None = None,
        safety_identifier: str | None = None,
    ) -> Generator[AITextDelta, None, AICompletion]:
        if settings.assistant_runtime == "anthropic":
            completion = yield from self._complete_stream_anthropic(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
            )
            return completion
        if settings.assistant_runtime == "hermes_agent":
            completion = yield from self._complete_stream_hermes_agent(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
            )
            return completion
        if settings.assistant_runtime in {"openai_responses", "groq_responses"}:
            completion = yield from self._complete_stream_openai_responses(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
                safety_identifier=safety_identifier,
            )
            return completion
        if settings.assistant_runtime == "codex_subscription":
            completion = yield from self._complete_stream_codex_subscription(
                system=system,
                messages=messages,
                tools=tools,
                timeout_seconds=timeout_seconds,
                safety_identifier=safety_identifier,
            )
            return completion
        raise AssistantUnavailableError("Assistant runtime is not supported")

    def _complete_anthropic(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
    ) -> AICompletion:
        client = self._get_anthropic_client()
        request_timeout = _bounded_gateway_timeout(timeout_seconds)
        try:
            response = client.messages.create(
                model=settings.assistant_model,
                max_tokens=settings.assistant_max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=_to_anthropic_messages(messages),
                tools=tools,
                timeout=request_timeout,
            )
        except anthropic.APITimeoutError as error:
            logger.error("Assistant API timeout: runtime=anthropic")
            raise AssistantTimeoutError("Assistant request timed out") from error
        except anthropic.APIStatusError as error:
            logger.error(
                "Assistant API error: status=%s type=%s",
                error.status_code,
                getattr(error, "type", None),
            )
            raise AssistantUnavailableError("Assistant API request failed") from error
        except anthropic.APIConnectionError as error:
            logger.error("Assistant API connection error")
            raise AssistantUnavailableError(
                "Assistant API connection failed"
            ) from error

        logger.info(
            "Assistant completion: runtime=anthropic model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            response.model,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return _from_anthropic_message(response)

    def _complete_stream_anthropic(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
    ) -> Generator[AITextDelta, None, AICompletion]:
        client = self._get_anthropic_client()
        request_timeout = _bounded_gateway_timeout(timeout_seconds)
        try:
            with client.messages.stream(
                model=settings.assistant_model,
                max_tokens=settings.assistant_max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=_to_anthropic_messages(messages),
                tools=tools,
                timeout=request_timeout,
            ) as stream:
                for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        text = getattr(delta, "text", "")
                        if text:
                            yield AITextDelta(text=text)
                response = stream.get_final_message()
        except anthropic.APITimeoutError as error:
            logger.error("Assistant API timeout: runtime=anthropic")
            raise AssistantTimeoutError("Assistant request timed out") from error
        except anthropic.APIStatusError as error:
            logger.error(
                "Assistant API error: status=%s type=%s",
                error.status_code,
                getattr(error, "type", None),
            )
            raise AssistantUnavailableError("Assistant API request failed") from error
        except anthropic.APIConnectionError as error:
            logger.error("Assistant API connection error")
            raise AssistantUnavailableError(
                "Assistant API connection failed"
            ) from error

        completion = _from_anthropic_message(response)
        logger.info(
            "Assistant completion: runtime=anthropic model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            completion.model,
            completion.stop_reason,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )
        return completion

    def _complete_hermes_agent(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
    ) -> AICompletion:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")

        request_timeout = _bounded_gateway_timeout(
            timeout_seconds,
            settings.hermes_agent_timeout_seconds,
        )
        completion = complete_hermes_agent(
            system=system,
            messages=messages,
            tools=tools,
            model=settings.hermes_agent_model,
            max_tokens=settings.assistant_max_tokens,
            timeout=request_timeout,
            tool_choice="auto",
            log_context="assistant",
        )
        logger.info(
            "Assistant completion: runtime=hermes_agent model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            completion.model,
            completion.stop_reason,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )
        return completion

    def _complete_stream_hermes_agent(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
    ) -> Generator[AITextDelta, None, AICompletion]:
        request_timeout = _bounded_gateway_timeout(
            timeout_seconds,
            settings.hermes_agent_timeout_seconds,
        )
        request_deadline = monotonic() + request_timeout
        try:
            completion = yield from complete_hermes_agent_stream(
                system=system,
                messages=messages,
                tools=tools,
                model=settings.hermes_agent_model,
                max_tokens=settings.assistant_max_tokens,
                timeout=request_timeout,
                tool_choice="auto",
                log_context="assistant",
            )
        except AssistantTimeoutError:
            raise
        except AssistantUnavailableError as stream_error:
            fallback_timeout = request_deadline - monotonic()
            if fallback_timeout <= 0:
                raise AssistantTimeoutError(
                    "Assistant request timed out"
                ) from stream_error
            completion = complete_hermes_agent(
                system=system,
                messages=messages,
                tools=tools,
                model=settings.hermes_agent_model,
                max_tokens=settings.assistant_max_tokens,
                timeout=fallback_timeout,
                tool_choice="auto",
                log_context="assistant_fallback",
            )
            text = _completion_text_for_delta(completion)
            if text:
                yield AITextDelta(text=text)

        logger.info(
            "Assistant completion: runtime=hermes_agent model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            completion.model,
            completion.stop_reason,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )
        return completion

    def _complete_openai_responses(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
        safety_identifier: str | None,
    ) -> AICompletion:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")

        request_timeout = _bounded_gateway_timeout(timeout_seconds)
        completion = complete_openai_responses(
            system=system,
            messages=messages,
            tools=tools,
            timeout=request_timeout,
            safety_identifier=safety_identifier,
        )
        _log_openai_responses_completion(completion)
        return completion

    def _complete_stream_openai_responses(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
        safety_identifier: str | None,
    ) -> Generator[AITextDelta, None, AICompletion]:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")

        request_timeout = _bounded_gateway_timeout(timeout_seconds)
        completion = yield from complete_openai_responses_stream(
            system=system,
            messages=messages,
            tools=tools,
            timeout=request_timeout,
            safety_identifier=safety_identifier,
        )
        _log_openai_responses_completion(completion)
        return completion

    def _complete_codex_subscription(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
        safety_identifier: str | None,
    ) -> AICompletion:
        if not self.enabled:
            raise AssistantUnavailableError(
                "Codex subscription runtime is not configured"
            )
        request_timeout = _bounded_gateway_timeout(timeout_seconds)
        try:
            runtime_completion = self._get_codex_subscription_runtime().complete(
                system=system,
                messages=messages,
                tools=tools,
                timeout=request_timeout,
                safety_identifier=safety_identifier,
            )
        except Exception as error:
            _raise_codex_subscription_gateway_error(error)
        completion = _from_codex_subscription_completion(runtime_completion)
        _log_codex_subscription_completion(completion)
        return completion

    def _complete_stream_codex_subscription(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_seconds: float | None,
        safety_identifier: str | None,
    ) -> Generator[AITextDelta, None, AICompletion]:
        if not self.enabled:
            raise AssistantUnavailableError(
                "Codex subscription runtime is not configured"
            )
        request_timeout = _bounded_gateway_timeout(timeout_seconds)
        stream = None
        stream_completed = False
        try:
            stream = self._get_codex_subscription_runtime().complete_stream(
                system=system,
                messages=messages,
                tools=tools,
                timeout=request_timeout,
                safety_identifier=safety_identifier,
            )
            while True:
                try:
                    delta = next(stream)
                except StopIteration as stop:
                    runtime_completion = stop.value
                    stream_completed = True
                    break
                if delta:
                    yield AITextDelta(text=delta)
        except Exception as error:
            _raise_codex_subscription_gateway_error(error)
        finally:
            if stream is not None and not stream_completed:
                stream.close()
        completion = _from_codex_subscription_completion(runtime_completion)
        _log_codex_subscription_completion(completion)
        return completion


def _bounded_gateway_timeout(
    requested_timeout: float | None,
    *runtime_limits: float,
) -> float:
    candidates = [settings.assistant_gateway_timeout_seconds, *runtime_limits]
    if requested_timeout is not None:
        candidates.append(requested_timeout)
    timeout = min(candidates)
    if timeout <= 0:
        raise AssistantTimeoutError("Assistant request timed out")
    return timeout


def _raise_codex_subscription_gateway_error(error: Exception) -> None:
    from app.assistant.codex_app_server import (
        CodexSubscriptionError,
        CodexTimeoutError,
    )

    if isinstance(error, CodexTimeoutError):
        logger.error("Assistant API timeout: runtime=codex_subscription")
        raise AssistantTimeoutError("Assistant request timed out") from error
    if isinstance(error, CodexSubscriptionError):
        logger.error(
            "Assistant runtime failed: runtime=codex_subscription error_type=%s",
            type(error).__name__,
        )
        raise AssistantUnavailableError("Assistant runtime failed") from error
    raise error


def _from_codex_subscription_completion(response) -> AICompletion:
    content: list[AITextBlock | AIToolUseBlock] = []
    if response.text:
        content.append(AITextBlock(text=response.text))
    content.extend(
        AIToolUseBlock(
            id=call.id,
            name=call.name,
            input=dict(call.arguments),
        )
        for call in response.tool_calls
    )
    provider_state: tuple[dict[str, Any], ...] = ()
    if response.state_handle:
        provider_state = (
            {
                "type": "codex_subscription_session",
                "handle": response.state_handle,
            },
        )
    return AICompletion(
        model=response.model,
        stop_reason=response.stop_reason,
        content=content,
        usage=AIUsage(
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        ),
        provider_state=provider_state,
    )


def _log_codex_subscription_completion(completion: AICompletion) -> None:
    logger.info(
        "Assistant completion: runtime=codex_subscription model=%s "
        "stop_reason=%s input_tokens=%s output_tokens=%s",
        completion.model,
        completion.stop_reason,
        completion.usage.input_tokens,
        completion.usage.output_tokens,
    )


def complete_openai_responses(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    timeout: float,
    safety_identifier: str | None,
) -> AICompletion:
    if not _responses_api_key():
        raise AssistantUnavailableError("Responses runtime is not configured")

    payload = _openai_responses_payload(
        system=system,
        messages=messages,
        tools=tools,
        safety_identifier=safety_identifier,
    )
    request = urlrequest.Request(
        _openai_responses_url(),
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=_openai_responses_headers(accept="application/json"),
        method="POST",
    )

    try:
        with _openai_responses_urlopen(request, timeout=timeout) as response:
            raw_body = response.read(MAX_OPENAI_RESPONSES_BYTES + 1)
        if len(raw_body) > MAX_OPENAI_RESPONSES_BYTES:
            raise ValueError("response body exceeds the configured limit")
        response_data = json.loads(raw_body.decode("utf-8"))
        return _from_openai_responses_response(response_data)
    except urlerror.HTTPError as error:
        logger.error(
            "OpenAI Responses API error: status=%s",
            error.code,
        )
        raise AssistantUnavailableError("Assistant API request failed") from error
    except TimeoutError as error:
        logger.error("OpenAI Responses API timeout")
        raise AssistantTimeoutError("Assistant request timed out") from error
    except urlerror.URLError as error:
        if isinstance(error.reason, TimeoutError):
            logger.error("OpenAI Responses API timeout")
            raise AssistantTimeoutError("Assistant request timed out") from error
        logger.error("OpenAI Responses API connection error")
        raise AssistantUnavailableError(
            "Assistant API connection failed"
        ) from error
    except (http_client.HTTPException, OSError) as error:
        logger.error("OpenAI Responses API connection error")
        raise AssistantUnavailableError(
            "Assistant API connection failed"
        ) from error
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as error:
        logger.error("OpenAI Responses API returned an invalid response")
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error


def complete_openai_responses_stream(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    timeout: float,
    safety_identifier: str | None,
) -> Generator[AITextDelta, None, AICompletion]:
    if not _responses_api_key():
        raise AssistantUnavailableError("Responses runtime is not configured")

    payload = _openai_responses_payload(
        system=system,
        messages=messages,
        tools=tools,
        safety_identifier=safety_identifier,
    )
    payload["stream"] = True
    request = urlrequest.Request(
        _openai_responses_url(),
        data=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=_openai_responses_headers(accept="text/event-stream"),
        method="POST",
    )

    terminal_response: dict | None = None
    request_deadline = monotonic() + timeout
    try:
        with _openai_responses_urlopen(request, timeout=timeout) as response:
            for event in _iter_openai_responses_sse(
                response,
                deadline=request_deadline,
            ):
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise ValueError("text delta is not a string")
                    if delta:
                        yield AITextDelta(text=delta)
                elif event_type in {"response.completed", "response.incomplete"}:
                    candidate = event.get("response")
                    if not isinstance(candidate, dict):
                        raise ValueError("terminal event has no response")
                    terminal_response = candidate
                    break
                elif event_type in {
                    "error",
                    "response.cancelled",
                    "response.failed",
                }:
                    logger.error(
                        "OpenAI Responses stream failed: event=%s",
                        event_type,
                    )
                    raise AssistantUnavailableError(
                        "Assistant API stream failed"
                    )

        if terminal_response is None:
            raise ValueError("stream ended without a terminal response")
        return _from_openai_responses_response(terminal_response)
    except AssistantUnavailableError:
        raise
    except urlerror.HTTPError as error:
        logger.error(
            "OpenAI Responses stream API error: status=%s",
            error.code,
        )
        raise AssistantUnavailableError("Assistant API request failed") from error
    except TimeoutError as error:
        logger.error("OpenAI Responses stream API timeout")
        raise AssistantTimeoutError("Assistant request timed out") from error
    except urlerror.URLError as error:
        if isinstance(error.reason, TimeoutError):
            logger.error("OpenAI Responses stream API timeout")
            raise AssistantTimeoutError("Assistant request timed out") from error
        logger.error("OpenAI Responses stream API connection error")
        raise AssistantUnavailableError(
            "Assistant API connection failed"
        ) from error
    except (http_client.HTTPException, OSError) as error:
        logger.error("OpenAI Responses stream API connection error")
        raise AssistantUnavailableError(
            "Assistant API connection failed"
        ) from error
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as error:
        logger.error("OpenAI Responses stream returned an invalid response")
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error


def _openai_responses_payload(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    safety_identifier: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": _responses_model(),
        "instructions": system,
        "input": _to_openai_responses_input(messages),
        "max_output_tokens": _responses_max_output_tokens(),
        "reasoning": {
            "effort": _responses_reasoning_effort(),
        },
        "parallel_tool_calls": True,
    }
    if settings.assistant_runtime == "openai_responses":
        payload.update(
            {
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "truncation": "auto",
            }
        )
    response_tools = _to_openai_responses_tools(tools)
    if response_tools:
        payload["tools"] = response_tools
        payload["tool_choice"] = "auto"
    if safety_identifier and settings.assistant_runtime == "openai_responses":
        if len(safety_identifier) > 64:
            raise ValueError("safety identifier exceeds 64 characters")
        payload["safety_identifier"] = safety_identifier
    return payload


def _responses_api_key() -> str | None:
    if settings.assistant_runtime == "groq_responses":
        return settings.groq_api_key
    return settings.openai_api_key


def _responses_base_url() -> str:
    if settings.assistant_runtime == "groq_responses":
        return settings.groq_responses_base_url
    return settings.openai_responses_base_url


def _responses_model() -> str:
    if settings.assistant_runtime == "groq_responses":
        return settings.groq_responses_model
    return settings.openai_responses_model


def _responses_reasoning_effort() -> str:
    if settings.assistant_runtime == "groq_responses":
        return settings.groq_responses_reasoning_effort
    return settings.openai_responses_reasoning_effort


def _responses_max_output_tokens() -> int:
    if settings.assistant_runtime == "groq_responses":
        return settings.groq_responses_max_output_tokens
    return settings.openai_responses_max_output_tokens


def _openai_responses_url() -> str:
    return f"{_responses_base_url().rstrip('/')}/responses"


def _openai_responses_headers(*, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {_responses_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": f"{settings.app_name}/{settings.app_version}",
    }


def _openai_responses_urlopen(request, *, timeout: float):
    return urlopen_without_redirects(request, timeout=timeout)


def _iter_openai_responses_sse(
    response,
    *,
    deadline: float,
) -> Generator[dict, None, None]:
    data_lines: list[str] = []
    for line in _iter_openai_responses_sse_lines(response, deadline=deadline):
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data != "[DONE]":
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ValueError("stream event is not an object")
                    yield event
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())

    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError("stream event is not an object")
            yield event


def _iter_openai_responses_sse_lines(
    response,
    *,
    deadline: float,
) -> Generator[str, None, None]:
    """Read bounded chunks so a peer cannot hold an unlimited SSE line open."""
    buffered = bytearray()
    total_bytes = 0
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise AssistantTimeoutError("Assistant request timed out")
        _set_openai_response_read_timeout(response, remaining)
        read1 = getattr(response, "read1", None)
        if callable(read1):
            raw_chunk = read1(OPENAI_RESPONSES_STREAM_CHUNK_BYTES)
        else:
            # A one-byte fallback still returns control to the monotonic
            # deadline after every socket operation.
            raw_chunk = response.read(1)
        if monotonic() >= deadline:
            raise AssistantTimeoutError("Assistant request timed out")
        if not raw_chunk:
            break
        if not isinstance(raw_chunk, bytes):
            raise ValueError("stream chunk is not bytes")
        total_bytes += len(raw_chunk)
        if total_bytes > MAX_OPENAI_RESPONSES_STREAM_BYTES:
            raise ValueError("stream exceeds the configured limit")
        buffered.extend(raw_chunk)
        while True:
            newline_index = buffered.find(b"\n")
            if newline_index < 0:
                break
            raw_line = bytes(buffered[:newline_index])
            del buffered[: newline_index + 1]
            yield raw_line.rstrip(b"\r").decode("utf-8")

    if buffered:
        yield bytes(buffered).rstrip(b"\r").decode("utf-8")


def _set_openai_response_read_timeout(response, timeout: float) -> None:
    """Reduce the CPython HTTP socket timeout to the remaining deadline."""
    explicit_setter = getattr(response, "set_read_timeout", None)
    if callable(explicit_setter):
        explicit_setter(timeout)
        return
    response_fp = getattr(response, "fp", None)
    raw = getattr(response_fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(timeout)


def _log_openai_responses_completion(completion: AICompletion) -> None:
    logger.info(
        "Assistant completion: runtime=%s model=%s "
        "stop_reason=%s input_tokens=%s output_tokens=%s",
        settings.assistant_runtime,
        completion.model,
        completion.stop_reason,
        completion.usage.input_tokens,
        completion.usage.output_tokens,
    )


def hermes_agent_enabled() -> bool:
    if not settings.hermes_agent_base_url or not settings.hermes_agent_api_key:
        return False
    if not settings.hermes_agent_native_tools_disabled_confirmed:
        return False
    return (
        settings.environment != "production"
        or settings.hermes_agent_real_data_allowed
    )


def hermes_agent_healthy(*, timeout: float) -> bool:
    request = urlrequest.Request(
        _hermes_agent_url("health"),
        headers=_hermes_agent_headers(),
        method="GET",
    )
    try:
        with urlopen_without_redirects(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urlerror.HTTPError, urlerror.URLError, TimeoutError):
        return False


def complete_hermes_agent(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    model: str,
    max_tokens: int,
    timeout: float,
    tool_choice: str | dict | None,
    log_context: str,
) -> AICompletion:
    if not hermes_agent_enabled():
        raise AssistantUnavailableError("Hermes Agent is not configured")

    payload: dict[str, Any] = {
        "model": model,
        "messages": _to_openai_messages(system, messages),
        "max_tokens": max_tokens,
    }
    openai_tools = _to_openai_tools(tools)
    if openai_tools:
        payload["tools"] = openai_tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    request = urlrequest.Request(
        _hermes_agent_url("chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers=_hermes_agent_headers(),
        method="POST",
    )

    try:
        with urlopen_without_redirects(request, timeout=timeout) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as error:
        logger.error(
            "Hermes Agent API error: context=%s status=%s",
            log_context,
            error.code,
        )
        raise AssistantUnavailableError("Assistant API request failed") from error
    except TimeoutError as error:
        logger.error("Hermes Agent API timeout: context=%s", log_context)
        raise AssistantTimeoutError("Assistant request timed out") from error
    except urlerror.URLError as error:
        if isinstance(error.reason, TimeoutError):
            logger.error("Hermes Agent API timeout: context=%s", log_context)
            raise AssistantTimeoutError("Assistant request timed out") from error
        logger.error("Hermes Agent API connection error: context=%s", log_context)
        raise AssistantUnavailableError("Assistant API connection failed") from error
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        logger.error(
            "Hermes Agent API returned an invalid response: context=%s",
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error

    try:
        return _from_openai_response(response_data)
    except (KeyError, TypeError, ValueError) as error:
        logger.error(
            "Hermes Agent API returned an invalid response: context=%s",
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error


def complete_hermes_agent_stream(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    model: str,
    max_tokens: int,
    timeout: float,
    tool_choice: str | dict | None,
    log_context: str,
) -> Generator[AITextDelta, None, AICompletion]:
    if not hermes_agent_enabled():
        raise AssistantUnavailableError("Hermes Agent is not configured")

    payload: dict[str, Any] = {
        "model": model,
        "messages": _to_openai_messages(system, messages),
        "max_tokens": max_tokens,
        "stream": True,
    }
    openai_tools = _to_openai_tools(tools)
    if openai_tools:
        payload["tools"] = openai_tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    request = urlrequest.Request(
        _hermes_agent_url("chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers=_hermes_agent_headers(),
        method="POST",
    )

    content_buffer = ""
    emitted_text = ""
    finish_reason: str | None = None
    response_model = model
    tool_calls: dict[int, dict] = {}
    try:
        with urlopen_without_redirects(request, timeout=timeout) as response:
            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                response_model = chunk.get("model") or response_model
                choice = (chunk.get("choices") or [{}])[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                content_delta = delta.get("content") or ""
                if content_delta:
                    content_buffer += content_delta
                    safe_text, content_buffer = _pop_safe_hermes_text(content_buffer)
                    emitted_text += safe_text
                    if safe_text:
                        yield AITextDelta(text=safe_text)
                for tool_call_delta in delta.get("tool_calls") or []:
                    index = int(tool_call_delta.get("index") or 0)
                    accumulated = tool_calls.setdefault(
                        index,
                        {
                            "id": tool_call_delta.get("id"),
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if tool_call_delta.get("id"):
                        accumulated["id"] = tool_call_delta["id"]
                    function_delta = tool_call_delta.get("function") or {}
                    if function_delta.get("name"):
                        accumulated["function"]["name"] += function_delta["name"]
                    if function_delta.get("arguments"):
                        accumulated["function"]["arguments"] += function_delta[
                            "arguments"
                        ]
    except urlerror.HTTPError as error:
        logger.error(
            "Hermes Agent stream API error: context=%s status=%s",
            log_context,
            error.code,
        )
        raise AssistantUnavailableError("Assistant API request failed") from error
    except TimeoutError as error:
        logger.error("Hermes Agent stream API timeout: context=%s", log_context)
        raise AssistantTimeoutError("Assistant request timed out") from error
    except urlerror.URLError as error:
        if isinstance(error.reason, TimeoutError):
            logger.error(
                "Hermes Agent stream API timeout: context=%s",
                log_context,
            )
            raise AssistantTimeoutError("Assistant request timed out") from error
        logger.error("Hermes Agent stream API connection error: context=%s", log_context)
        raise AssistantUnavailableError("Assistant API connection failed") from error
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        logger.error(
            "Hermes Agent stream API returned an invalid response: context=%s",
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error

    final_safe_text, held_text = _pop_safe_hermes_text(content_buffer, final=True)
    emitted_text += final_safe_text
    if final_safe_text:
        yield AITextDelta(text=final_safe_text)

    full_content = emitted_text + held_text
    response_data = {
        "model": response_model,
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": full_content,
                    "tool_calls": [
                        tool_call
                        for _, tool_call in sorted(tool_calls.items())
                        if (tool_call.get("function") or {}).get("name")
                    ],
                },
            }
        ],
        "usage": {},
    }
    try:
        return _from_openai_response(response_data)
    except (KeyError, TypeError, ValueError) as error:
        logger.error(
            "Hermes Agent stream API returned an invalid response: context=%s",
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error


def _hermes_agent_url(path: str) -> str:
    base_url = settings.hermes_agent_base_url.rstrip("/")
    clean_path = path.strip("/")
    if clean_path.startswith("health"):
        health_base_url = base_url.removesuffix("/v1")
        return f"{health_base_url}/{clean_path}"
    if base_url.endswith("/v1"):
        return f"{base_url}/{clean_path}"
    return f"{base_url}/v1/{clean_path}"


def _hermes_agent_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.hermes_agent_api_key:
        headers["Authorization"] = f"Bearer {settings.hermes_agent_api_key}"
    return headers


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    converted = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = [_to_anthropic_content_block(block) for block in content]
        converted.append({"role": message["role"], "content": content})
    return converted


def _to_anthropic_content_block(block) -> dict:
    if isinstance(block, dict):
        return block
    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {"type": "text", "text": block.text}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    return {"type": "text", "text": str(block)}


def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    converted: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
        elif role == "assistant":
            converted.append(_assistant_blocks_to_openai_message(content))
        elif role == "user" and _is_tool_result_list(content):
            converted.extend(_tool_results_to_openai_messages(content))
        else:
            converted.append(
                {
                    "role": role,
                    "content": json.dumps(content, ensure_ascii=False),
                }
            )
    return converted


def _assistant_blocks_to_openai_message(content: list) -> dict:
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content:
        block_type = _block_value(block, "type")
        if block_type == "text":
            text = _block_value(block, "text")
            if text:
                text_parts.append(str(text))
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": str(_block_value(block, "id")),
                    "type": "function",
                    "function": {
                        "name": str(_block_value(block, "name")),
                        "arguments": json.dumps(
                            _block_value(block, "input") or {},
                            ensure_ascii=False,
                        ),
                    },
                }
            )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tool_results_to_openai_messages(content: list[dict]) -> list[dict]:
    return [
        {
            "role": "tool",
            "tool_call_id": str(block["tool_use_id"]),
            "content": str(block.get("content", "")),
        }
        for block in content
        if block.get("type") == "tool_result"
    ]


def _is_tool_result_list(content) -> bool:
    return isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _to_openai_responses_input(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        provider_state = message.get("provider_state")
        if provider_state:
            if not isinstance(provider_state, (list, tuple)) or not all(
                isinstance(item, dict) for item in provider_state
            ):
                raise ValueError("provider state is invalid")
            converted.extend(provider_state)
        elif isinstance(content, str):
            input_message = {"role": role, "content": content}
            if role == "assistant" and settings.assistant_runtime == "openai_responses":
                # Persisted assistant messages are completed replies. GPT-5.6
                # uses this label to avoid treating tool preambles as answers.
                input_message["phase"] = "final_answer"
            converted.append(input_message)
        elif role == "assistant":
            converted.extend(_assistant_blocks_to_openai_responses_items(content))
        elif role == "user" and _is_tool_result_list(content):
            converted.extend(_tool_results_to_openai_responses_items(content))
        else:
            converted.append(
                {
                    "role": role,
                    "content": json.dumps(content, ensure_ascii=False),
                }
            )
    return converted


def _assistant_blocks_to_openai_responses_items(content: list) -> list[dict]:
    text_parts: list[str] = []
    function_calls: list[dict] = []
    for block in content:
        block_type = _block_value(block, "type")
        if block_type == "text":
            text = _block_value(block, "text")
            if text:
                text_parts.append(str(text))
        elif block_type == "tool_use":
            call_id = str(_block_value(block, "id") or "")
            name = str(_block_value(block, "name") or "")
            if not call_id or not name:
                raise ValueError("tool call is missing its identifier or name")
            function_calls.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(
                        _block_value(block, "input") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )

    items: list[dict] = []
    if text_parts:
        message_item = {
            "role": "assistant",
            "content": "\n\n".join(text_parts),
        }
        if settings.assistant_runtime == "openai_responses":
            message_item["phase"] = (
                "commentary" if function_calls else "final_answer"
            )
        items.append(message_item)
    items.extend(function_calls)
    return items


def _tool_results_to_openai_responses_items(content: list[dict]) -> list[dict]:
    return [
        {
            "type": "function_call_output",
            "call_id": str(block["tool_use_id"]),
            "output": str(block.get("content", "")),
        }
        for block in content
        if block.get("type") == "tool_result"
    ]


def _to_openai_responses_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object"}),
            # Existing schemas intentionally permit optional fields and do not
            # all declare additionalProperties=false. Preserve that contract.
            "strict": False,
        }
        for tool in tools
    ]


def _from_openai_responses_response(response_data: dict) -> AICompletion:
    if not isinstance(response_data, dict):
        raise ValueError("response is not an object")
    status = response_data.get("status")
    if status not in {"completed", "incomplete"}:
        raise ValueError("response did not reach a supported terminal state")
    if response_data.get("error"):
        raise ValueError("response contains an upstream error")
    model = response_data.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("response model is missing")
    output = response_data.get("output")
    if not isinstance(output, list):
        raise ValueError("response output is missing")

    content: list[AITextBlock | AIToolUseBlock] = []
    commentary_content: list[AITextBlock] = []
    refused = False
    saw_final_answer = False
    saw_function_call = False
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("response output item is invalid")
        item_type = item.get("type")
        if item_type == "message":
            item_status = item.get("status")
            if item_status not in {"completed", "incomplete"}:
                raise ValueError("response message status is invalid")
            phase = item.get("phase")
            if phase not in {None, "commentary", "final_answer"}:
                raise ValueError("response message phase is invalid")
            if phase in {None, "final_answer"}:
                saw_final_answer = True
            text_target = (
                commentary_content if phase == "commentary" else content
            )
            parts = item.get("content")
            if not isinstance(parts, list):
                raise ValueError("response message content is invalid")
            for part in parts:
                if not isinstance(part, dict):
                    raise ValueError("response content part is invalid")
                part_type = part.get("type")
                if part_type == "output_text":
                    text = part.get("text")
                    if not isinstance(text, str):
                        raise ValueError("response text is invalid")
                    if text:
                        text_target.append(AITextBlock(text=text))
                elif part_type == "refusal":
                    refusal = part.get("refusal")
                    if not isinstance(refusal, str):
                        raise ValueError("response refusal is invalid")
                    refused = True
        elif item_type == "function_call":
            saw_function_call = True
            if status != "completed" or item.get("status") != "completed":
                raise ValueError("incomplete function calls cannot be executed")
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("function call identifier is missing")
            if not isinstance(name, str) or not name:
                raise ValueError("function call name is missing")
            if not isinstance(arguments, str):
                raise ValueError("function call arguments are invalid")
            try:
                decoded_arguments = json.loads(arguments)
            except json.JSONDecodeError as error:
                raise ValueError("function call arguments are malformed") from error
            if not isinstance(decoded_arguments, dict):
                raise ValueError("function call arguments must be an object")
            content.append(
                AIToolUseBlock(
                    id=call_id,
                    name=name,
                    input=decoded_arguments,
                )
            )

    incomplete_details = response_data.get("incomplete_details") or {}
    incomplete_reason = (
        incomplete_details.get("reason")
        if isinstance(incomplete_details, dict)
        else None
    )
    if incomplete_reason == "content_filter":
        refused = True

    if refused:
        stop_reason = "refusal"
    elif status == "incomplete":
        if saw_function_call:
            raise ValueError("incomplete response contains a function call")
        content.extend(commentary_content)
        stop_reason = "pause_turn"
    elif any(block.type == "tool_use" for block in content):
        content.extend(commentary_content)
        stop_reason = "tool_use"
    elif not saw_final_answer:
        content.extend(commentary_content)
        stop_reason = "pause_turn"
    else:
        stop_reason = "end_turn"

    usage = response_data.get("usage") or {}
    if not isinstance(usage, dict):
        raise ValueError("response usage is invalid")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is not None and not isinstance(input_tokens, int):
        raise ValueError("input token usage is invalid")
    if output_tokens is not None and not isinstance(output_tokens, int):
        raise ValueError("output token usage is invalid")

    return AICompletion(
        model=model,
        stop_reason=stop_reason,
        content=content,
        usage=AIUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
        provider_state=tuple(output),
    )


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object"}),
            },
        }
        for tool in tools
    ]


def _from_openai_response(response_data: dict) -> AICompletion:
    choice = response_data["choices"][0]
    message = choice["message"]
    raw_content = message.get("content") or ""
    cleaned_content, inline_tool_calls = _extract_inline_tool_calls(raw_content)
    standalone_tool_call = (
        _parse_standalone_tool_call(cleaned_content)
        if not inline_tool_calls and not (message.get("tool_calls") or [])
        else None
    )
    if standalone_tool_call is not None:
        cleaned_content = ""

    content: list[AITextBlock | AIToolUseBlock] = []
    if cleaned_content:
        content.append(AITextBlock(text=cleaned_content))

    for tool_call in message.get("tool_calls") or []:
        parsed_tool_call = _parse_openai_tool_call(tool_call)
        if parsed_tool_call is not None:
            content.append(parsed_tool_call)

    content.extend(inline_tool_calls)
    if standalone_tool_call is not None:
        content.append(standalone_tool_call)
    stop_reason = (
        "tool_use"
        if any(block.type == "tool_use" for block in content)
        else _map_openai_finish_reason(choice.get("finish_reason"))
    )
    usage = response_data.get("usage") or {}
    return AICompletion(
        model=response_data.get("model") or settings.hermes_agent_model,
        stop_reason=stop_reason,
        content=content,
        usage=AIUsage(
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        ),
    )


def _extract_inline_tool_calls(
    content: str,
) -> tuple[str, list[AIToolUseBlock]]:
    tool_calls: list[AIToolUseBlock] = []
    for match in _INLINE_TOOL_CALL_RE.finditer(content):
        parsed_tool_call = _parse_tool_call_payload(match.group("payload"))
        if parsed_tool_call is not None:
            tool_calls.append(parsed_tool_call)

    cleaned_content = _INLINE_TOOL_CALL_RE.sub("", content).strip()
    return cleaned_content, tool_calls


def _parse_standalone_tool_call(content: str) -> AIToolUseBlock | None:
    stripped = content.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not (
        parsed.get("name")
        or parsed.get("tool_name")
        or (parsed.get("function") or {}).get("name")
    ):
        return None
    return _parse_tool_call_payload(stripped)


def _parse_openai_tool_call(tool_call: dict) -> AIToolUseBlock | None:
    function = tool_call.get("function") or {}
    name = function.get("name")
    if not name:
        return None
    return AIToolUseBlock(
        id=str(tool_call.get("id") or f"call_{uuid.uuid4().hex}"),
        name=str(name),
        input=_decode_tool_arguments(function.get("arguments")),
    )


def _parse_tool_call_payload(payload: str) -> AIToolUseBlock | None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("AI runtime emitted a malformed inline tool call")
        return None

    function = parsed.get("function") or {}
    name = parsed.get("name") or parsed.get("tool_name") or function.get("name")
    if not name:
        logger.warning("AI runtime emitted an inline tool call without a name")
        return None

    arguments = (
        parsed.get("arguments")
        if "arguments" in parsed
        else parsed.get("input", function.get("arguments"))
    )
    return AIToolUseBlock(
        id=str(parsed.get("id") or f"call_{uuid.uuid4().hex}"),
        name=str(name),
        input=_decode_tool_arguments(arguments),
    )


def _decode_tool_arguments(arguments) -> dict:
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            logger.warning("AI runtime emitted malformed tool arguments")
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _map_openai_finish_reason(finish_reason: str | None) -> str:
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


def _block_value(block, key: str):
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _from_anthropic_message(response) -> AICompletion:
    return AICompletion(
        model=response.model,
        stop_reason=response.stop_reason,
        content=[
            AITextBlock(text=block.text)
            if block.type == "text"
            else AIToolUseBlock(
                id=block.id,
                name=block.name,
                input=dict(block.input),
            )
            for block in response.content
            if block.type in {"text", "tool_use"}
        ],
        usage=AIUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        ),
    )


def _completion_text_for_delta(completion: AICompletion) -> str:
    if completion.stop_reason == "tool_use":
        return ""
    return "\n\n".join(
        block.text for block in completion.content if block.type == "text"
    ).strip()


def _pop_safe_hermes_text(buffer: str, *, final: bool = False) -> tuple[str, str]:
    marker = "<tool_call>"
    partial_marker = "<tool_call"
    marker_index = buffer.find(partial_marker)
    if marker_index >= 0:
        return buffer[:marker_index], buffer[marker_index:]

    if final:
        return buffer, ""

    hold_length = 0
    max_hold = min(len(buffer), len(marker) - 1)
    for length in range(1, max_hold + 1):
        if marker.startswith(buffer[-length:]):
            hold_length = length
    if hold_length == 0:
        return buffer, ""
    return buffer[:-hold_length], buffer[-hold_length:]


gateway = AIGateway()
