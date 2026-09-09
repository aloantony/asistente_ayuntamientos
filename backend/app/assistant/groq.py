"""Explicit Groq chat transport. Only structured, validated tool calls execute.

No provider fallback or content logging. The existing orchestrator owns tool
permissions, confirmation, audit, context limits and cancellation.
"""
import json
import logging
from collections.abc import Generator
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request

from app.assistant.gateway import (
    AICompletion, AITextBlock, AITextDelta, AIToolUseBlock, AIUsage,
    AssistantTimeoutError, AssistantUnavailableError,
    _iter_openai_responses_sse, _to_openai_messages, _to_openai_tools,
)
from app.core.config import settings
from app.core.http import urlopen_without_redirects

logger = logging.getLogger(__name__)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _request(system, messages, tools, *, stream):
    if not settings.groq_api_key:
        raise AssistantUnavailableError("Groq is not configured")
    payload = {
        "model": settings.groq_model,
        "messages": _to_openai_messages(system, messages),
        "max_completion_tokens": settings.assistant_max_tokens,
        "stream": stream,
    }
    if tools:
        payload.update(tools=_to_openai_tools(tools), tool_choice="auto",
                       parallel_tool_calls=False)
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return Request(GROQ_URL, data=json.dumps(payload).encode(), method="POST",
                   headers={"Authorization": f"Bearer {settings.groq_api_key}",
                            "Content-Type": "application/json",
                            "User-Agent": "MiConcejo/0.1",
                            "Accept": "text/event-stream" if stream else "application/json"})


def _completion(message, finish, usage, model):
    if finish not in ("stop", "tool_calls", "length"):
        raise ValueError("missing or unsupported finish reason")
    blocks = []
    text = message.get("content")
    if text is not None and not isinstance(text, str):
        raise ValueError("invalid text content")
    if text:
        blocks.append(AITextBlock(text))
    calls = message.get("tool_calls") or []
    if calls and finish != "tool_calls":
        # Never execute partial arguments after length exhaustion.
        raise ValueError("incomplete tool calls")
    seen = set()
    for call in calls:
        function = call["function"]
        identity, name = call["id"], function["name"]
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("invalid tool identity")
        if not isinstance(name, str) or not name:
            raise ValueError("invalid tool name")
        seen.add(identity)
        args = json.loads(function["arguments"])
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        blocks.append(AIToolUseBlock(identity, name, args))
    if finish == "tool_calls" and not calls:
        raise ValueError("missing tool calls")
    completion = AICompletion(
        model=model or settings.groq_model,
        stop_reason={"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn"}[finish],
        content=blocks,
        usage=AIUsage(usage.get("prompt_tokens"), usage.get("completion_tokens")),
    )
    logger.info("Assistant completion: runtime=groq model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
                completion.model, completion.stop_reason,
                completion.usage.input_tokens, completion.usage.output_tokens)
    return completion


def _raise_transport_error(exc):
    if isinstance(exc, (TimeoutError, AssistantTimeoutError)) or (
        isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)
    ):
        raise AssistantTimeoutError("Assistant request timed out") from None
    if isinstance(exc, HTTPError):
        status = exc.code
        exc.close()
        logger.warning("Assistant API failed: runtime=groq status=%s", status)
        if status == 429:
            raise AssistantUnavailableError("Groq quota exceeded; retry later") from None
    else:
        logger.warning("Assistant API failed: runtime=groq error_type=%s", type(exc).__name__)
    raise AssistantUnavailableError("Groq request failed") from None


_ERRORS = (HTTPError, URLError, TimeoutError, HTTPException, OSError,
           ValueError, KeyError, IndexError, TypeError, AssistantTimeoutError)


def complete_groq(*, system, messages, tools, timeout) -> AICompletion:
    # Use the same bounded streaming reader for both entry points: its deadline
    # is absolute, including slow peers that keep a connection alive.
    stream = complete_groq_stream(system=system, messages=messages, tools=tools,
                                  timeout=timeout)
    try:
        while True:
            next(stream)
    except StopIteration as stop:
        return stop.value
    finally:
        stream.close()


def complete_groq_stream(*, system, messages, tools, timeout) -> Generator[AITextDelta, None, AICompletion]:
    request = _request(system, messages, tools, stream=True)
    deadline = monotonic() + timeout
    text_parts = []
    calls = {}
    finish = None
    usage = {}
    model = settings.groq_model
    try:
        with urlopen_without_redirects(request, timeout=timeout) as response:
            for event in _iter_openai_responses_sse(response, deadline=deadline):
                if event.get("error"):
                    raise ValueError("upstream stream error")
                model = event.get("model") or model
                usage = event.get("usage") or (event.get("x_groq") or {}).get("usage") or usage
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if finish is not None and (delta.get("content") or delta.get("tool_calls")):
                    raise ValueError("content after terminal event")
                content = delta.get("content")
                if content:
                    if not isinstance(content, str):
                        raise ValueError("invalid text delta")
                    text_parts.append(content)
                    yield AITextDelta(content)
                for fragment in delta.get("tool_calls") or []:
                    index = fragment["index"]
                    if not isinstance(index, int) or not 0 <= index < 64:
                        raise ValueError("invalid tool index")
                    call = calls.setdefault(index, {"id": "", "function": {"name": "", "arguments": ""}})
                    if fragment.get("id"):
                        call["id"] += fragment["id"]
                    function = fragment.get("function") or {}
                    for key in ("name", "arguments"):
                        if function.get(key):
                            call["function"][key] += function[key]
                if choice.get("finish_reason") is not None:
                    finish = choice["finish_reason"]
        return _completion({"content": "".join(text_parts),
                            "tool_calls": [calls[i] for i in sorted(calls)]},
                           finish, usage, model)
    except _ERRORS as exc:
        _raise_transport_error(exc)
