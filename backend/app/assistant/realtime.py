"""Realtime voice orchestration for Anacleto."""

import hashlib
import json
import logging
from urllib import error as urlerror
from urllib import request as urlrequest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant.guards import check_tool_confirmation
from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import build_system_prompt
from app.assistant.schemas import (
    AssistantRealtimeToolCallCreate,
    AssistantRealtimeTurnCreate,
)
from app.assistant.tools import ToolContext, execute_tool, get_available_tool_specs
from app.assistant.turn import MAX_TOOL_RESULT_CHARS, build_history
from app.core.config import settings
from app.users.models import User

logger = logging.getLogger(__name__)


class AssistantRealtimeUnavailableError(Exception):
    """Realtime voice is not configured or the upstream request failed."""


def realtime_voice_enabled() -> bool:
    return settings.assistant_realtime_enabled and bool(settings.openai_api_key)


def create_realtime_client_secret(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
) -> dict:
    if not realtime_voice_enabled():
        raise AssistantRealtimeUnavailableError("Realtime voice is not configured")

    payload = build_realtime_client_secret_payload(db, current_user, conversation)
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
            "create_response": True,
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


def execute_realtime_tool_call(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    payload: AssistantRealtimeToolCallCreate,
) -> tuple[dict, str, AssistantMessage]:
    user_message = _ensure_realtime_user_message(
        db,
        current_user,
        conversation,
        user_text=payload.user_transcript,
        user_message_id=payload.user_message_id,
    )
    tools = get_available_tool_specs(db, current_user)
    allowed_tool_names = frozenset(tool.name for tool in tools)
    tool_input = dict(payload.arguments or {})
    guarded_result = check_tool_confirmation(
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
        "tool": payload.name,
        "ok": result.ok,
        "input": tool_input,
        "result": result.content[:MAX_TOOL_RESULT_CHARS],
    }
    conversation.updated_at = func.now()
    db.commit()
    db.refresh(user_message)
    db.refresh(conversation)
    return action, result.content, user_message


def persist_realtime_turn(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    payload: AssistantRealtimeTurnCreate,
) -> tuple[AssistantMessage | None, AssistantMessage | None]:
    user_text = (payload.user_text or "").strip()
    assistant_text = (payload.assistant_text or "").strip()
    has_actions = bool(payload.actions)
    if not user_text and not assistant_text and not has_actions:
        return None, None

    user_message = None
    if user_text or payload.user_message_id is not None:
        user_message = _ensure_realtime_user_message(
            db,
            current_user,
            conversation,
            user_text=user_text or None,
            user_message_id=payload.user_message_id,
        )

    assistant_message = None
    if assistant_text or has_actions:
        assistant_message = AssistantMessage(
            conversation=conversation,
            role="assistant",
            content=assistant_text or (
                "Respuesta interrumpida." if payload.interrupted else ""
            ),
            actions=(
                json.dumps(
                    [action.model_dump() for action in payload.actions],
                    ensure_ascii=False,
                )
                if has_actions
                else None
            ),
            agent_key="anacleto",
            routing=None,
        )
        db.add(assistant_message)

    if user_text and conversation.title == "Conversación":
        conversation.title = user_text[:255]
    conversation.updated_at = func.now()
    db.commit()
    if user_message is not None:
        db.refresh(user_message)
    if assistant_message is not None:
        db.refresh(assistant_message)
    db.refresh(conversation)
    return user_message, assistant_message


def get_conversation_user_message(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_message_id: int,
) -> AssistantMessage | None:
    message = db.scalar(
        select(AssistantMessage).where(AssistantMessage.id == user_message_id)
    )
    if (
        message is None
        or message.conversation_id != conversation.id
        or message.role != "user"
        or conversation.created_by_id != current_user.id
    ):
        return None
    return message


def _ensure_realtime_user_message(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    *,
    user_text: str | None,
    user_message_id: int | None,
) -> AssistantMessage:
    content = (user_text or "").strip()
    if user_message_id is not None:
        existing = get_conversation_user_message(
            db,
            current_user,
            conversation,
            user_message_id,
        )
        if existing is not None:
            if content and existing.content != content:
                existing.content = content
                if conversation.title == "Conversación":
                    conversation.title = content[:255]
                conversation.updated_at = func.now()
                db.commit()
                db.refresh(existing)
            return existing

    user_message = AssistantMessage(
        conversation=conversation,
        role="user",
        content=content or "Mensaje de voz",
    )
    if content and conversation.title == "Conversación":
        conversation.title = content[:255]
    conversation.updated_at = func.now()
    db.add(user_message)
    db.commit()
    db.refresh(user_message)
    db.refresh(conversation)
    return user_message


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
        "resultado antes de dar una conclusión. No leas JSON ni detalles "
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
