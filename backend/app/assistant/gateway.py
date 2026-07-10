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
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import anthropic

from app.core.config import settings

logger = logging.getLogger(__name__)

_INLINE_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(?P<payload>\{.*?\})\s*</tool_call>",
    flags=re.DOTALL,
)


class AssistantUnavailableError(Exception):
    """The AI gateway is not configured or the upstream API failed."""


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


@dataclass(frozen=True)
class OpenAICompatibleRuntime:
    name: str
    base_url: str
    api_key: str | None
    model: str
    timeout: float
    health_timeout: float


class AIGateway:
    def __init__(self) -> None:
        self._anthropic_client: anthropic.Anthropic | None = None

    @property
    def enabled(self) -> bool:
        if settings.assistant_runtime == "anthropic":
            return bool(settings.anthropic_api_key)
        if settings.assistant_runtime == "hermes_agent":
            return hermes_agent_enabled()
        if settings.assistant_runtime == "self_hosted":
            return self_hosted_ai_enabled()
        return False

    @property
    def runtime_healthy(self) -> bool | None:
        if settings.assistant_runtime not in {"hermes_agent", "self_hosted"}:
            return None
        if not self.enabled:
            return False
        runtime = _openai_compatible_runtime(settings.assistant_runtime)
        return openai_compatible_runtime_healthy(runtime)

    def _get_anthropic_client(self) -> anthropic.Anthropic:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")
        if self._anthropic_client is None:
            self._anthropic_client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key
            )
        return self._anthropic_client

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> AICompletion:
        if settings.assistant_runtime == "anthropic":
            return self._complete_anthropic(
                system=system,
                messages=messages,
                tools=tools,
            )
        if settings.assistant_runtime == "hermes_agent":
            return self._complete_hermes_agent(
                system=system,
                messages=messages,
                tools=tools,
            )
        if settings.assistant_runtime == "self_hosted":
            return self._complete_self_hosted(
                system=system,
                messages=messages,
                tools=tools,
            )
        raise AssistantUnavailableError("Assistant runtime is not supported")

    def complete_stream(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> Generator[AITextDelta, None, AICompletion]:
        if settings.assistant_runtime == "anthropic":
            completion = yield from self._complete_stream_anthropic(
                system=system,
                messages=messages,
                tools=tools,
            )
            return completion
        if settings.assistant_runtime == "hermes_agent":
            completion = yield from self._complete_stream_hermes_agent(
                system=system,
                messages=messages,
                tools=tools,
            )
            return completion
        if settings.assistant_runtime == "self_hosted":
            completion = yield from self._complete_stream_self_hosted(
                system=system,
                messages=messages,
                tools=tools,
            )
            return completion
        raise AssistantUnavailableError("Assistant runtime is not supported")

    def _complete_anthropic(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> AICompletion:
        client = self._get_anthropic_client()
        try:
            response = client.messages.create(
                model=settings.assistant_model,
                max_tokens=settings.assistant_max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=_to_anthropic_messages(messages),
                tools=tools,
            )
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
    ) -> Generator[AITextDelta, None, AICompletion]:
        client = self._get_anthropic_client()
        try:
            with client.messages.stream(
                model=settings.assistant_model,
                max_tokens=settings.assistant_max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=_to_anthropic_messages(messages),
                tools=tools,
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
    ) -> AICompletion:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")

        completion = complete_hermes_agent(
            system=system,
            messages=messages,
            tools=tools,
            model=settings.hermes_agent_model,
            max_tokens=settings.assistant_max_tokens,
            timeout=settings.hermes_agent_timeout_seconds,
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
    ) -> Generator[AITextDelta, None, AICompletion]:
        try:
            completion = yield from complete_hermes_agent_stream(
                system=system,
                messages=messages,
                tools=tools,
                model=settings.hermes_agent_model,
                max_tokens=settings.assistant_max_tokens,
                timeout=settings.hermes_agent_timeout_seconds,
                tool_choice="auto",
                log_context="assistant",
            )
        except AssistantUnavailableError:
            completion = complete_hermes_agent(
                system=system,
                messages=messages,
                tools=tools,
                model=settings.hermes_agent_model,
                max_tokens=settings.assistant_max_tokens,
                timeout=settings.hermes_agent_timeout_seconds,
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

    def _complete_self_hosted(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> AICompletion:
        runtime = _openai_compatible_runtime("self_hosted")
        completion = complete_openai_compatible(
            runtime,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=settings.assistant_max_tokens,
            tool_choice="auto",
            log_context="assistant",
        )
        _log_openai_compatible_completion(runtime.name, completion)
        return completion

    def _complete_stream_self_hosted(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> Generator[AITextDelta, None, AICompletion]:
        runtime = _openai_compatible_runtime("self_hosted")
        try:
            completion = yield from complete_openai_compatible_stream(
                runtime,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=settings.assistant_max_tokens,
                tool_choice="auto",
                log_context="assistant",
            )
        except AssistantUnavailableError:
            completion = complete_openai_compatible(
                runtime,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=settings.assistant_max_tokens,
                tool_choice="auto",
                log_context="assistant_same_runtime_non_streaming",
            )
            text = _completion_text_for_delta(completion)
            if text:
                yield AITextDelta(text=text)
        _log_openai_compatible_completion(runtime.name, completion)
        return completion


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENAI_COMPATIBLE_OPENER = urlrequest.build_opener(_NoRedirectHandler())


def _openai_compatible_runtime(name: str) -> OpenAICompatibleRuntime:
    if name == "hermes_agent":
        return OpenAICompatibleRuntime(
            name=name,
            base_url=settings.hermes_agent_base_url,
            api_key=settings.hermes_agent_api_key,
            model=settings.hermes_agent_model,
            timeout=settings.hermes_agent_timeout_seconds,
            health_timeout=settings.hermes_agent_health_timeout_seconds,
        )
    if name == "self_hosted":
        return OpenAICompatibleRuntime(
            name=name,
            base_url=settings.self_hosted_ai_base_url or "",
            api_key=settings.self_hosted_ai_api_key,
            model=settings.self_hosted_ai_model,
            timeout=settings.self_hosted_ai_timeout_seconds,
            health_timeout=settings.self_hosted_ai_health_timeout_seconds,
        )
    raise AssistantUnavailableError("OpenAI-compatible runtime is not supported")


def hermes_agent_enabled() -> bool:
    if not settings.hermes_agent_base_url or not settings.hermes_agent_api_key:
        return False
    return (
        settings.environment != "production"
        or settings.hermes_agent_real_data_allowed
    )


def self_hosted_ai_enabled() -> bool:
    if not settings.self_hosted_ai_base_url or not settings.self_hosted_ai_model:
        return False
    return settings.environment != "production" or bool(
        settings.self_hosted_ai_api_key
    )


def _openai_compatible_runtime_enabled(name: str) -> bool:
    if name == "hermes_agent":
        return hermes_agent_enabled()
    if name == "self_hosted":
        return self_hosted_ai_enabled()
    return False


def hermes_agent_healthy(*, timeout: float) -> bool:
    runtime = _openai_compatible_runtime("hermes_agent")
    return openai_compatible_runtime_healthy(runtime, timeout=timeout)


def openai_compatible_runtime_healthy(
    runtime: OpenAICompatibleRuntime,
    *,
    timeout: float | None = None,
) -> bool:
    request = urlrequest.Request(
        _openai_compatible_url(runtime.base_url, "health"),
        headers=_openai_compatible_headers(runtime.api_key),
        method="GET",
    )
    try:
        with _OPENAI_COMPATIBLE_OPENER.open(
            request,
            timeout=timeout or runtime.health_timeout,
        ) as response:
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
    runtime = _openai_compatible_runtime("hermes_agent")
    runtime = OpenAICompatibleRuntime(
        name=runtime.name,
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        model=model,
        timeout=timeout,
        health_timeout=runtime.health_timeout,
    )
    return complete_openai_compatible(
        runtime,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        tool_choice=tool_choice,
        log_context=log_context,
    )


def complete_openai_compatible(
    runtime: OpenAICompatibleRuntime,
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    tool_choice: str | dict | None,
    log_context: str,
) -> AICompletion:
    if not _openai_compatible_runtime_enabled(runtime.name):
        raise AssistantUnavailableError(
            f"Assistant runtime {runtime.name} is not configured"
        )

    payload: dict[str, Any] = {
        "model": runtime.model,
        "messages": _to_openai_messages(system, messages),
        "max_tokens": max_tokens,
    }
    openai_tools = _to_openai_tools(tools)
    if openai_tools:
        payload["tools"] = openai_tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    request = urlrequest.Request(
        _openai_compatible_url(runtime.base_url, "chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers=_openai_compatible_headers(runtime.api_key),
        method="POST",
    )

    try:
        with _OPENAI_COMPATIBLE_OPENER.open(
            request,
            timeout=runtime.timeout,
        ) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as error:
        logger.error(
            "OpenAI-compatible API error: runtime=%s context=%s status=%s",
            runtime.name,
            log_context,
            error.code,
        )
        raise AssistantUnavailableError("Assistant API request failed") from error
    except (urlerror.URLError, TimeoutError) as error:
        logger.error(
            "OpenAI-compatible API connection error: runtime=%s context=%s",
            runtime.name,
            log_context,
        )
        raise AssistantUnavailableError("Assistant API connection failed") from error
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        logger.error(
            "OpenAI-compatible API returned an invalid response: "
            "runtime=%s context=%s",
            runtime.name,
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error

    try:
        return _from_openai_response(
            response_data,
            fallback_model=runtime.model,
        )
    except (KeyError, TypeError, ValueError) as error:
        logger.error(
            "OpenAI-compatible API returned an invalid response: "
            "runtime=%s context=%s",
            runtime.name,
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
    configured = _openai_compatible_runtime("hermes_agent")
    runtime = OpenAICompatibleRuntime(
        name=configured.name,
        base_url=configured.base_url,
        api_key=configured.api_key,
        model=model,
        timeout=timeout,
        health_timeout=configured.health_timeout,
    )
    completion = yield from complete_openai_compatible_stream(
        runtime,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        tool_choice=tool_choice,
        log_context=log_context,
    )
    return completion


def complete_openai_compatible_stream(
    runtime: OpenAICompatibleRuntime,
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    tool_choice: str | dict | None,
    log_context: str,
) -> Generator[AITextDelta, None, AICompletion]:
    if not _openai_compatible_runtime_enabled(runtime.name):
        raise AssistantUnavailableError(
            f"Assistant runtime {runtime.name} is not configured"
        )

    payload: dict[str, Any] = {
        "model": runtime.model,
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
        _openai_compatible_url(runtime.base_url, "chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers=_openai_compatible_headers(runtime.api_key),
        method="POST",
    )

    content_buffer = ""
    emitted_text = ""
    finish_reason: str | None = None
    response_model = runtime.model
    tool_calls: dict[int, dict] = {}
    try:
        with _OPENAI_COMPATIBLE_OPENER.open(
            request,
            timeout=runtime.timeout,
        ) as response:
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
                    safe_text, content_buffer = _pop_safe_openai_text(content_buffer)
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
            "OpenAI-compatible stream API error: runtime=%s context=%s "
            "status=%s",
            runtime.name,
            log_context,
            error.code,
        )
        raise AssistantUnavailableError("Assistant API request failed") from error
    except (urlerror.URLError, TimeoutError) as error:
        logger.error(
            "OpenAI-compatible stream connection error: runtime=%s context=%s",
            runtime.name,
            log_context,
        )
        raise AssistantUnavailableError("Assistant API connection failed") from error
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        logger.error(
            "OpenAI-compatible stream returned an invalid response: "
            "runtime=%s context=%s",
            runtime.name,
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error

    final_safe_text, held_text = _pop_safe_openai_text(content_buffer, final=True)
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
        return _from_openai_response(
            response_data,
            fallback_model=runtime.model,
        )
    except (KeyError, TypeError, ValueError) as error:
        logger.error(
            "OpenAI-compatible stream returned an invalid response: "
            "runtime=%s context=%s",
            runtime.name,
            log_context,
        )
        raise AssistantUnavailableError(
            "Assistant API returned an invalid response"
        ) from error


def _openai_compatible_url(base_url: str, path: str) -> str:
    normalized_base_url = base_url.rstrip("/")
    clean_path = path.strip("/")
    if clean_path.startswith("health"):
        health_base_url = normalized_base_url.removesuffix("/v1")
        return f"{health_base_url}/{clean_path}"
    if normalized_base_url.endswith("/v1"):
        return f"{normalized_base_url}/{clean_path}"
    return f"{normalized_base_url}/v1/{clean_path}"


def _hermes_agent_url(path: str) -> str:
    return _openai_compatible_url(settings.hermes_agent_base_url, path)


def _hermes_agent_headers() -> dict[str, str]:
    return _openai_compatible_headers(settings.hermes_agent_api_key)


def _openai_compatible_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
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


def _from_openai_response(
    response_data: dict,
    *,
    fallback_model: str | None = None,
) -> AICompletion:
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
        model=(
            response_data.get("model")
            or fallback_model
            or settings.hermes_agent_model
        ),
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


def _log_openai_compatible_completion(
    runtime: str,
    completion: AICompletion,
) -> None:
    logger.info(
        "Assistant completion: runtime=%s model=%s stop_reason=%s "
        "input_tokens=%s output_tokens=%s",
        runtime,
        completion.model,
        completion.stop_reason,
        completion.usage.input_tokens,
        completion.usage.output_tokens,
    )


def _pop_safe_openai_text(buffer: str, *, final: bool = False) -> tuple[str, str]:
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
