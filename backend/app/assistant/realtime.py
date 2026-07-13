"""Realtime voice orchestration for Anacleto."""

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib import error as urlerror
from urllib import request as urlrequest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant.guards import (
    ConfirmationReference,
    ConfirmationToolResult,
    build_confirmation_prompt,
    check_tool_confirmation,
    dump_conversation_state,
    finalize_confirmation_turn,
    load_conversation_state,
    lock_conversation_for_confirmation,
    process_pending_confirmation_response,
)
from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import build_system_prompt
from app.assistant.schemas import (
    AssistantRealtimeToolCallCreate,
    AssistantRealtimeTurnCreate,
    AssistantRealtimeTurnStartCreate,
)
from app.assistant.tools import ToolContext, execute_tool, get_available_tool_specs
from app.assistant.turn import MAX_TOOL_RESULT_CHARS, build_history
from app.core.config import settings
from app.users.models import User

logger = logging.getLogger(__name__)

REALTIME_STATE_KEY = "realtime_voice"
MAX_RECENT_REALTIME_TURNS = 4
MAX_REALTIME_TOOL_CALLS = 16
MAX_REALTIME_RESPONSES = 4
MAX_REALTIME_TOOL_ARGUMENT_BYTES = 20_000
MAX_REALTIME_COMBINED_TEXT_CHARS = 40_000
MAX_SEALED_REALTIME_ACTION_CONTEXT_CHARS = 900
REALTIME_TOOL_CALL_LEASE_SECONDS = 120
INDETERMINATE_TOOL_RESULT = (
    "No se pudo confirmar si la herramienta terminó. Revisa el sistema antes "
    "de repetir la acción."
)


class AssistantRealtimeUnavailableError(Exception):
    """Realtime voice is not configured or the upstream request failed."""


class AssistantRealtimeConflictError(Exception):
    """A realtime request conflicts with the server-owned turn state."""


def realtime_voice_enabled() -> bool:
    return (
        settings.assistant_realtime_enabled
        and bool(settings.openai_api_key)
        and bool(settings.assistant_realtime_transcription_model.strip())
    )


