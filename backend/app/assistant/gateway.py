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


class AIGateway:
    def __init__(self) -> None:
        self._anthropic_client: anthropic.Anthropic | None = None

    @property
    def enabled(self) -> bool:
        if settings.assistant_runtime == "anthropic":
            return bool(settings.anthropic_api_key)
        if settings.assistant_runtime == "hermes_agent":
            if not settings.hermes_agent_base_url or not settings.hermes_agent_api_key:
                return False
            return (
                settings.environment != "production"
                or settings.hermes_agent_real_data_allowed
            )
        return False

    @property
    def runtime_healthy(self) -> bool | None:
        if settings.assistant_runtime != "hermes_agent":
            return None
        if not self.enabled:
            return False

        request = urlrequest.Request(
            _hermes_agent_url("health"),
            headers=_hermes_agent_headers(),
            method="GET",
        )
        try:
            with urlrequest.urlopen(
                request,
                timeout=settings.hermes_agent_health_timeout_seconds,
            ) as response:
                return 200 <= response.status < 300
        except (urlerror.HTTPError, urlerror.URLError, TimeoutError):
            return False

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

    def _complete_hermes_agent(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> AICompletion:
        if not self.enabled:
            raise AssistantUnavailableError("Assistant is not configured")

        payload: dict[str, Any] = {
            "model": settings.hermes_agent_model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": settings.assistant_max_tokens,
        }
        openai_tools = _to_openai_tools(tools)
        if openai_tools:
            payload["tools"] = openai_tools
            payload["tool_choice"] = "auto"

        request = urlrequest.Request(
            _hermes_agent_url("chat/completions"),
            data=json.dumps(payload).encode("utf-8"),
            headers=_hermes_agent_headers(),
            method="POST",
        )

        try:
            with urlrequest.urlopen(
                request,
                timeout=settings.hermes_agent_timeout_seconds,
            ) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urlerror.HTTPError as error:
            logger.error("Hermes Agent API error: status=%s", error.code)
            raise AssistantUnavailableError("Assistant API request failed") from error
        except (urlerror.URLError, TimeoutError) as error:
            logger.error("Hermes Agent API connection error")
            raise AssistantUnavailableError(
                "Assistant API connection failed"
            ) from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            logger.error("Hermes Agent API returned an invalid response")
            raise AssistantUnavailableError(
                "Assistant API returned an invalid response"
            ) from error

        try:
            completion = _from_openai_response(response_data)
        except (KeyError, TypeError, ValueError) as error:
            logger.error("Hermes Agent API returned an invalid response")
            raise AssistantUnavailableError(
                "Assistant API returned an invalid response"
            ) from error
        logger.info(
            "Assistant completion: runtime=hermes_agent model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            completion.model,
            completion.stop_reason,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )
        return completion


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

    content: list[AITextBlock | AIToolUseBlock] = []
    if cleaned_content:
        content.append(AITextBlock(text=cleaned_content))

    for tool_call in message.get("tool_calls") or []:
        parsed_tool_call = _parse_openai_tool_call(tool_call)
        if parsed_tool_call is not None:
            content.append(parsed_tool_call)

    content.extend(inline_tool_calls)
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


gateway = AIGateway()
