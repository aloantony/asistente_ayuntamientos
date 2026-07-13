"""Model-first turn engine for the municipal assistant."""

import json
import logging
import re
import uuid
from collections.abc import Generator
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant.gateway import (
    AICompletion,
    AITextDelta,
    AIToolUseBlock,
    AIUsage,
    AIGateway,
    AssistantUnavailableError,
)
from app.assistant.guards import (
    CONFIRMATION_REQUIRED_TOOLS,
    ConfirmationReference,
    ConfirmationToolResult,
    build_confirmation_prompt,
    check_tool_confirmation,
    finalize_confirmation_turn,
    lock_conversation_for_confirmation,
    process_pending_confirmation_response,
)
from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import (
    ERROR_REPLY,
    FALLBACK_REPLY,
    REFUSAL_REPLY,
    build_system_prompt,
)
from app.assistant.tools import (
    ToolContext,
    ToolResult,
    ToolSpec,
    execute_tool,
    get_available_tool_specs,
)
from app.core.config import settings
from app.users.models import User

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 4000
STALE_MUTATING_TOOL_RESULT = (
    "No se ejecutó la herramienta porque este turno quedó desactualizado por "
    "un mensaje posterior del usuario."
)
TOKEN_PATTERN = re.compile(r"[a-záéíóúüñ0-9]+", re.IGNORECASE)
TOOL_INTENT_STOPWORDS = {
    "a",
    "al",
    "and",
    "antes",
    "as",
    "by",
    "como",
    "con",
    "cuando",
    "de",
    "del",
    "el",
    "en",
    "for",
    "la",
    "las",
    "lo",
    "los",
    "no",
    "o",
    "of",
    "on",
    "or",
    "para",
    "por",
    "que",
    "se",
    "si",
    "sin",
    "the",
    "to",
    "un",
    "una",
    "unas",
    "unos",
    "usa",
    "usuario",
    "visible",
    "visibles",
    "y",
}


@dataclass(frozen=True)
class TurnEvent:
    type: str
    data: dict


