"""Code-level assistant guards that do not depend on prompt obedience."""

import hashlib
import json
import logging
import re
import secrets
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import (
    CONFIRMATION_CANCELLED_TOOL_RESULT,
    CONFIRMATION_REQUIRED_TOOL_RESULT,
)
from app.assistant.tools import ToolResult

logger = logging.getLogger(__name__)

CONFIRMATION_REQUIRED_TOOLS = frozenset({"create_requirement"})
EXPLICIT_CONFIRMATIONS = frozenset(
    {
        "adelante",
        "adelante crealo",
        "confirmo",
        "confirmo la creacion",
        "crealo",
        "de acuerdo crealo",
        "guardalo",
        "hazlo",
        "lo confirmo",
        "si adelante",
        "si confirmo",
        "si crealo",
        "si guardalo",
        "si hazlo",
    }
)
CANCELLATION_PATTERN = re.compile(
    r"\b(?:no|cancela(?:r|lo)?|rechaz(?:a|ar|o)|detente|olvida(?:lo|r)?)\b"
)


def load_conversation_state(conversation: AssistantConversation) -> dict:
    if not conversation.state:
        return {}
    try:
        parsed = json.loads(conversation.state)
    except json.JSONDecodeError:
        logger.warning("Assistant conversation state is malformed: %s", conversation.id)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def dump_conversation_state(conversation: AssistantConversation, state: dict) -> None:
    conversation.state = json.dumps(state, ensure_ascii=False) if state else None


def check_tool_confirmation(
    db: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    tool_input: dict,
) -> ToolResult | None:
    if tool_name not in CONFIRMATION_REQUIRED_TOOLS:
        return None

    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state = load_conversation_state(locked_conversation)
    pending = state.get("pending_confirmation")
    current_digest = _confirmation_digest(tool_name, tool_input)
    if isinstance(pending, dict):
        confirmation_id = pending.get("confirmation_id")
        response_message_id = int(pending.get("response_user_message_id") or 0)
        response_confirmation_id = pending.get("response_confirmation_id")
        response = pending.get("response")
        response_matches = (
            bool(confirmation_id)
            and response_message_id == user_message.id
            and response_confirmation_id == confirmation_id
        )
        if response_matches and response == "cancelled":
            state.pop("pending_confirmation", None)
            dump_conversation_state(locked_conversation, state)
            db.commit()
            return ToolResult(content=CONFIRMATION_CANCELLED_TOOL_RESULT, ok=False)

        pending_matches = (
            pending.get("tool") == tool_name
            and pending.get("input_digest") == current_digest
        )
        if pending_matches and response_matches and response == "confirmed":
            state.pop("pending_confirmation", None)
            state["last_consumed_confirmation"] = {
                "confirmation_id": confirmation_id,
                "tool": tool_name,
                "input_digest": current_digest,
                "proposed_at_user_message_id": pending.get(
                    "proposed_at_user_message_id"
                ),
                "confirmed_at_user_message_id": response_message_id,
                "consumed_at_user_message_id": user_message.id,
            }
            dump_conversation_state(locked_conversation, state)
            # This commit is the one-shot boundary. Tool executors run in a new
            # transaction, so their rollback cannot restore the authorization.
            db.commit()
            return None

        if pending_matches:
            db.commit()
            return ToolResult(
                content=CONFIRMATION_REQUIRED_TOOL_RESULT,
                ok=False,
            )

    record_pending_confirmation(
        locked_conversation,
        user_message,
        tool_name,
        tool_input,
    )
    db.commit()
    return ToolResult(content=CONFIRMATION_REQUIRED_TOOL_RESULT, ok=False)


def process_pending_confirmation_response(
    db: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
) -> None:
    """Record an explicit response to the current pending confirmation.

    The response is valid only for this user message. The tool guard still
    verifies the complete payload digest before consuming it.
    """
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state = load_conversation_state(locked_conversation)
    pending = state.get("pending_confirmation")
    if not isinstance(pending, dict):
        return

    proposed_message_id = int(pending.get("proposed_at_user_message_id") or 0)
    if proposed_message_id >= user_message.id:
        return

    previous_response_message_id = int(
        pending.get("response_user_message_id") or 0
    )
    if previous_response_message_id and previous_response_message_id < user_message.id:
        # A prior turn died or finished without consuming its claim. Latest-turn
        # wins preserves one-shot behavior while avoiding a permanently stuck
        # confirmation after a worker interruption.
        pending.pop("response", None)
        pending.pop("response_user_message_id", None)
        pending.pop("response_confirmation_id", None)

    normalized_response = normalize_confirmation_response(user_message.content)
    if CANCELLATION_PATTERN.search(normalized_response):
        pending["response"] = "cancelled"
        pending["response_user_message_id"] = user_message.id
        pending["response_confirmation_id"] = pending.get("confirmation_id")
        dump_conversation_state(locked_conversation, state)
        return

    if pending.get("response_user_message_id"):
        # Another in-flight turn already claimed this confirmation. Its tool
        # guard will either consume it or release the claim at the end of turn.
        return

    if normalized_response in EXPLICIT_CONFIRMATIONS:
        pending["response"] = "confirmed"
        pending["response_user_message_id"] = user_message.id
        pending["response_confirmation_id"] = pending.get("confirmation_id")
        dump_conversation_state(locked_conversation, state)


def release_unconsumed_confirmation_response(
    db: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
) -> None:
    """Expire a response claim that this turn did not consume."""
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state = load_conversation_state(locked_conversation)
    pending = state.get("pending_confirmation")
    if not isinstance(pending, dict):
        db.commit()
        return

    response_matches = (
        int(pending.get("response_user_message_id") or 0) == user_message.id
        and pending.get("response_confirmation_id")
        == pending.get("confirmation_id")
    )
    if not response_matches:
        db.commit()
        return

    if pending.get("response") == "cancelled":
        state.pop("pending_confirmation", None)
    else:
        pending.pop("response", None)
        pending.pop("response_user_message_id", None)
        pending.pop("response_confirmation_id", None)
    dump_conversation_state(locked_conversation, state)
    db.commit()


def record_pending_confirmation(
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    tool_input: dict,
) -> None:
    confirmation_id = secrets.token_urlsafe(12)
    state = load_conversation_state(conversation)
    pending = {
        "confirmation_id": confirmation_id,
        "tool": tool_name,
        "input": tool_input,
        "input_digest": _confirmation_digest(tool_name, tool_input),
        "proposed_at_user_message_id": user_message.id,
    }
    state["pending_confirmation"] = pending
    dump_conversation_state(conversation, state)


def lock_conversation_for_confirmation(
    db: Session,
    conversation_id: int,
) -> AssistantConversation:
    conversation = db.scalar(
        select(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise RuntimeError(f"Assistant conversation not found: {conversation_id}")
    return conversation


def _confirmation_digest(tool_name: str, tool_input: dict) -> str:
    canonical_payload = json.dumps(
        {"tool": tool_name, "input": tool_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def normalize_confirmation_response(text: str) -> str:
    normalized = normalize_text(text)
    words_only = re.sub(r"[^a-z0-9\s]", " ", normalized)
    return re.sub(r"\s+", " ", words_only).strip()


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.strip().lower())
    stripped = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", stripped)
