"""Code-level assistant guards that do not depend on prompt obedience."""

import hashlib
import json
import logging
import re
import secrets
import unicodedata
from copy import deepcopy
from dataclasses import dataclass

from fastapi import HTTPException
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
    TOOL_CATALOG,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.assistant.tool_authorization import (
    CONVERSATION_AUTHORIZATION_STATE_KEY,
    ConversationToolAuthorization,
    issue_conversation_tool_authorization,
    recover_conversation_tool_authorization,
    tool_input_digest,
)
from app.users.models import User

logger = logging.getLogger(__name__)
_TOOL_SPEC_UNSET = object()
MAX_FINALIZED_CONFIRMATION_EXCHANGES = 32
CONFIRMATION_SUFFIX_ROUTING_KEY = "_confirmation_suffix"
CONFIRMATION_RESPONSE_ROUTING_KEY = "_confirmation_response"

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
    ("status", "estado_al_guardar"),
    ("source_type", "origen"),
)
ADMIN_FEEDBACK_CONFIRMATION_FIELDS = (
    ("category", "categoria"),
    ("title", "titulo"),
    ("description", "descripcion"),
    ("priority", "prioridad"),
    ("organization_id", "organizacion_id"),
    ("status", "estado_al_enviar"),
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


def confirmation_safe_history_content(
    message: AssistantMessage,
) -> str | None:
    routing = _load_message_routing(message)
    if isinstance(routing.get(CONFIRMATION_RESPONSE_ROUTING_KEY), dict):
        return None
    suffix = routing.get(CONFIRMATION_SUFFIX_ROUTING_KEY)
    if not isinstance(suffix, dict):
        return message.content
    suffix_length = int(suffix.get("length") or 0)
    expected_digest = suffix.get("sha256")
    if (
        suffix_length <= 0
        or suffix_length > len(message.content)
        or not isinstance(expected_digest, str)
    ):
        return message.content
    stored_suffix = message.content[-suffix_length:]
    if not secrets.compare_digest(
        hashlib.sha256(stored_suffix.encode("utf-8")).hexdigest(),
        expected_digest,
    ):
        return message.content
    functional_content = message.content[:-suffix_length].rstrip()
    return functional_content or None


def message_has_confirmation_suffix_metadata(
    message: AssistantMessage,
) -> bool:
    routing = _load_message_routing(message)
    return isinstance(routing.get(CONFIRMATION_SUFFIX_ROUTING_KEY), dict)


def message_is_confirmation_response(message: AssistantMessage) -> bool:
    routing = _load_message_routing(message)
    return isinstance(routing.get(CONFIRMATION_RESPONSE_ROUTING_KEY), dict)


def _record_finalized_confirmation_exchange(
    state: dict,
    pending: dict,
    *,
    response_user_message_id: int,
    outcome: str,
) -> None:
    prompt_message_id = int(
        pending.get("response_prompted_at_assistant_message_id")
        or pending.get("prompted_at_assistant_message_id")
        or 0
    )
    if not prompt_message_id or not response_user_message_id:
        return
    confirmation_id = pending.get("confirmation_id")
    exchanges = state.get("finalized_confirmation_exchanges")
    if not isinstance(exchanges, list):
        exchanges = []
    exchanges = [
        exchange
        for exchange in exchanges
        if not (
            isinstance(exchange, dict)
            and exchange.get("confirmation_id") == confirmation_id
        )
    ]
    exchange = {
        "confirmation_id": confirmation_id,
        "prompted_at_assistant_message_id": prompt_message_id,
        "response_user_message_id": response_user_message_id,
        "outcome": outcome,
    }
    suffix_metadata = pending.get("confirmation_suffix")
    if isinstance(suffix_metadata, dict):
        exchange["confirmation_suffix"] = deepcopy(suffix_metadata)
    exchanges.append(exchange)
    state["finalized_confirmation_exchanges"] = exchanges[
        -MAX_FINALIZED_CONFIRMATION_EXCHANGES:
    ]


def _load_message_routing(message: AssistantMessage) -> dict:
    if not message.routing:
        return {}
    try:
        value = json.loads(message.routing)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _store_message_routing(message: AssistantMessage, routing: dict) -> None:
    message.routing = json.dumps(routing, ensure_ascii=False) if routing else None


def _record_confirmation_suffix_metadata(
    message: AssistantMessage,
    confirmation_prompt: str,
) -> dict:
    prompt_start = len(message.content) - len(confirmation_prompt)
    if prompt_start < 0 or not message.content.endswith(confirmation_prompt):
        raise ValueError("Confirmation prompt is missing from assistant message")
    suffix_start = prompt_start
    if message.content[max(0, prompt_start - 2) : prompt_start] == "\n\n":
        suffix_start -= 2
    suffix = message.content[suffix_start:]
    metadata = {
        "length": len(suffix),
        "sha256": hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        "prompt_sha256": hashlib.sha256(
            confirmation_prompt.encode("utf-8")
        ).hexdigest(),
    }
    routing = _load_message_routing(message)
    routing[CONFIRMATION_SUFFIX_ROUTING_KEY] = metadata
    _store_message_routing(message, routing)
    return metadata


def _mark_confirmation_response_message(
    message: AssistantMessage,
    *,
    confirmation_id: str | None,
    outcome: str,
) -> None:
    routing = _load_message_routing(message)
    routing[CONFIRMATION_RESPONSE_ROUTING_KEY] = {
        "confirmation_id": confirmation_id,
        "outcome": outcome,
    }
    _store_message_routing(message, routing)


def check_tool_confirmation(
    db: Session,
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    tool_input: dict,
    *,
    current_user: User,
    tool_spec: ToolSpec | None | object = _TOOL_SPEC_UNSET,
) -> ConfirmationToolResult | ConversationToolAuthorization | None:
    if tool_spec is _TOOL_SPEC_UNSET:
        spec = TOOL_CATALOG.get(tool_name)
    elif isinstance(tool_spec, ToolSpec):
        spec = tool_spec
    else:
        spec = None
    if spec is None or not spec.requires_confirmation:
        return None
    if spec.name != tool_name:
        raise ValueError(
            f"Tool policy mismatch: expected {tool_name}, received {spec.name}"
        )

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
        normalized_input = spec.normalize_input(
            db,
            current_user,
            tool_input,
            ToolContext(
                conversation_id=locked_conversation.id,
                user_message_id=user_message.id,
            ),
        )
        current_digest = tool_input_digest(tool_name, normalized_input)
    except HTTPException as error:
        db.rollback()
        return ConfirmationToolResult(
            content=f"Entrada inválida para {tool_name}: {error.detail}",
            ok=False,
            status="invalid",
        )
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
        and int(consumed.get("consumed_at_user_message_id") or 0)
        == user_message.id
    ):
        exact_consumed_effect = (
            consumed.get("tool") == tool_name
            and consumed.get("input_digest") == current_digest
        )
        if exact_consumed_effect:
            try:
                recovered = recover_conversation_tool_authorization(
                    state,
                    confirmation_id=str(consumed.get("confirmation_id") or ""),
                    tool=tool_name,
                    input_digest=current_digest,
                    conversation_id=locked_conversation.id,
                    user_message_id=user_message.id,
                    actor_id=current_user.id,
                )
            except ValueError as error:
                db.commit()
                return ConfirmationToolResult(
                    content=f"No se puede recuperar la acción confirmada: {error}",
                    ok=False,
                    status="invalid",
                )
            if recovered is not None:
                db.commit()
                return recovered
        pending_intent = state.get(CONVERSATION_AUTHORIZATION_STATE_KEY)
        if exact_consumed_effect or isinstance(pending_intent, dict):
            # A changed effect must not replace a still-retriable intent. Once
            # that intent has completed and left the ledger, another call in
            # the same model turn may create a separate confirmation proposal.
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
            _mark_confirmation_response_message(
                user_message,
                confirmation_id=confirmation_id,
                outcome="confirmed",
            )
            _record_finalized_confirmation_exchange(
                state,
                pending,
                response_user_message_id=user_message.id,
                outcome="confirmed",
            )
            authorization = issue_conversation_tool_authorization(
                state,
                confirmation_id=confirmation_id,
                tool=tool_name,
                input_digest=current_digest,
                conversation_id=locked_conversation.id,
                user_message_id=user_message.id,
                actor_id=current_user.id,
            )
            dump_conversation_state(locked_conversation, state)
            # This commit is the one-shot boundary. Tool executors run in a new
            # transaction, so their rollback cannot restore the authorization.
            db.commit()
            return authorization

        if pending_matches:
            db.commit()
            return ConfirmationToolResult(
                content=CONFIRMATION_REQUIRED_TOOL_RESULT,
                ok=False,
                status="required",
                confirmation=reference,
            )

        if reference is not None:
            db.commit()
            return ConfirmationToolResult(
                content=CONFIRMATION_IN_PROGRESS_TOOL_RESULT,
                ok=False,
                status="in_progress",
                confirmation=reference,
            )

    reference = _record_pending_confirmation(
        locked_conversation,
        user_message,
        tool_name,
        normalized_input,
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
        _mark_confirmation_response_message(
            user_message,
            confirmation_id=pending.get("confirmation_id"),
            outcome="cancelled",
        )
        _record_finalized_confirmation_exchange(
            state,
            pending,
            response_user_message_id=user_message.id,
            outcome="cancelled",
        )
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
        suffix_metadata = _record_confirmation_suffix_metadata(
            assistant_message,
            confirmation_prompt,
        )
        pending["prompted_at_assistant_message_id"] = assistant_message.id
        pending["confirmation_suffix"] = suffix_metadata

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
    *,
    db: Session,
    current_user: User,
    tool_spec: ToolSpec | None = None,
) -> ConfirmationReference:
    spec = tool_spec or TOOL_CATALOG.get(tool_name)
    if spec is None or not spec.requires_confirmation:
        raise ValueError(f"Tool does not support confirmation: {tool_name}")
    normalized_input = spec.normalize_input(
        db,
        current_user,
        tool_input,
        ToolContext(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
        ),
    )
    existing = _confirmation_reference(
        load_conversation_state(conversation).get("pending_confirmation")
    )
    if existing is not None:
        return existing
    return _record_pending_confirmation(
        conversation,
        user_message,
        tool_name,
        normalized_input,
    )


