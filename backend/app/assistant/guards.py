"""Code-level assistant guards that do not depend on prompt obedience."""

import json
import logging
import re
import unicodedata

from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import CONFIRMATION_REQUIRED_TOOL_RESULT
from app.assistant.tools import ToolResult

logger = logging.getLogger(__name__)

CONFIRMATION_REQUIRED_TOOLS = frozenset({"create_requirement"})


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
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    tool_input: dict,
) -> ToolResult | None:
    if tool_name not in CONFIRMATION_REQUIRED_TOOLS:
        return None

    state = load_conversation_state(conversation)
    pending = state.get("pending_confirmation")
    current_key = _confirmation_key(tool_name, tool_input)
    if (
        isinstance(pending, dict)
        and pending.get("tool") == tool_name
        and pending.get("input_key") == current_key
        and int(pending.get("proposed_at_user_message_id") or 0) < user_message.id
    ):
        clear_pending_confirmation(conversation)
        return None

    record_pending_confirmation(conversation, user_message, tool_name, tool_input)
    return ToolResult(content=CONFIRMATION_REQUIRED_TOOL_RESULT, ok=False)


def record_pending_confirmation(
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    tool_input: dict,
) -> None:
    state = load_conversation_state(conversation)
    state["pending_confirmation"] = {
        "tool": tool_name,
        "input": tool_input,
        "input_key": _confirmation_key(tool_name, tool_input),
        "proposed_at_user_message_id": user_message.id,
    }
    dump_conversation_state(conversation, state)


def clear_pending_confirmation(conversation: AssistantConversation) -> None:
    state = load_conversation_state(conversation)
    if "pending_confirmation" in state:
        state.pop("pending_confirmation", None)
        dump_conversation_state(conversation, state)


def _confirmation_key(tool_name: str, tool_input: dict) -> str:
    if tool_name == "create_requirement":
        organization_id = tool_input.get("organization_id")
        title = normalize_text(str(tool_input.get("title") or ""))
        return f"{tool_name}:{organization_id}:{title}"
    return f"{tool_name}:{json.dumps(tool_input, sort_keys=True, ensure_ascii=False)}"


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.strip().lower())
    stripped = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", stripped)