def run_agent_turn_events(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_text: str,
    gateway: AIGateway,
    *,
    input_mode: str = "text",
) -> Generator[TurnEvent, None, AssistantMessage]:
    """Persist the user message, run the tool loop and stream turn events."""
    # Lock before inserting the message: concurrent FK inserts followed by a
    # row-lock upgrade can deadlock. The first commit releases this short lock.
    conversation = lock_conversation_for_confirmation(db, conversation.id)
    # Imported lazily because realtime orchestration reuses this module's
    # history and tool-loop helpers.
    from app.assistant.realtime import seal_active_realtime_turn

    seal_active_realtime_turn(db, conversation)
    user_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content=user_text,
    )
    db.add(user_message)
    if conversation.title == "Conversación":
        conversation.title = user_text[:255]
    conversation.updated_at = func.now()
    db.flush()
    confirmation_context = process_pending_confirmation_response(
        db,
        conversation,
        user_message,
    )
    db.commit()
    db.refresh(user_message)
    db.refresh(conversation)

    yield TurnEvent(
        "message_start",
        {
            "conversation_id": conversation.id,
            "user_message_id": user_message.id,
        },
    )

    tools = get_available_tool_specs(db, current_user)
    tools_by_name = {tool.name: tool for tool in tools}
    tool_definitions = [tool.definition for tool in tools]
    tool_names = frozenset(tool.name for tool in tools)
    system = build_system_prompt(db, current_user, tools, input_mode=input_mode)
    messages = build_history(conversation)

    actions: list[dict] = []
    reply_text = ""
    try:
        response = yield from _complete_with_events(
            gateway,
            system=system,
            messages=messages,
            tools=tool_definitions,
        )

        for _ in range(settings.assistant_max_tool_iterations):
            response = recover_textual_read_tool_call(response, tools, messages)

            if response.stop_reason == "refusal":
                reply_text = REFUSAL_REPLY
                break

            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                response = yield from _complete_with_events(
                    gateway,
                    system=system,
                    messages=messages,
                    tools=tool_definitions,
                )
                continue

            if response.stop_reason != "tool_use":
                reply_text = sanitize_model_reply(extract_text(response.content))
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_input = dict(block.input)
                yield TurnEvent(
                    "tool_activity",
                    {
                        "tool": block.name,
                        "status": "started",
                        "input": tool_input,
                    },
                )
                guarded_result = check_tool_confirmation(
                    db,
                    conversation,
                    user_message,
                    block.name,
                    tool_input,
                )
                if block.name in CONFIRMATION_REQUIRED_TOOLS:
                    required_confirmation = _confirmation_context_from_result(
                        guarded_result
                    )
                    if required_confirmation is not None:
                        confirmation_context = required_confirmation
                result = guarded_result or _execute_tool_for_current_turn(
                    db=db,
                    current_user=current_user,
                    conversation=conversation,
                    user_message=user_message,
                    tool=tools_by_name.get(block.name),
                    tool_name=block.name,
                    tool_input=tool_input,
                    context=ToolContext(
                        conversation_id=conversation.id,
                        user_message_id=user_message.id,
                    ),
                    allowed=tool_names,
                )
                action = {
                    "tool": block.name,
                    "ok": result.ok,
                    "input": tool_input,
                    "result": result.content[:MAX_TOOL_RESULT_CHARS],
                }
                actions.append(action)
                yield TurnEvent(
                    "tool_activity",
                    {
                        "tool": block.name,
                        "status": "finished",
                        "input": tool_input,
                        "ok": result.ok,
                        "result": action["result"],
                    },
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.content,
                        "is_error": not result.ok,
                    }
                )

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            response = yield from _complete_with_events(
                gateway,
                system=system,
                messages=messages,
                tools=tool_definitions,
            )
        else:
            logger.warning(
                "Assistant hit the tool iteration limit (conversation=%s)",
                conversation.id,
            )
            reply_text = sanitize_model_reply(extract_text(response.content))
    except AssistantUnavailableError:
        logger.warning(
            "Assistant gateway failed mid-turn (conversation=%s)",
            conversation.id,
        )
        reply_text = ERROR_REPLY

    if not reply_text:
        reply_text = FALLBACK_REPLY

    # Lock before inserting the assistant message to keep the same lock order
    # as user-message insertion and avoid FK/row-lock deadlocks.
    conversation = lock_conversation_for_confirmation(db, conversation.id)
    confirmation_prompt = None
    if reply_text not in {ERROR_REPLY, FALLBACK_REPLY, REFUSAL_REPLY}:
        confirmation_prompt = build_confirmation_prompt(
            conversation,
            confirmation_context,
            input_mode=input_mode,
            turn_user_message_id=user_message.id,
        )
    if confirmation_prompt:
        confirmation_suffix = f"\n\n{confirmation_prompt}"
        reply_text = f"{reply_text.rstrip()}{confirmation_suffix}"
        yield TurnEvent("text_delta", {"text": confirmation_suffix})
    assistant_message = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content=reply_text,
        actions=json.dumps(actions, ensure_ascii=False) if actions else None,
        agent_key="anacleto",
        routing=None,
    )
    db.add(assistant_message)
    conversation.updated_at = func.now()
    db.flush()
    finalize_confirmation_turn(
        conversation,
        user_message,
        assistant_message,
        confirmation_context,
        confirmation_prompt=confirmation_prompt,
    )
    db.commit()
    db.refresh(assistant_message)
    db.refresh(conversation)

    yield TurnEvent(
        "done",
        {
            "message": _message_payload(assistant_message),
            "conversation": {
                "id": conversation.id,
                "title": conversation.title,
                "status": conversation.status,
                "folder_id": conversation.folder_id,
                "created_at": conversation.created_at.isoformat(),
                "updated_at": conversation.updated_at.isoformat(),
            },
        },
    )
    return assistant_message


def _execute_tool_for_current_turn(
    *,
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool: ToolSpec | None,
    tool_name: str,
    tool_input: dict,
    context: ToolContext,
    allowed: frozenset[str],
) -> ToolResult:
    if tool is None or tool.read_only:
        return execute_tool(
            db,
            current_user,
            tool_name,
            tool_input,
            context,
            allowed=allowed,
        )

    lock_conversation_for_confirmation(db, conversation.id)
    latest_user_message_id = db.scalar(
        select(AssistantMessage.id)
        .where(
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.role == "user",
        )
        .order_by(AssistantMessage.id.desc())
        .limit(1)
    )
    if latest_user_message_id != user_message.id:
        db.commit()
        return ToolResult(content=STALE_MUTATING_TOOL_RESULT, ok=False)

    # Mutating executors commit or roll back their own transaction. Calling the
    # executor while this row lock is held makes the latest-turn check atomic
    # with the mutation.
    return execute_tool(
        db,
        current_user,
        tool_name,
        tool_input,
        context,
        allowed=allowed,
    )


def _confirmation_context_from_result(
    result: ConfirmationToolResult | None,
) -> ConfirmationReference | None:
    if (
        isinstance(result, ConfirmationToolResult)
        and result.status == "required"
    ):
        return result.confirmation
    return None


def run_agent_turn(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_text: str,
    gateway: AIGateway,
    *,
    input_mode: str = "text",
) -> AssistantMessage:
    events = run_agent_turn_events(
        db,
        current_user,
        conversation,
        user_text,
        gateway,
        input_mode=input_mode,
    )
    while True:
        try:
            next(events)
        except StopIteration as stop:
            return stop.value