def _record_pending_confirmation(
    conversation: AssistantConversation,
    user_message: AssistantMessage,
    tool_name: str,
    normalized_input: dict,
) -> ConfirmationReference:
    confirmation_id = secrets.token_urlsafe(12)
    state = load_conversation_state(conversation)
    pending = {
        "confirmation_id": confirmation_id,
        "tool": tool_name,
        "input": deepcopy(normalized_input),
        "input_digest": tool_input_digest(tool_name, normalized_input),
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
        return _render_generic_confirmation_prompt(
            reference,
            input_mode=input_mode,
        )

    display_payload = {
        label: reference.tool_input[field]
        for field, label in REQUIREMENT_CONFIRMATION_FIELDS
        if field in reference.tool_input
    }
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
        if field in reference.tool_input
    }
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


def _render_generic_confirmation_prompt(
    reference: ConfirmationReference,
    *,
    input_mode: str,
) -> str:
    spec = TOOL_CATALOG.get(reference.tool)
    label = spec.label if spec is not None else reference.tool
    visible_input = {key: value for key, value in reference.tool_input.items()
                     if key != "_expected_version"}
    if input_mode == "voice":
        details = "; ".join(
            f"{field.replace('_', ' ')}: {_plain_confirmation_value(value)}"
            for field, value in visible_input.items()
        )
        return (
            f"Acción pendiente de confirmación: {label}. "
            f"Datos exactos: {details or 'sin parámetros'}. "
            "Para ejecutarla, responde: Confirmo; Adelante; o Hazlo."
        )

    serialized = json.dumps(visible_input, ensure_ascii=False, indent=2)
    indented_payload = "\n".join(
        f"    {line}" for line in serialized.splitlines()
    )
    return (
        "### Acción pendiente de confirmación\n\n"
        f"Acción: **{label}** (`{reference.tool}`)\n\n"
        f"Referencia: `{reference.confirmation_id}`\n\n"
        f"{indented_payload}\n\n"
        "Esta acción modificará datos con los parámetros exactos mostrados.\n\n"
        "Responde **Confirmo**, **Adelante** o **Hazlo** solo si son correctos."
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
