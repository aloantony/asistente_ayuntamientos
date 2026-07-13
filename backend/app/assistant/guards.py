"""Code-level assistant guards that do not depend on prompt obedience."""

import hashlib
import json
import logging
import re
import secrets
import unicodedata
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import (
    CONFIRMATION_ALREADY_CONSUMED_TOOL_RESULT,
    CONFIRMATION_CANCELLED_TOOL_RESULT,
    CONFIRMATION_IN_PROGRESS_TOOL_RESULT,
    CONFIRMATION_REQUIRED_TOOL_RESULT,
    CONFIRMATION_STALE_TURN_TOOL_RESULT,
)
from app.assistant.tools import (
    ToolResult,
    normalize_admin_feedback_input,
    normalize_create_requirement_input,
)

logger = logging.getLogger(__name__)

CONFIRMATION_REQUIRED_TOOLS = frozenset(
    {"create_requirement", "send_admin_feedback"}
)
GENERIC_EXPLICIT_CONFIRMATIONS = frozenset(
    {
        "adelante",
        "confirmo",
        "hazlo",
        "lo confirmo",
        "si adelante",
        "si confirmo",
        "si hazlo",
    }
)
TOOL_EXPLICIT_CONFIRMATIONS = {
    "create_requirement": frozenset(
        {
            "adelante crealo",
            "confirmo el borrador",
            "confirmo la creacion",
            "crealo",
            "de acuerdo crealo",
            "guardalo",
            "si crealo",
            "si guardalo",
        }
    ),
    "send_admin_feedback": frozenset({"envialo", "si envialo"}),
}
CANCELLATION_PATTERN = re.compile(
    r"^(?:"
    r"no(?:\s+gracias)?|"
    r"no\s+.*\b(?:crees|guardes|hagas|envies|mandes)\b.*|"
    r".*\b(?:cancela(?:r|lo)?|rechaz(?:a|ar|o)|detente|olvida(?:lo|r)?)\b.*"
    r")$"
)

REQUIREMENT_CONFIRMATION_FIELDS = (
    ("organization_id", "organizacion_id"),
    ("project_id", "proyecto_id"),
    ("title", "titulo"),
    ("summary", "resumen"),
    ("problem", "problema"),
    ("current_process", "proceso_actual"),
    ("desired_process", "proceso_deseado"),
    ("affected_users", "personas_afectadas"),
    ("involved_documents", "documentos_implicados"),
    ("data_sensitivity_notes", "notas_sobre_datos"),
    ("legal_notes", "notas_legales"),
    ("acceptance_criteria", "criterios_de_aceptacion"),
    ("open_questions", "preguntas_abiertas"),
    ("priority", "prioridad"),
)
ADMIN_FEEDBACK_CONFIRMATION_FIELDS = (
    ("category", "categoria"),
    ("title", "titulo"),
    ("description", "descripcion"),
    ("priority", "prioridad"),
    ("organization_id", "organizacion_id"),
)


@dataclass(frozen=True)
class ConfirmationReference:
    confirmation_id: str
    tool: str
    input_digest: str
    tool_input: dict