def build_history(conversation: AssistantConversation) -> list[dict]:
    messages = [
        {"role": message.role, "content": message.content}
        for message in conversation.messages
        if message.content
    ]
    max_messages = max(2, settings.assistant_history_max_messages)
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]


def extract_text(content_blocks) -> str:
    return "\n\n".join(
        block.text for block in content_blocks if block.type == "text"
    ).strip()


def sanitize_model_reply(text: str) -> str:
    stripped = text.strip()
    technical_markers = (
        "No reply:",
        "the model returned empty content",
        "the model returned empty content after retries",
    )
    if any(marker in stripped for marker in technical_markers):
        return ""
    return stripped


def recover_textual_read_tool_call(
    response: AICompletion,
    tools: list[ToolSpec],
    messages: list[dict],
) -> AICompletion:
    if response.stop_reason == "tool_use":
        return response

    text = extract_text(response.content)
    arguments = extract_prefix_json_object(text)
    if not arguments:
        return response

    tool = infer_read_only_tool_from_arguments(
        arguments=arguments,
        tools=tools,
        messages=messages,
    )
    if tool is None:
        return response

    logger.info(
        "Recovered textual read-only tool call from AI response: tool=%s",
        tool.name,
    )
    return AICompletion(
        model=getattr(response, "model", "assistant"),
        stop_reason="tool_use",
        content=[
            AIToolUseBlock(
                id=f"call_{uuid.uuid4().hex}",
                name=tool.name,
                input=arguments,
            )
        ],
        usage=getattr(response, "usage", AIUsage()),
    )


def extract_prefix_json_object(text: str) -> dict | None:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None

    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(stripped):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[: index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def infer_read_only_tool_from_arguments(
    *,
    arguments: dict,
    tools: list[ToolSpec],
    messages: list[dict],
) -> ToolSpec | None:
    if not arguments:
        return None

    context_tokens = tokenize_tool_intent(" ".join(recent_text_messages(messages)))
    if not context_tokens:
        return None

    scored_candidates: list[tuple[int, ToolSpec]] = []
    for tool in tools:
        if not tool.read_only or not tool_accepts_arguments(tool, arguments):
            continue
        tool_tokens = tokenize_tool_intent(
            f"{tool.name.replace('_', ' ')} {tool.label} {tool.description}"
        )
        score = len(context_tokens.intersection(tool_tokens))
        if score > 0:
            scored_candidates.append((score, tool))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_tool = scored_candidates[0]
    if len(scored_candidates) > 1 and scored_candidates[1][0] == best_score:
        return None
    return best_tool


def recent_text_messages(messages: list[dict]) -> list[str]:
    texts: list[str] = []
    for message in messages[-8:]:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
    return texts


def tokenize_tool_intent(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw_token in TOKEN_PATTERN.findall(text.lower()):
        if raw_token in TOOL_INTENT_STOPWORDS:
            continue
        tokens.add(raw_token)
        if len(raw_token) > 4 and raw_token.endswith("es"):
            tokens.add(raw_token[:-2])
        if len(raw_token) > 4 and raw_token.endswith("s"):
            tokens.add(raw_token[:-1])
    return tokens


def tool_accepts_arguments(tool: ToolSpec, arguments: dict) -> bool:
    input_schema = tool.input_schema or {}
    properties = set((input_schema.get("properties") or {}).keys())
    required = set(input_schema.get("required") or [])
    argument_keys = set(arguments.keys())
    return required.issubset(argument_keys) and argument_keys.issubset(properties)


def _complete_with_events(
    gateway: AIGateway,
    *,
    system: str,
    messages: list[dict],
    tools: list[dict],
) -> Generator[TurnEvent, None, AICompletion]:
    complete_stream = getattr(gateway, "complete_stream", None)
    if complete_stream is None:
        completion = gateway.complete(system=system, messages=messages, tools=tools)
        text = sanitize_model_reply(extract_text(completion.content))
        if text:
            yield TurnEvent("text_delta", {"text": text})
        return completion

    stream = complete_stream(system=system, messages=messages, tools=tools)
    while True:
        try:
            delta = next(stream)
        except StopIteration as stop:
            return stop.value
        text = getattr(delta, "text", "")
        if text:
            yield TurnEvent("text_delta", {"text": text})


def _message_payload(message: AssistantMessage) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "actions": json.loads(message.actions) if message.actions else [],
        "agent_key": message.agent_key,
        "routing": json.loads(message.routing) if message.routing else None,
        "created_at": message.created_at.isoformat(),
    }