def create_realtime_client_secret(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
) -> dict:
    if not realtime_voice_enabled():
        raise AssistantRealtimeUnavailableError("Realtime voice is not configured")

    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    seal_active_realtime_turn(
        db,
        locked_conversation,
        status="abandoned",
    )
    payload = build_realtime_client_secret_payload(
        db,
        current_user,
        locked_conversation,
    )
    db.commit()
    request = urlrequest.Request(
        settings.assistant_realtime_client_secret_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": _safety_identifier(current_user),
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(
            request,
            timeout=settings.assistant_realtime_timeout_seconds,
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as error:
        logger.error(
            "OpenAI Realtime client secret request failed: status=%s",
            error.code,
        )
        raise AssistantRealtimeUnavailableError(
            "Realtime voice request failed"
        ) from error
    except (urlerror.URLError, TimeoutError) as error:
        logger.error("OpenAI Realtime client secret request failed")
        raise AssistantRealtimeUnavailableError(
            "Realtime voice connection failed"
        ) from error
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        logger.error("OpenAI Realtime client secret response was invalid")
        raise AssistantRealtimeUnavailableError(
            "Realtime voice returned an invalid response"
        ) from error

    client_secret = data.get("value")
    if not isinstance(client_secret, str) or not client_secret:
        raise AssistantRealtimeUnavailableError(
            "Realtime voice returned no client secret"
        )
    return data


def build_realtime_client_secret_payload(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
) -> dict:
    tools = get_available_tool_specs(db, current_user)
    realtime_tools = [_to_realtime_tool(tool.definition) for tool in tools]
    input_audio = {
        "turn_detection": {
            "type": "server_vad",
            "threshold": settings.assistant_realtime_vad_threshold,
            "prefix_padding_ms": settings.assistant_realtime_vad_prefix_padding_ms,
            "silence_duration_ms": settings.assistant_realtime_vad_silence_duration_ms,
            "create_response": False,
            "interrupt_response": True,
        },
    }
    transcription = _transcription_config()
    if transcription:
        input_audio["transcription"] = transcription

    session: dict = {
        "type": "realtime",
        "model": settings.assistant_realtime_model,
        "instructions": _build_realtime_instructions(
            db,
            current_user,
            conversation,
            tools,
        ),
        "output_modalities": ["audio"],
        "audio": {
            "input": input_audio,
            "output": {"voice": settings.assistant_realtime_voice},
        },
    }
    if realtime_tools:
        session["tools"] = realtime_tools
        session["tool_choice"] = "auto"

    return {"session": session}


def start_realtime_turn(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    payload: AssistantRealtimeTurnStartCreate,
) -> tuple[AssistantMessage, bool]:
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state, realtime_state = _load_realtime_state(locked_conversation)
    user_text_digest = _text_digest(payload.user_text)
    existing = _find_realtime_turn(realtime_state, payload.turn_id)
    if existing is not None:
        if existing.get("user_text_digest") != user_text_digest:
            raise AssistantRealtimeConflictError(
                "Realtime turn was already started with different text"
            )
        user_message = _get_realtime_user_message(
            db,
            locked_conversation,
            existing,
        )
        db.commit()
        return user_message, True

    active_turn = realtime_state.get("active_turn")
    if isinstance(active_turn, dict):
        _seal_active_realtime_turn(
            db,
            locked_conversation,
            state,
            realtime_state,
            status="superseded",
        )
        state, realtime_state = _load_realtime_state(locked_conversation)

    user_message = AssistantMessage(
        conversation=locked_conversation,
        role="user",
        content=payload.user_text,
    )
    db.add(user_message)
    if locked_conversation.title == "Conversación":
        locked_conversation.title = payload.user_text[:255]
    locked_conversation.updated_at = func.now()
    db.flush()

    confirmation_context = process_pending_confirmation_response(
        db,
        locked_conversation,
        user_message,
    )
    state, realtime_state = _load_realtime_state(locked_conversation)
    realtime_state["active_turn"] = {
        "turn_id": payload.turn_id,
        "user_message_id": user_message.id,
        "user_text_digest": user_text_digest,
        "status": "open",
        "confirmation": _serialize_confirmation_reference(confirmation_context),
        "call_order": [],
        "calls": {},
        "response_order": [],
        "responses": {},
    }
    _trim_recent_realtime_turns(realtime_state)
    state[REALTIME_STATE_KEY] = realtime_state
    dump_conversation_state(locked_conversation, state)
    db.commit()
    db.refresh(user_message)
    db.refresh(locked_conversation)
    return user_message, False


def seal_active_realtime_turn(
    db: Session,
    locked_conversation: AssistantConversation,
    *,
    status: str = "superseded",
) -> None:
    """Seal realtime work while the caller holds the conversation row lock."""
    state, realtime_state = _load_realtime_state(locked_conversation)
    if not isinstance(realtime_state.get("active_turn"), dict):
        return
    _seal_active_realtime_turn(
        db,
        locked_conversation,
        state,
        realtime_state,
        status=status,
    )


def execute_realtime_tool_call(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    turn_id: str,
    payload: AssistantRealtimeToolCallCreate,
) -> tuple[dict, str, AssistantMessage, str | None, bool]:
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state, realtime_state = _load_realtime_state(locked_conversation)
    turn = _find_realtime_turn(realtime_state, turn_id)
    if turn is None:
        raise AssistantRealtimeConflictError("Realtime turn does not exist")

    user_message = _get_realtime_user_message(db, locked_conversation, turn)
    request_digest = _tool_call_digest(payload.name, payload.arguments)
    tool_input = dict(payload.arguments or {})
    calls = _turn_calls(turn)
    recovered_expired_call = _recover_expired_realtime_tool_calls(turn)
    existing = calls.get(payload.call_id)
    if isinstance(existing, dict):
        if existing.get("request_digest") != request_digest:
            raise AssistantRealtimeConflictError(
                "Realtime call ID was reused with a different payload"
            )
        if recovered_expired_call:
            state[REALTIME_STATE_KEY] = realtime_state
            dump_conversation_state(locked_conversation, state)
            db.commit()
        if existing.get("status") not in {"finished", "indeterminate"}:
            raise AssistantRealtimeConflictError(
                "Realtime tool call is already in progress"
            )
        action = _stored_action(existing)
        output = str(existing.get("output") or "")
        confirmation_prompt = _optional_string(existing.get("confirmation_prompt"))
        db.commit()
        return action, output, user_message, confirmation_prompt, True

    if recovered_expired_call:
        state[REALTIME_STATE_KEY] = realtime_state
        dump_conversation_state(locked_conversation, state)
        db.commit()
        raise AssistantRealtimeConflictError(
            "Realtime turn has an indeterminate tool result and must be closed"
        )

    if realtime_state.get("active_turn") is not turn or turn.get("status") != "open":
        raise AssistantRealtimeConflictError("Realtime turn is already complete")
    if any(
        isinstance(call, dict) and call.get("status") == "indeterminate"
        for call in calls.values()
    ):
        raise AssistantRealtimeConflictError(
            "Realtime turn has an indeterminate tool result and must be closed"
        )
    if len(calls) >= MAX_REALTIME_TOOL_CALLS:
        raise AssistantRealtimeConflictError("Realtime turn has too many tool calls")
    if any(
        isinstance(call, dict) and call.get("status") == "started"
        for call in calls.values()
    ):
        raise AssistantRealtimeConflictError(
            "Realtime tool calls must be executed sequentially"
        )

    calls[payload.call_id] = {
        "request_digest": request_digest,
        "status": "started",
        "name": payload.name,
        "input": deepcopy(tool_input),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    turn.setdefault("call_order", []).append(payload.call_id)
    state[REALTIME_STATE_KEY] = realtime_state
    dump_conversation_state(locked_conversation, state)
    db.commit()

    try:
        tools = get_available_tool_specs(db, current_user)
        allowed_tool_names = frozenset(tool.name for tool in tools)
        guarded_result = check_tool_confirmation(
            db,
            conversation,
            user_message,
            payload.name,
            tool_input,
        )
        result = guarded_result or execute_tool(
            db,
            current_user,
            payload.name,
            tool_input,
            ToolContext(
                conversation_id=conversation.id,
                user_message_id=user_message.id,
            ),
            allowed=allowed_tool_names,
        )
        action = {
            "call_id": payload.call_id,
            "tool": payload.name,
            "ok": result.ok,
            "input": tool_input,
            "result": result.content[:MAX_TOOL_RESULT_CHARS],
        }

        confirmation_context = _confirmation_context_from_result(guarded_result)
        locked_conversation = lock_conversation_for_confirmation(
            db, conversation.id
        )
        state, realtime_state = _load_realtime_state(locked_conversation)
        turn = _find_realtime_turn(realtime_state, turn_id)
        if turn is None:
            raise AssistantRealtimeConflictError("Realtime turn state was lost")
        calls = _turn_calls(turn)
        stored_call = calls.get(payload.call_id)
        if not isinstance(stored_call, dict):
            raise AssistantRealtimeConflictError(
                "Realtime tool call state was lost"
            )
        if stored_call.get("status") != "started":
            raise AssistantRealtimeConflictError(
                "Realtime tool call is no longer active"
            )

        if confirmation_context is not None:
            turn["confirmation"] = _serialize_confirmation_reference(
                confirmation_context
            )
        confirmation_prompt = build_confirmation_prompt(
            locked_conversation,
            confirmation_context,
            input_mode="voice",
            turn_user_message_id=user_message.id,
        )
        output = result.content[:MAX_TOOL_RESULT_CHARS]
        if confirmation_prompt:
            output = json.dumps(
                {"status": "confirmation_required"},
                ensure_ascii=False,
            )

        stored_call.update(
            {
                "status": "finished",
                "action": action,
                "output": output,
                "confirmation_prompt": confirmation_prompt,
            }
        )
        locked_conversation.updated_at = func.now()
        state[REALTIME_STATE_KEY] = realtime_state
        dump_conversation_state(locked_conversation, state)
        db.commit()
        db.refresh(user_message)
        db.refresh(locked_conversation)
        return action, output, user_message, confirmation_prompt, False
    except Exception:
        logger.exception(
            "Realtime tool call ended with an indeterminate result: call_id=%s",
            payload.call_id,
        )
        db.rollback()
        action, output, user_message = _mark_realtime_tool_call_indeterminate(
            db,
            conversation,
            turn_id,
            payload,
            tool_input,
        )
        return action, output, user_message, None, False


def persist_realtime_turn(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    turn_id: str,
    payload: AssistantRealtimeTurnCreate,
) -> tuple[AssistantMessage, AssistantMessage | None, str | None, bool, bool]:
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state, realtime_state = _load_realtime_state(locked_conversation)
    turn = _find_realtime_turn(realtime_state, turn_id)
    if turn is None:
        raise AssistantRealtimeConflictError("Realtime turn does not exist")

    user_message = _get_realtime_user_message(db, locked_conversation, turn)
    latest_user_message_id = db.scalar(
        select(AssistantMessage.id)
        .where(
            AssistantMessage.conversation_id == locked_conversation.id,
            AssistantMessage.role == "user",
        )
        .order_by(AssistantMessage.id.desc())
        .limit(1)
    )
    if latest_user_message_id != user_message.id:
        if realtime_state.get("active_turn") is turn:
            _seal_active_realtime_turn(
                db,
                locked_conversation,
                state,
                realtime_state,
                status="stale",
            )
            db.commit()
        raise AssistantRealtimeConflictError(
            "Realtime turn was superseded by a newer user message"
        )
    if turn.get("status") not in {"open", "completed"}:
        raise AssistantRealtimeConflictError("Realtime turn is no longer active")

    response_digest = _response_digest(payload)
    responses = _turn_responses(turn)
    existing_response = responses.get(payload.response_id)
    replayed = False
    if turn.get("status") == "completed":
        if (
            not isinstance(existing_response, dict)
            or existing_response.get("response_digest") != response_digest
        ):
            raise AssistantRealtimeConflictError(
                "Realtime turn is already complete"
            )
        assistant_message = _get_realtime_assistant_message(
            db,
            locked_conversation,
            turn,
        )
        db.commit()
        return (
            user_message,
            assistant_message,
            _optional_string(turn.get("confirmation_prompt")),
            False,
            True,
        )

    if isinstance(existing_response, dict):
        if existing_response.get("response_digest") != response_digest:
            raise AssistantRealtimeConflictError(
                "Realtime response ID was reused with different content"
            )
        replayed = True
    else:
        if len(responses) >= MAX_REALTIME_RESPONSES:
            raise AssistantRealtimeConflictError(
                "Realtime turn has too many responses"
            )
        assistant_text = (payload.assistant_text or "").strip()
        combined_text_chars = sum(
            len(_optional_string(response.get("assistant_text")) or "")
            for response in responses.values()
            if isinstance(response, dict)
        )
        if (
            combined_text_chars + len(assistant_text)
            > MAX_REALTIME_COMBINED_TEXT_CHARS
        ):
            raise AssistantRealtimeConflictError(
                "Realtime turn response text is too large"
            )
        responses[payload.response_id] = {
            "response_digest": response_digest,
            "status": payload.response_status,
            "assistant_text": assistant_text,
            "interrupted": payload.interrupted,
        }
        turn.setdefault("response_order", []).append(payload.response_id)

    calls = _turn_calls(turn)
    _recover_expired_realtime_tool_calls(turn)
    if any(
        isinstance(call, dict) and call.get("status") == "started"
        for call in calls.values()
    ):
        raise AssistantRealtimeConflictError(
            "Realtime tool call is still in progress"
        )

    confirmation_context = _deserialize_confirmation_reference(
        turn.get("confirmation")
    )
    confirmation_prompt = build_confirmation_prompt(
        locked_conversation,
        confirmation_context,
        input_mode="voice",
        turn_user_message_id=user_message.id,
    )
    assistant_text = _combined_realtime_response_text(turn)
    response_completed = (
        payload.response_status == "completed" and not payload.interrupted
    )
    prompt_was_delivered = bool(
        confirmation_prompt
        and response_completed
        and _contains_exact_confirmation_prompt(
            (payload.assistant_text or "").strip(),
            confirmation_prompt,
        )
    )

    if confirmation_prompt and response_completed and not prompt_was_delivered:
        state[REALTIME_STATE_KEY] = realtime_state
        dump_conversation_state(locked_conversation, state)
        db.commit()
        return user_message, None, confirmation_prompt, True, replayed

    if not assistant_text:
        assistant_text = (
            "Respuesta interrumpida."
            if payload.interrupted or payload.response_status != "completed"
            else "Respuesta de voz sin transcripción."
        )
    if prompt_was_delivered and not assistant_text.rstrip().endswith(
        confirmation_prompt
    ):
        assistant_text = f"{assistant_text.rstrip()}\n\n{confirmation_prompt}"

    actions = _finished_realtime_actions(turn)
    if actions and (payload.interrupted or payload.response_status != "completed"):
        action_context = _sealed_realtime_action_context(turn)
        if action_context:
            assistant_text = f"{assistant_text.rstrip()}\n\n{action_context}"
    assistant_message = AssistantMessage(
        conversation=locked_conversation,
        role="assistant",
        content=assistant_text,
        actions=json.dumps(actions, ensure_ascii=False) if actions else None,
        agent_key="anacleto",
        routing=None,
    )
    db.add(assistant_message)
    locked_conversation.updated_at = func.now()
    db.flush()
    turn["assistant_message_id"] = assistant_message.id
    turn["confirmation_prompt"] = confirmation_prompt
    _archive_realtime_turn(realtime_state, turn, status="completed")
    state[REALTIME_STATE_KEY] = realtime_state
    dump_conversation_state(locked_conversation, state)
    finalize_confirmation_turn(
        locked_conversation,
        user_message,
        assistant_message,
        confirmation_context,
        confirmation_prompt=(
            confirmation_prompt if prompt_was_delivered else None
        ),
    )
    db.commit()
    db.refresh(user_message)
    db.refresh(assistant_message)
    db.refresh(locked_conversation)
    return user_message, assistant_message, confirmation_prompt, False, replayed


def _load_realtime_state(
    conversation: AssistantConversation,
) -> tuple[dict, dict]:
    state = load_conversation_state(conversation)
    realtime_state = state.get(REALTIME_STATE_KEY)
    if not isinstance(realtime_state, dict):
        realtime_state = {"active_turn": None, "recent_turns": []}
    if not isinstance(realtime_state.get("recent_turns"), list):
        realtime_state["recent_turns"] = []
    return state, realtime_state


def _find_realtime_turn(realtime_state: dict, turn_id: str) -> dict | None:
    active_turn = realtime_state.get("active_turn")
    if isinstance(active_turn, dict) and active_turn.get("turn_id") == turn_id:
        return active_turn
    for turn in realtime_state.get("recent_turns", []):
        if isinstance(turn, dict) and turn.get("turn_id") == turn_id:
            return turn
    return None


def _trim_recent_realtime_turns(realtime_state: dict) -> None:
    recent_turns = realtime_state.get("recent_turns")
    if not isinstance(recent_turns, list):
        realtime_state["recent_turns"] = []
        return
    realtime_state["recent_turns"] = recent_turns[:MAX_RECENT_REALTIME_TURNS]


def _archive_realtime_turn(
    realtime_state: dict,
    turn: dict,
    *,
    status: str,
) -> None:
    turn["status"] = status
    turn.pop("confirmation", None)
    for response in _turn_responses(turn).values():
        if isinstance(response, dict):
            response.pop("assistant_text", None)
    realtime_state["active_turn"] = None
    recent_turns = realtime_state.setdefault("recent_turns", [])
    recent_turns.insert(0, turn)
    _trim_recent_realtime_turns(realtime_state)


def _seal_active_realtime_turn(
    db: Session,
    conversation: AssistantConversation,
    state: dict,
    realtime_state: dict,
    *,
    status: str,
) -> AssistantMessage | None:
    turn = realtime_state.get("active_turn")
    if not isinstance(turn, dict):
        return None
    calls = _turn_calls(turn)
    _recover_expired_realtime_tool_calls(turn)
    if any(
        isinstance(call, dict) and call.get("status") == "started"
        for call in calls.values()
    ):
        raise AssistantRealtimeConflictError(
            "Realtime tool call is still in progress"
        )

    user_message = _get_realtime_user_message(db, conversation, turn)
    partial_text = _combined_realtime_response_text(turn).strip()
    interruption_text = "Respuesta de voz interrumpida."
    action_context = _sealed_realtime_action_context(turn)
    if action_context:
        assistant_text = f"{interruption_text}\n\n{action_context}"
        if partial_text and partial_text != interruption_text:
            assistant_text = (
                f"{assistant_text}\n\nTranscripción parcial anterior:\n{partial_text}"
            )
    else:
        assistant_text = (
            f"{partial_text}\n\n{interruption_text}"
            if partial_text and partial_text != interruption_text
            else interruption_text
        )
    actions = _finished_realtime_actions(turn)
    assistant_message = AssistantMessage(
        conversation=conversation,
        role="assistant",
        content=assistant_text,
        actions=json.dumps(actions, ensure_ascii=False) if actions else None,
        agent_key="anacleto",
        routing=None,
    )
    db.add(assistant_message)
    conversation.updated_at = func.now()
    db.flush()
    turn["assistant_message_id"] = assistant_message.id
    turn["confirmation_prompt"] = None
    confirmation_context = _deserialize_confirmation_reference(
        turn.get("confirmation")
    )
    _archive_realtime_turn(realtime_state, turn, status=status)
    state[REALTIME_STATE_KEY] = realtime_state
    dump_conversation_state(conversation, state)
    finalize_confirmation_turn(
        conversation,
        user_message,
        assistant_message,
        confirmation_context,
        confirmation_prompt=None,
    )
    return assistant_message


def _mark_realtime_tool_call_indeterminate(
    db: Session,
    conversation: AssistantConversation,
    turn_id: str,
    payload: AssistantRealtimeToolCallCreate,
    tool_input: dict,
) -> tuple[dict, str, AssistantMessage]:
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state, realtime_state = _load_realtime_state(locked_conversation)
    turn = _find_realtime_turn(realtime_state, turn_id)
    if turn is None:
        raise AssistantRealtimeConflictError("Realtime turn state was lost")
    stored_call = _turn_calls(turn).get(payload.call_id)
    if not isinstance(stored_call, dict):
        raise AssistantRealtimeConflictError("Realtime tool call state was lost")
    if stored_call.get("status") == "indeterminate":
        action = _stored_action(stored_call)
    else:
        action = _set_realtime_tool_call_indeterminate(
            stored_call,
            call_id=payload.call_id,
            name=payload.name,
            tool_input=tool_input,
        )
    locked_conversation.updated_at = func.now()
    state[REALTIME_STATE_KEY] = realtime_state
    dump_conversation_state(locked_conversation, state)
    db.commit()
    user_message = _get_realtime_user_message(db, locked_conversation, turn)
    db.refresh(user_message)
    return action, INDETERMINATE_TOOL_RESULT, user_message


def _recover_expired_realtime_tool_calls(turn: dict) -> bool:
    recovered = False
    for call_id, call in _turn_calls(turn).items():
        if not isinstance(call, dict) or call.get("status") != "started":
            continue
        if not _realtime_tool_call_lease_expired(call):
            continue
        _set_realtime_tool_call_indeterminate(
            call,
            call_id=call_id,
            name=str(call.get("name") or "unknown"),
            tool_input=(
                deepcopy(call.get("input"))
                if isinstance(call.get("input"), dict)
                else {}
            ),
        )
        recovered = True
    return recovered


def _realtime_tool_call_lease_expired(call: dict) -> bool:
    started_at = call.get("started_at")
    if not isinstance(started_at, str):
        return True
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed >= timedelta(
        seconds=REALTIME_TOOL_CALL_LEASE_SECONDS
    )


def _set_realtime_tool_call_indeterminate(
    call: dict,
    *,
    call_id: str,
    name: str,
    tool_input: dict,
) -> dict:
    action = {
        "call_id": call_id,
        "tool": name,
        "ok": False,
        "input": deepcopy(tool_input),
        "result": INDETERMINATE_TOOL_RESULT,
    }
    call.update(
        {
            "status": "indeterminate",
            "action": action,
            "output": INDETERMINATE_TOOL_RESULT,
            "confirmation_prompt": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return action


def _get_realtime_user_message(
    db: Session,
    conversation: AssistantConversation,
    turn: dict,
) -> AssistantMessage:
    user_message_id = int(turn.get("user_message_id") or 0)
    message = db.scalar(
        select(AssistantMessage).where(
            AssistantMessage.id == user_message_id,
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.role == "user",
        )
    )
    if message is None:
        raise AssistantRealtimeConflictError(
            "Realtime user message no longer exists"
        )
    return message


def _get_realtime_assistant_message(
    db: Session,
    conversation: AssistantConversation,
    turn: dict,
) -> AssistantMessage | None:
    assistant_message_id = int(turn.get("assistant_message_id") or 0)
    if not assistant_message_id:
        return None
    return db.scalar(
        select(AssistantMessage).where(
            AssistantMessage.id == assistant_message_id,
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.role == "assistant",
        )
    )


def _turn_calls(turn: dict) -> dict:
    calls = turn.get("calls")
    if not isinstance(calls, dict):
        calls = {}
        turn["calls"] = calls
    return calls


def _turn_responses(turn: dict) -> dict:
    responses = turn.get("responses")
    if not isinstance(responses, dict):
        responses = {}
        turn["responses"] = responses
    return responses


def _stored_action(call: dict) -> dict:
    action = call.get("action")
    if not isinstance(action, dict):
        raise AssistantRealtimeConflictError(
            "Realtime tool result is incomplete"
        )
    return deepcopy(action)


def _finished_realtime_actions(turn: dict) -> list[dict]:
    calls = _turn_calls(turn)
    actions: list[dict] = []
    for call_id in turn.get("call_order", []):
        call = calls.get(call_id)
        if isinstance(call, dict) and call.get("status") in {
            "finished",
            "indeterminate",
        }:
            actions.append(_stored_action(call))
    return actions


def _sealed_realtime_action_context(turn: dict) -> str | None:
    calls = _turn_calls(turn)
    grouped: dict[str, dict[str, int]] = {
        "correctas": {},
        "sin éxito": {},
        "indeterminadas": {},
    }
    for call_id in turn.get("call_order", []):
        call = calls.get(call_id)
        if not isinstance(call, dict) or call.get("status") not in {
            "finished",
            "indeterminate",
        }:
            continue
        action = call.get("action")
        if not isinstance(action, dict):
            continue
        if call.get("status") == "indeterminate":
            outcome = "indeterminadas"
        else:
            outcome = "correctas" if action.get("ok") is True else "sin éxito"
        tool = _safe_realtime_tool_label(action.get("tool"))
        grouped[outcome][tool] = grouped[outcome].get(tool, 0) + 1

    summaries: list[str] = []
    for outcome, tools in grouped.items():
        if not tools:
            continue
        rendered_tools = ", ".join(
            f"{tool} ({count})" for tool, count in tools.items()
        )
        summaries.append(f"{outcome}: {rendered_tools}")
    if not summaries:
        return None

    context = (
        "Registro del sistema: estas herramientas ya fueron procesadas antes "
        "de la interrupción. No las repitas automáticamente; revisa las "
        "acciones auditadas y el sistema antes de intentarlo de nuevo. "
        + "; ".join(summaries)
        + "."
    )
    if len(context) <= MAX_SEALED_REALTIME_ACTION_CONTEXT_CHARS:
        return context
    return context[: MAX_SEALED_REALTIME_ACTION_CONTEXT_CHARS - 3].rstrip() + "..."


def _safe_realtime_tool_label(value: object) -> str:
    raw = value if isinstance(value, str) else "herramienta"
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "_.-")
        else "_"
        for character in raw
    ).strip("_")
    return (safe or "herramienta")[:64]


def _serialize_confirmation_reference(
    reference: ConfirmationReference | None,
) -> dict | None:
    if reference is None:
        return None
    return {
        "confirmation_id": reference.confirmation_id,
        "tool": reference.tool,
        "input_digest": reference.input_digest,
        "tool_input": deepcopy(reference.tool_input),
    }


def _deserialize_confirmation_reference(
    value: object,
) -> ConfirmationReference | None:
    if not isinstance(value, dict):
        return None
    confirmation_id = value.get("confirmation_id")
    tool = value.get("tool")
    input_digest = value.get("input_digest")
    tool_input = value.get("tool_input")
    if not all(
        isinstance(item, str) and item
        for item in (confirmation_id, tool, input_digest)
    ) or not isinstance(tool_input, dict):
        return None
    return ConfirmationReference(
        confirmation_id=confirmation_id,
        tool=tool,
        input_digest=input_digest,
        tool_input=deepcopy(tool_input),
    )


def _confirmation_context_from_result(
    result: ConfirmationToolResult | None,
) -> ConfirmationReference | None:
    if isinstance(result, ConfirmationToolResult) and result.status == "required":
        return result.confirmation
    return None


def _combined_realtime_response_text(turn: dict) -> str:
    responses = _turn_responses(turn)
    parts: list[str] = []
    for response_id in turn.get("response_order", []):
        response = responses.get(response_id)
        if not isinstance(response, dict):
            continue
        text = _optional_string(response.get("assistant_text"))
        if text and (not parts or parts[-1] != text):
            parts.append(text)
    return "\n\n".join(parts)


def _contains_exact_confirmation_prompt(text: str, prompt: str) -> bool:
    return text == prompt or text.endswith(f"\n\n{prompt}")


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tool_call_digest(name: str, arguments: dict) -> str:
    serialized = json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) > MAX_REALTIME_TOOL_ARGUMENT_BYTES:
        raise AssistantRealtimeConflictError(
            "Realtime tool call arguments are too large"
        )
    return _text_digest(serialized)


def _response_digest(payload: AssistantRealtimeTurnCreate) -> str:
    serialized = json.dumps(
        {
            "response_status": payload.response_status,
            "assistant_text": (payload.assistant_text or "").strip(),
            "interrupted": payload.interrupted,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_digest(serialized)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _to_realtime_tool(definition: dict) -> dict:
    return {
        "type": "function",
        "name": definition["name"],
        "description": definition.get("description", ""),
        "parameters": definition.get("input_schema") or {"type": "object"},
    }


def _build_realtime_instructions(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    tools: list,
) -> str:
    prompt = build_system_prompt(db, current_user, tools, input_mode="voice")
    history = _format_history_for_realtime(conversation)
    voice_rules = (
        "Estás en una conversación hablada en tiempo real. Responde de forma "
        "breve, natural y accionable. Si llamas a una herramienta, espera al "
        "resultado antes de dar una conclusión. Si el resultado contiene "
        "confirmation_prompt, lee literalmente ese texto, sin añadir, quitar "
        "ni reformular nada. En cualquier otro caso, no leas JSON ni detalles "
        "técnicos al usuario."
    )
    if not history:
        return f"{prompt}\n\n{voice_rules}"
    return f"{prompt}\n\n{voice_rules}\n\nHistorial reciente:\n{history}"


def _format_history_for_realtime(conversation: AssistantConversation) -> str:
    lines: list[str] = []
    for message in build_history(conversation)[-10:]:
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = "Usuario" if message.get("role") == "user" else "Asistente"
        lines.append(f"{role}: {content.strip()[:1200]}")
    return "\n".join(lines)


def _transcription_config() -> dict | None:
    model = settings.assistant_realtime_transcription_model.strip()
    if not model:
        return None

    config = {"model": model}
    language = settings.assistant_realtime_language_code.strip()
    if language and language.lower() != "multi":
        config["language"] = language
    delay = settings.assistant_realtime_transcription_delay.strip()
    if delay:
        config["delay"] = delay
    return config


def _safety_identifier(current_user: User) -> str:
    raw = f"assistant-user:{current_user.id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