@dataclass
class ConfirmationToolResult(ToolResult):
    status: str
    confirmation: ConfirmationReference | None = None


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
) -> ConfirmationToolResult | None:
    if tool_name not in CONFIRMATION_REQUIRED_TOOLS:
        return None

    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state = load_conversation_state(locked_conversation)
    if _is_stale_confirmation_turn(db, locked_conversation, user_message):
        db.commit()
        return ConfirmationToolResult(
            content=CONFIRMATION_STALE_TURN_TOOL_RESULT,
            ok=False,
            status="stale",
        )
    try:
        current_digest = _confirmation_digest(tool_name, tool_input)
    except (TypeError, ValueError) as error:
        db.commit()
        return ConfirmationToolResult(
            content=f"Entrada inválida para {tool_name}: {error}",
            ok=False,
            status="invalid",
        )

    cancelled = state.get("last_cancelled_confirmation")
    if (
        isinstance(cancelled, dict)
        and int(cancelled.get("cancelled_at_user_message_id") or 0)
        == user_message.id
    ):
        db.commit()
        return ConfirmationToolResult(
            content=CONFIRMATION_CANCELLED_TOOL_RESULT,
            ok=False,
            status="cancelled",
        )

    consumed = state.get("last_consumed_confirmation")
    if (
        isinstance(consumed, dict)
        and consumed.get("tool") == tool_name
        and consumed.get("input_digest") == current_digest
        and int(consumed.get("consumed_at_user_message_id") or 0)
        == user_message.id
    ):
        db.commit()
        return ConfirmationToolResult(
            content=CONFIRMATION_ALREADY_CONSUMED_TOOL_RESULT,
            ok=False,
            status="already_consumed",
        )

    pending = state.get("pending_confirmation")
    if isinstance(pending, dict):
        reference = _confirmation_reference(pending)
        confirmation_id = reference.confirmation_id if reference else None
        response_message_id = int(pending.get("response_user_message_id") or 0)
        response_confirmation_id = pending.get("response_confirmation_id")
        response = pending.get("response")
        if (
            reference is not None
            and response_message_id
            and response_message_id != user_message.id
        ):
            db.commit()
            return ConfirmationToolResult(
                content=CONFIRMATION_IN_PROGRESS_TOOL_RESULT,
                ok=False,
                status="in_progress",
                confirmation=reference,
            )
        response_matches = (
            bool(confirmation_id)
            and response_message_id == user_message.id
            and response_confirmation_id == confirmation_id
        )
        pending_matches = (
            reference is not None
            and pending.get("tool") == tool_name
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
                "prompted_at_assistant_message_id": pending.get(
                    "response_prompted_at_assistant_message_id"
                )
                or pending.get("prompted_at_assistant_message_id"),
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
            return ConfirmationToolResult(
                content=CONFIRMATION_REQUIRED_TOOL_RESULT,
                ok=False,
                status="required",
                confirmation=reference,
            )

    reference = record_pending_confirmation(
        locked_conversation,
        user_message,
        tool_name,
        tool_input,
    )
    db.commit()
    return ConfirmationToolResult(
        content=CONFIRMATION_REQUIRED_TOOL_RESULT,
        ok=False,
        status="required",
        confirmation=reference,
    )


def process_pending_confirmation_response(
    db: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
) -> ConfirmationReference | None:
    """Record an explicit response to the current pending confirmation.

    The response is valid only for this user message. The tool guard still
    verifies the complete payload digest before consuming it.
    """
    locked_conversation = lock_conversation_for_confirmation(db, conversation.id)
    state = load_conversation_state(locked_conversation)
    pending = state.get("pending_confirmation")
    if not isinstance(pending, dict):
        return None

    proposed_message_id = int(pending.get("proposed_at_user_message_id") or 0)
    if proposed_message_id >= user_message.id:
        return None

    state_changed = False
    previous_response_message_id = int(pending.get("response_user_message_id") or 0)
    if previous_response_message_id and previous_response_message_id < user_message.id:
        # A prior turn died or finished without consuming its claim. Latest-turn
        # wins preserves one-shot behavior while avoiding a permanently stuck
        # confirmation after a worker interruption.
        pending.pop("response", None)
        pending.pop("response_user_message_id", None)
        pending.pop("response_confirmation_id", None)
        pending.pop("response_prompted_at_assistant_message_id", None)
        state_changed = True

    pending_tool = pending.get("tool")
    response = classify_confirmation_response(
        user_message.content,
        tool_name=pending_tool if isinstance(pending_tool, str) else None,
    )
    if response == "cancelled":
        state.pop("pending_confirmation", None)
        state["last_cancelled_confirmation"] = {
            "confirmation_id": pending.get("confirmation_id"),
            "tool": pending.get("tool"),
            "input_digest": pending.get("input_digest"),
            "proposed_at_user_message_id": pending.get(
                "proposed_at_user_message_id"
            ),
            "prompted_at_assistant_message_id": pending.get(
                "prompted_at_assistant_message_id"
            ),
            "cancelled_at_user_message_id": user_message.id,
        }
        dump_conversation_state(locked_conversation, state)
        return None

    reference = _confirmation_reference(pending)
    if reference is None:
        state.pop("pending_confirmation", None)
        dump_conversation_state(locked_conversation, state)
        return None

    if pending.get("response_user_message_id"):
        # Another in-flight turn already claimed this confirmation. Its tool
        # guard will either consume it or the next turn will clear the claim.
        if state_changed:
            dump_conversation_state(locked_conversation, state)
        if int(pending.get("response_user_message_id") or 0) == user_message.id:
            return reference
        return None

    if response == "confirmed" and _is_direct_response_to_prompt(
        db,
        locked_conversation,
        pending,
        user_message,
    ):
        pending["response"] = "confirmed"
        pending["response_user_message_id"] = user_message.id
        pending["response_confirmation_id"] = pending.get("confirmation_id")
        pending["response_prompted_at_assistant_message_id"] = pending.get(
            "prompted_at_assistant_message_id"
        )
        dump_conversation_state(locked_conversation, state)
        return reference

    if state_changed:
        dump_conversation_state(locked_conversation, state)
    return reference


def finalize_confirmation_turn(
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    assistant_message: AssistantMessage,
    expected: ConfirmationReference | None,
    *,
    confirmation_prompt: str | None,
) -> None:
    """Release an unused claim and bind a fresh prompt to its assistant reply.

    The caller must hold a row lock for the conversation until this state and
    the assistant message are committed together.
    """
    state = load_conversation_state(conversation)
    pending = state.get("pending_confirmation")
    if not _pending_matches_reference(pending, expected):
        return

    response_user_message_id = int(pending.get("response_user_message_id") or 0)
    if response_user_message_id and response_user_message_id != user_message.id:
        return

    response_matches = (
        response_user_message_id == user_message.id
        and pending.get("response_confirmation_id")
        == pending.get("confirmation_id")
    )
    if response_matches:
        pending.pop("response", None)
        pending.pop("response_user_message_id", None)
        pending.pop("response_confirmation_id", None)
        pending.pop("response_prompted_at_assistant_message_id", None)

    if confirmation_prompt is not None:
        if not assistant_message.content.endswith(confirmation_prompt):
            raise ValueError("Confirmation prompt is missing from assistant message")
        pending["prompted_at_assistant_message_id"] = assistant_message.id

    dump_conversation_state(conversation, state)


def build_confirmation_prompt(
    conversation: AssistantConversation,
    expected: ConfirmationReference | None,
    *,
    input_mode: str,
    turn_user_message_id: int,
) -> str | None:
    """Render the exact pending payload only when it still matches this turn."""
    state = load_conversation_state(conversation)
    pending = state.get("pending_confirmation")
    if not _pending_matches_reference(pending, expected):
        return None
    response_user_message_id = int(pending.get("response_user_message_id") or 0)
    if response_user_message_id and response_user_message_id != turn_user_message_id:
        return None
    reference = _confirmation_reference(pending)
    if reference is None:
        return None
    return _render_confirmation_prompt(reference, input_mode=input_mode)


def record_pending_confirmation(
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    tool_input: dict,
) -> ConfirmationReference:
    confirmation_id = secrets.token_urlsafe(12)
    state = load_conversation_state(conversation)
    confirmed_input = _confirmation_input(tool_name, tool_input)
    pending = {
        "confirmation_id": confirmation_id,
        "tool": tool_name,
        "input": confirmed_input,
        "input_digest": _confirmation_digest(tool_name, confirmed_input),
        "proposed_at_user_message_id": user_message.id,
    }
    state["pending_confirmation"] = pending
    dump_conversation_state(conversation, state)
    reference = _confirmation_reference(pending)
    if reference is None:
        raise RuntimeError("Failed to build pending confirmation reference")
    return reference


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
    confirmed_input = _confirmation_input(tool_name, tool_input)
    canonical_payload = json.dumps(
        {"tool": tool_name, "input": confirmed_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _confirmation_input(tool_name: str, tool_input: dict) -> dict:
    if tool_name == "create_requirement":
        return normalize_create_requirement_input(tool_input)
    if tool_name == "send_admin_feedback":
        return normalize_admin_feedback_input(tool_input)
    return deepcopy(tool_input)


def _confirmation_reference(pending: object) -> ConfirmationReference | None:
    if not isinstance(pending, dict):
        return None
    confirmation_id = pending.get("confirmation_id")
    tool = pending.get("tool")
    input_digest = pending.get("input_digest")
    tool_input = pending.get("input")
    if not all(
        isinstance(value, str) and value
        for value in (confirmation_id, tool, input_digest)
    ) or not isinstance(tool_input, dict):
        return None
    return ConfirmationReference(
        confirmation_id=confirmation_id,
        tool=tool,
        input_digest=input_digest,
        tool_input=deepcopy(tool_input),
    )


def _pending_matches_reference(
    pending: object,
    expected: ConfirmationReference | None,
) -> bool:
    current = _confirmation_reference(pending)
    return (
        current is not None
        and expected is not None
        and current.confirmation_id == expected.confirmation_id
        and current.tool == expected.tool
        and current.input_digest == expected.input_digest
    )


def _render_confirmation_prompt(
    reference: ConfirmationReference,
    *,
    input_mode: str,
) -> str:
    if reference.tool == "send_admin_feedback":
        return _render_admin_feedback_confirmation_prompt(
            reference,
            input_mode=input_mode,
        )
    if reference.tool != "create_requirement":
        raise ValueError(f"Unsupported confirmation tool: {reference.tool}")

    display_payload = {
        label: reference.tool_input[field]
        for field, label in REQUIREMENT_CONFIRMATION_FIELDS
        if field in reference.tool_input and field != "priority"
    }
    display_payload["prioridad"] = reference.tool_input.get("priority") or "medium"
    display_payload["estado_al_guardar"] = "draft"
    display_payload["origen"] = "conversation"
    if input_mode == "voice":
        details = "; ".join(
            f"{label.replace('_', ' ')}: {_plain_confirmation_value(value)}"
            for label, value in display_payload.items()
        )
        return (
            "Borrador pendiente de confirmación. "
            f"Datos exactos: {details}. "
            "Los campos no mencionados quedarán sin informar. Para guardarlo "
            "como borrador, responde: Sí, créalo; Confirmo; o Adelante."
        )

    serialized = json.dumps(display_payload, ensure_ascii=False, indent=2)
    indented_payload = "\n".join(f"    {line}" for line in serialized.splitlines())
    return (
        "### Borrador pendiente de confirmación\n\n"
        f"Referencia: `{reference.confirmation_id}`\n\n"
        f"{indented_payload}\n\n"
        "Los campos que no aparecen quedarán sin informar. Se guardará como "
        "borrador con origen conversacional.\n\n"
        "Responde **Sí, créalo**, **Confirmo** o **Adelante** solo si estos "
        "datos son correctos."
    )


def _render_admin_feedback_confirmation_prompt(
    reference: ConfirmationReference,
    *,
    input_mode: str,
) -> str:
    display_payload = {
        label: reference.tool_input[field]
        for field, label in ADMIN_FEEDBACK_CONFIRMATION_FIELDS
        if field in reference.tool_input and field != "priority"
    }
    display_payload["prioridad"] = reference.tool_input.get("priority") or "medium"
    if input_mode == "voice":
        details = "; ".join(
            f"{label.replace('_', ' ')}: {_plain_confirmation_value(value)}"
            for label, value in display_payload.items()
        )
        return (
            "Feedback pendiente de confirmación. "
            f"Datos exactos: {details}. "
            "Para enviarlo al equipo administrador, responde: Sí, envíalo; "
            "Confirmo; o Adelante."
        )

    serialized = json.dumps(display_payload, ensure_ascii=False, indent=2)
    indented_payload = "\n".join(
        f"    {line}" for line in serialized.splitlines()
    )
    return (
        "### Feedback pendiente de confirmación\n\n"
        f"Referencia: `{reference.confirmation_id}`\n\n"
        f"{indented_payload}\n\n"
        "Se enviará al equipo administrador con estos datos exactos.\n\n"
        "Responde **Sí, envíalo**, **Confirmo** o **Adelante** solo si los "
        "datos son correctos."
    )


def _plain_confirmation_value(value: object) -> str:
    if value is None:
        return "sin informar"
    return re.sub(r"\s+", " ", str(value)).strip()


def _is_direct_response_to_prompt(
    db: Session,
    conversation: AssistantConversation,
    pending: dict,
    user_message: AssistantMessage,
) -> bool:
    prompted_message_id = int(
        pending.get("prompted_at_assistant_message_id") or 0
    )
    if not prompted_message_id:
        return False

    latest_prior_message_id = db.scalar(
        select(AssistantMessage.id)
        .where(
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.id < user_message.id,
        )
        .order_by(AssistantMessage.id.desc())
        .limit(1)
    )
    return latest_prior_message_id == prompted_message_id


def _is_stale_confirmation_turn(
    db: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
) -> bool:
    latest_user_message_id = db.scalar(
        select(AssistantMessage.id)
        .where(
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.role == "user",
        )
        .order_by(AssistantMessage.id.desc())
        .limit(1)
    )
    return bool(
        latest_user_message_id and latest_user_message_id > user_message.id
    )


def classify_confirmation_response(
    text: str,
    *,
    tool_name: str | None = None,
) -> str:
    normalized = normalize_confirmation_response(text)
    if normalized.endswith(" por favor"):
        normalized = normalized.removesuffix(" por favor").strip()
    tool_confirmations = TOOL_EXPLICIT_CONFIRMATIONS.get(tool_name, frozenset())
    if (
        normalized in GENERIC_EXPLICIT_CONFIRMATIONS
        or normalized in tool_confirmations
    ):
        return "confirmed"
    if CANCELLATION_PATTERN.fullmatch(normalized):
        return "cancelled"
    return "ambiguous"


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
