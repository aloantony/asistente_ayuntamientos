"""Model-first turn engine for the municipal assistant."""

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from time import monotonic

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.assistant.attachments import (
    PreparedAttachment,
    authorize_attachments_for_commit,
    attachment_payload,
    build_turn_attachment_context,
    ensure_attachment_preparation_within_deadline,
    ensure_attachment_runtime_supported,
    extract_attachment_contexts,
    persist_message_attachments,
)
from app.assistant.gateway import (
    AICompletion,
    AITextDelta,
    AIToolUseBlock,
    AIUsage,
    AIGateway,
    AssistantTimeoutError,
    AssistantUnavailableError,
)
from app.assistant.guards import (
    ConfirmationReference,
    ConfirmationToolResult,
    build_confirmation_prompt,
    check_tool_confirmation,
    confirmation_safe_history_content,
    finalize_confirmation_turn,
    load_conversation_state,
    lock_conversation_for_confirmation,
    message_has_confirmation_suffix_metadata,
    message_is_confirmation_response,
    process_pending_confirmation_response,
)
from app.assistant.models import AssistantConversation, AssistantMessage
from app.assistant.prompts import (
    ERROR_REPLY,
    FALLBACK_REPLY,
    REFUSAL_REPLY,
    build_system_prompt,
)
from app.assistant.safety import build_assistant_safety_identifier
from app.assistant.tool_authorization import ConversationToolAuthorization
from app.assistant.tools import (
    ATTACHMENT_CONTENT_TOOL_RESULT,
    MAX_ORDINANCE_TOOL_RESULT_CHARS,
    ToolContext,
    ToolResult,
    ToolSpec,
    REDACTED_UNTRUSTED_TOOL_NAME,
    UNTRUSTED_EXTERNAL_TOOL_BLOCKED,
    canonical_untrusted_web_reader_input,
    execute_tool,
    get_available_tool_specs,
    redacted_untrusted_tool_input,
    tool_is_blocked_after_untrusted_content,
)
from app.core.config import settings
from app.rbac.locking import lock_authorization_graph
from app.users.models import User

logger = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 4000
ATTACHMENT_TOOL_INPUT_REDACTION = {"redacted": True}
ATTACHMENT_TOOL_NAME_REDACTION = "attachment_tool_redacted"
ATTACHMENT_TOOL_CALL_ID_PREFIX = "attachment-tool-call-redacted"
MAX_WEB_READER_AUDIT_REDIRECTS = 6
MAX_WEB_READER_AUDIT_URL_CHARS = 2000


def tool_result_for_activity(tool_name: str, content: str) -> str:
    if tool_name == "read_web_page":
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            return content[:MAX_TOOL_RESULT_CHARS]
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            text = payload["text"]
            redirect_chain = payload.get("redirect_chain")
            if not isinstance(redirect_chain, list):
                redirect_chain = []
            redirect_chain = [
                str(url)[:MAX_WEB_READER_AUDIT_URL_CHARS]
                for url in redirect_chain[:MAX_WEB_READER_AUDIT_REDIRECTS]
            ]
            activity_payload = {
                "source_url": str(payload.get("source_url") or "")[
                    :MAX_WEB_READER_AUDIT_URL_CHARS
                ],
                "final_url": str(payload.get("final_url") or "")[
                    :MAX_WEB_READER_AUDIT_URL_CHARS
                ],
                "title": str(payload.get("title") or "")[:300],
                "query": str(payload.get("query") or "")[:400],
                "provider": str(payload.get("provider") or "")[:100],
                "rank": payload.get("rank"),
                "content_type": str(payload.get("content_type") or "")[:100],
                "content_length_bytes": payload.get("content_length_bytes"),
                "text_char_count": payload.get("text_char_count", len(text)),
                "text_sha256": payload.get("text_sha256")
                or hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text_truncated": payload.get("text_truncated") is True,
                "redirects": payload.get("redirects"),
                "redirect_chain": redirect_chain,
                "untrusted_content": True,
            }
            compact = json.dumps(
                activity_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(compact) < MAX_TOOL_RESULT_CHARS:
                return compact

            chain_bytes = json.dumps(
                redirect_chain,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            activity_payload["redirect_chain"] = {
                "count": len(redirect_chain),
                "sha256": hashlib.sha256(chain_bytes).hexdigest(),
                "summarized": True,
            }
            compact = json.dumps(
                activity_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(compact) < MAX_TOOL_RESULT_CHARS:
                return compact

            source_url = activity_payload["source_url"]
            activity_payload["source_url"] = source_url[:256]
            activity_payload["source_url_sha256"] = hashlib.sha256(
                source_url.encode("utf-8")
            ).hexdigest()
            activity_payload["source_url_truncated"] = len(source_url) > 256
            activity_payload["title"] = activity_payload["title"][:100]
            compact = json.dumps(
                activity_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(compact) < MAX_TOOL_RESULT_CHARS:
                return compact

            # Keep the exact final URL used for citation. The remaining
            # high-variance display fields have hashes or bounded prefixes.
            query = activity_payload["query"]
            activity_payload["query"] = query[:128]
            activity_payload["query_sha256"] = hashlib.sha256(
                query.encode("utf-8")
            ).hexdigest()
            activity_payload["query_truncated"] = len(query) > 128
            activity_payload["title"] = ""
            return json.dumps(
                activity_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
    limit = (
        MAX_ORDINANCE_TOOL_RESULT_CHARS
        if tool_name in {"semantic_search_ordinances", "read_ordinance_chunk"}
        else MAX_TOOL_RESULT_CHARS
    )
    return content[:limit]


TOOL_CALL_BUDGET_RESULT = (
    "No se ejecutó la herramienta porque se agotó el presupuesto total de "
    "llamadas de este turno. Resume los resultados ya disponibles y explica "
    "honestamente cualquier comprobación pendiente."
)
TOOL_ROUND_BUDGET_RESULT = (
    "No se ejecutó la herramienta porque se agotó el presupuesto de rondas "
    "de este turno. Resume los resultados ya disponibles y explica "
    "honestamente cualquier comprobación pendiente."
)
REPEATED_TOOL_CALL_RESULT = (
    "No se volvió a ejecutar la herramienta porque repite una llamada "
    "equivalente ya realizada en este turno. Usa el resultado anterior y "
    "responde al usuario sin volver a intentarlo."
)
FAILED_TOOL_RETRY_RESULT = (
    "No se volvió a ejecutar la herramienta porque ya falló en este turno con "
    "un error que no se resolverá cambiando la consulta. Explica la "
    "indisponibilidad al usuario y continúa sin esta herramienta."
)
FINALIZATION_PENDING_TOOL_RESULT = (
    "No se ejecutó la herramienta porque el turno ya está cerrando su fase de "
    "consultas. Usa los resultados disponibles para responder al usuario."
)
TOOL_LOOP_LIMIT_REPLY = (
    "He detenido las consultas para evitar un bucle. No he podido completar "
    "todas las comprobaciones; puedes pedirme que reintente la parte pendiente."
)
TURN_TIMEOUT_REPLY = (
    "He detenido el turno porque alcanzó su límite de tiempo. Las operaciones "
    "que ya aparecen como completadas sí terminaron; puedes pedirme que retome "
    "la parte pendiente en un nuevo mensaje."
)
TURN_TIMEOUT_TOOL_RESULT = (
    "No se ejecutó la herramienta porque el turno alcanzó su límite de tiempo. "
    "No inicies más operaciones y responde con lo ya comprobado."
)
TOOL_LOOP_FINALIZATION_INSTRUCTION = """

CIERRE OBLIGATORIO DEL TURNO
Se ha alcanzado un límite de seguridad de herramientas. Las herramientas están
deshabilitadas para esta última respuesta. Redacta ahora una respuesta final y
autosuficiente para el usuario basándote únicamente en los resultados que ya
figuran en la conversación. No anuncies nuevas consultas ni prometas seguir
trabajando. Distingue lo comprobado de lo que quedó pendiente y explica de forma
breve cualquier limitación o error de herramienta.
""".strip()
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
    prepared_attachments: list[PreparedAttachment] | None = None,
) -> Generator[TurnEvent, None, AssistantMessage]:
    """Stream a turn and reconcile speculative text before its terminal event."""
    streamed_text: list[str] = []
    events = _run_agent_turn_events(
        db,
        current_user,
        conversation,
        user_text,
        gateway,
        input_mode=input_mode,
        prepared_attachments=prepared_attachments,
    )
    try:
        while True:
            try:
                event = next(events)
            except StopIteration as stop:
                return stop.value
            if event.type == "text_delta":
                streamed_text.append(str(event.data.get("text", "")))
            elif event.type == "done" and streamed_text:
                message = event.data.get("message") or {}
                canonical_text = str(message.get("content", ""))
                if "".join(streamed_text) != canonical_text:
                    yield TurnEvent("text_reset", {"text": canonical_text})
            yield event
    finally:
        events.close()


def _run_agent_turn_events(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    user_text: str,
    gateway: AIGateway,
    *,
    input_mode: str = "text",
    prepared_attachments: list[PreparedAttachment] | None = None,
) -> Generator[TurnEvent, None, AssistantMessage]:
    """Persist the user message, run the tool loop and stream turn events."""
    turn_deadline = monotonic() + settings.assistant_turn_timeout_seconds
    conversation_id = conversation.id
    current_user_id = current_user.id
    current_attachments = prepared_attachments or []
    ensure_attachment_runtime_supported(bool(current_attachments))
    if current_attachments and input_mode != "text":
        raise ValueError("Assistant attachments are supported only for text input")
    authorization_graph_lock = None
    if current_attachments:
        # PreparedAttachment contains only an immutable identity snapshot. A
        # rollback, rather than a commit, both releases preflight locks and
        # discards ORM *and Core* DML accidentally left by a caller. No ORM
        # attribute is touched again until rows are reloaded after extraction.
        db.rollback()
    current_attachments = extract_attachment_contexts(
        current_attachments,
        turn_deadline=turn_deadline,
    )
    if current_attachments:
        # Canonical order is RBAC graph -> conversation -> user -> document ->
        # project. In particular, user deletion also takes the graph lock
        # first and can cascade to conversations without creating an inversion.
        authorization_graph_lock = lock_authorization_graph(db)

    # Lock before inserting the message: concurrent FK inserts followed by a
    # row-lock upgrade can deadlock. The first commit releases this short lock.
    conversation = lock_conversation_for_confirmation(db, conversation_id)
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
    try:
        db.flush()
        confirmation_context = process_pending_confirmation_response(
            db,
            conversation,
            user_message,
        )
        # This is the linear authorization boundary for attachment turns.
        # Authorization evidence and document identity remain locked until the
        # message+relation commit below, then no lock crosses provider I/O.
        if current_attachments:
            if authorization_graph_lock is None:
                raise RuntimeError("Attachment authorization lock is missing")
            current_attachments = authorize_attachments_for_commit(
                db,
                current_user_id,
                current_attachments,
                turn_deadline=turn_deadline,
                authorization_lock=authorization_graph_lock,
            )
        persist_message_attachments(
            db,
            user_message,
            current_attachments,
            authorized_by_id=current_user_id,
        )
        db.flush()
        if current_attachments:
            ensure_attachment_preparation_within_deadline(turn_deadline)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user_message)
    db.refresh(conversation)

    yield TurnEvent(
        "message_start",
        {
            "conversation_id": conversation.id,
            "user_message_id": user_message.id,
            "user_message": _message_payload(user_message),
        },
    )

    attachment_tainted = bool(current_attachments)
    tools = [] if attachment_tainted else get_available_tool_specs(db, current_user)
    tools_by_name = {tool.name: tool for tool in tools}
    tool_definitions = [tool.definition for tool in tools]
    tool_names = frozenset(tool.name for tool in tools)
    tool_context = ToolContext(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
    )
    system = build_system_prompt(db, current_user, tools, input_mode=input_mode)
    messages = build_history(
        conversation,
        attachment_context_message_id=user_message.id,
        attachment_context=build_turn_attachment_context(current_attachments),
    )
    safety_identifier = build_assistant_safety_identifier(current_user.id)

    actions: list[dict] = []
    reply_text = ""
    tool_calls_used = 0
    seen_read_calls: set[str] = set()
    unavailable_read_tools: set[str] = set()
    iterations_remaining = max(0, settings.assistant_max_tool_iterations)
    tool_call_budget = max(0, settings.assistant_max_tool_calls)
    try:
        response = yield from _complete_with_events(
            gateway,
            system=system,
            messages=messages,
            tools=tool_definitions,
            timeout_seconds=_remaining_gateway_timeout(turn_deadline),
            safety_identifier=safety_identifier,
        )

        while True:
            if attachment_tainted:
                response = _redact_attachment_tool_completion(gateway, response)
            response = recover_textual_read_tool_call(response, tools, messages)

            if response.stop_reason == "refusal":
                reply_text = REFUSAL_REPLY
                break

            if response.stop_reason == "pause_turn":
                messages.append(_assistant_response_message(response))
                if iterations_remaining <= 0:
                    reply_text = yield from _complete_forced_synthesis(
                        gateway,
                        system=system,
                        messages=messages,
                        reason="iteration_budget",
                        conversation_id=conversation.id,
                        turn_deadline=turn_deadline,
                        safety_identifier=safety_identifier,
                    )
                    break
                iterations_remaining -= 1
                response = yield from _complete_with_events(
                    gateway,
                    system=system,
                    messages=messages,
                    tools=tool_definitions,
                    timeout_seconds=_remaining_gateway_timeout(turn_deadline),
                    safety_identifier=safety_identifier,
                )
                continue

            if response.stop_reason != "tool_use":
                reply_text = sanitize_model_reply(extract_text(response.content))
                break

            # Retain opaque provider state before yielding tool activity. If an
            # SSE client disconnects at either activity event, the outer
            # ``finally`` can still discard the paused provider session.
            messages.append(_assistant_response_message(response))
            force_synthesis_reason: str | None = None
            execute_round = iterations_remaining > 0
            if execute_round:
                iterations_remaining -= 1
            else:
                force_synthesis_reason = "iteration_budget"

            tool_results: list[dict] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                raw_tool_input = dict(block.input)
                audited_tool_input = (
                    dict(ATTACHMENT_TOOL_INPUT_REDACTION)
                    if attachment_tainted
                    else raw_tool_input
                )
                tool = tools_by_name.get(block.name)
                post_taint_blocked = (
                    False
                    if attachment_tainted
                    else tool_is_blocked_after_untrusted_content(
                        block.name,
                        raw_tool_input,
                        tool_context,
                        allow_web_reader=True,
                    )
                )
                canonical_reader_input = (
                    None
                    if attachment_tainted
                    else canonical_untrusted_web_reader_input(
                        block.name,
                        raw_tool_input,
                        tool_context,
                        allow_web_reader=True,
                    )
                )
                persisted_tool_input = (
                    dict(ATTACHMENT_TOOL_INPUT_REDACTION)
                    if attachment_tainted
                    else (
                        redacted_untrusted_tool_input()
                        if post_taint_blocked
                        else canonical_reader_input or raw_tool_input
                    )
                )
                persisted_tool_name = (
                    ATTACHMENT_TOOL_NAME_REDACTION
                    if attachment_tainted
                    else (
                        REDACTED_UNTRUSTED_TOOL_NAME
                        if post_taint_blocked
                        else block.name
                    )
                )
                yield TurnEvent(
                    "tool_activity",
                    {
                        "tool": persisted_tool_name,
                        "status": "started",
                        "input": persisted_tool_input,
                    },
                )
                signature = tool_call_signature(block.name, audited_tool_input)
                track_repetition = tool is None or tool.read_only
                if attachment_tainted:
                    # The provider receives no tool definitions for attachment
                    # turns, but a hallucinated tool block must still fail
                    # closed if it reaches this code path. Never pass its
                    # untrusted arguments to confirmations or executors.
                    result = _execute_tool_for_current_turn(
                        db=db,
                        current_user=current_user,
                        tool_name=block.name,
                        tool_input={},
                        context=ToolContext(
                            conversation_id=conversation.id,
                            user_message_id=user_message.id,
                            attachment_content_seen=True,
                        ),
                        allowed=tool_names,
                        attachment_tainted=True,
                    )
                    force_synthesis_reason = "attachment_tools_disabled"
                elif _turn_timed_out(turn_deadline):
                    result = ToolResult(content=TURN_TIMEOUT_TOOL_RESULT, ok=False)
                    force_synthesis_reason = "turn_timeout"
                elif not execute_round:
                    result = ToolResult(content=TOOL_ROUND_BUDGET_RESULT, ok=False)
                elif force_synthesis_reason is not None:
                    result = ToolResult(
                        content=FINALIZATION_PENDING_TOOL_RESULT,
                        ok=False,
                    )
                elif track_repetition and block.name in unavailable_read_tools:
                    result = ToolResult(content=FAILED_TOOL_RETRY_RESULT, ok=False)
                    force_synthesis_reason = "failed_tool_retry"
                elif track_repetition and signature in seen_read_calls:
                    result = ToolResult(content=REPEATED_TOOL_CALL_RESULT, ok=False)
                    force_synthesis_reason = "repeated_tool_call"
                elif tool_calls_used >= tool_call_budget:
                    result = ToolResult(content=TOOL_CALL_BUDGET_RESULT, ok=False)
                    force_synthesis_reason = "tool_call_budget"
                elif post_taint_blocked:
                    result = ToolResult(
                        content=UNTRUSTED_EXTERNAL_TOOL_BLOCKED,
                        ok=False,
                    )
                else:
                    tool_calls_used += 1
                    guarded_result = None
                    # A provider must not be able to arm confirmation state for
                    # a tool omitted from this turn's code-level allowlist.
                    if tool is not None:
                        guarded_result = check_tool_confirmation(
                            db,
                            conversation,
                            user_message,
                            block.name,
                            raw_tool_input,
                            current_user=current_user,
                            tool_spec=tool,
                        )
                    if tool is not None:
                        required_confirmation = _confirmation_context_from_result(
                            guarded_result
                        )
                        if required_confirmation is not None:
                            confirmation_context = required_confirmation
                    if isinstance(guarded_result, ConfirmationToolResult):
                        result = guarded_result
                    else:
                        authorization = (
                            guarded_result
                            if isinstance(
                                guarded_result,
                                ConversationToolAuthorization,
                            )
                            else None
                        )
                        result = _execute_tool_for_current_turn(
                            db=db,
                            current_user=current_user,
                            tool_name=block.name,
                            tool_input=raw_tool_input,
                            context=tool_context,
                            allowed=tool_names,
                            authorization=authorization,
                            attachment_tainted=False,
                        )
                    if track_repetition:
                        seen_read_calls.add(signature)
                        if _is_non_retryable_tool_failure(result):
                            unavailable_read_tools.add(block.name)
                    elif result.ok:
                        # A successful mutation can make an identical read useful
                        # again later in this same turn.
                        seen_read_calls.clear()
                action = {
                    "tool": persisted_tool_name,
                    "ok": result.ok,
                    "input": persisted_tool_input,
                    "result": tool_result_for_activity(
                        persisted_tool_name,
                        result.content,
                    ),
                }
                actions.append(action)
                yield TurnEvent(
                    "tool_activity",
                    {
                        "tool": persisted_tool_name,
                        "status": "finished",
                        "input": persisted_tool_input,
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

            messages.append({"role": "user", "content": tool_results})
            if _turn_timed_out(turn_deadline):
                force_synthesis_reason = "turn_timeout"
            elif not tool_results:
                force_synthesis_reason = "empty_tool_response"
            elif iterations_remaining <= 0:
                force_synthesis_reason = (
                    force_synthesis_reason or "iteration_budget"
                )
            elif tool_calls_used >= tool_call_budget:
                force_synthesis_reason = (
                    force_synthesis_reason or "tool_call_budget"
                )

            if force_synthesis_reason is not None:
                if force_synthesis_reason == "turn_timeout":
                    reply_text = TURN_TIMEOUT_REPLY
                    yield TurnEvent("text_delta", {"text": reply_text})
                else:
                    reply_text = yield from _complete_forced_synthesis(
                        gateway,
                        system=system,
                        messages=messages,
                        reason=force_synthesis_reason,
                        conversation_id=conversation.id,
                        turn_deadline=turn_deadline,
                        safety_identifier=safety_identifier,
                    )
                break

            response = yield from _complete_with_events(
                gateway,
                system=system,
                messages=messages,
                tools=tool_definitions,
                timeout_seconds=_remaining_gateway_timeout(turn_deadline),
                safety_identifier=safety_identifier,
            )
    except AssistantTimeoutError:
        logger.warning(
            "Assistant turn reached its time budget (conversation=%s)",
            conversation.id,
        )
        reply_text = TURN_TIMEOUT_REPLY
        yield TurnEvent("text_delta", {"text": reply_text})
    except AssistantUnavailableError:
        logger.warning(
            "Assistant gateway failed mid-turn (conversation=%s)",
            conversation.id,
        )
        reply_text = ERROR_REPLY
    finally:
        discard_provider_state = getattr(gateway, "discard_provider_state", None)
        if callable(discard_provider_state):
            try:
                discard_provider_state(messages)
            except Exception:
                logger.warning(
                    "Assistant gateway state cleanup failed (conversation=%s)",
                    conversation.id,
                )

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
            "user_message": _message_payload(user_message),
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


def tool_call_signature(tool_name: str, tool_input: dict) -> str:
    """Return a stable signature for superficially equivalent tool inputs."""
    normalized = _normalize_tool_call_value(tool_input)
    return f"{tool_name}:{json.dumps(normalized, sort_keys=True, separators=(',', ':'))}"


def _redact_attachment_tool_completion(
    gateway: AIGateway,
    response: AICompletion,
) -> AICompletion:
    """Remove model-controlled tool/provider fields before local reuse or audit."""

    provider_state = getattr(response, "provider_state", ())
    discard_provider_state = getattr(gateway, "discard_provider_state", None)
    if provider_state and callable(discard_provider_state):
        try:
            discard_provider_state([{"provider_state": provider_state}])
        except Exception:
            logger.warning("Attachment provider state cleanup failed")

    redacted_content = []
    tool_index = 0
    for block in response.content:
        if block.type != "tool_use":
            redacted_content.append(block)
            continue
        tool_index += 1
        redacted_content.append(
            AIToolUseBlock(
                id=f"{ATTACHMENT_TOOL_CALL_ID_PREFIX}-{tool_index}",
                name=ATTACHMENT_TOOL_NAME_REDACTION,
                input=dict(ATTACHMENT_TOOL_INPUT_REDACTION),
            )
        )

    return AICompletion(
        model=response.model,
        stop_reason=response.stop_reason,
        content=redacted_content,
        usage=response.usage,
        provider_state=(),
    )


def _remaining_gateway_timeout(turn_deadline: float) -> float:
    remaining = turn_deadline - monotonic()
    if remaining <= 0:
        raise AssistantTimeoutError("Assistant turn timed out")
    return min(settings.assistant_gateway_timeout_seconds, remaining)


def _turn_timed_out(turn_deadline: float) -> bool:
    return monotonic() >= turn_deadline


def _normalize_tool_call_value(value):
    if isinstance(value, dict):
        return {
            str(key): _normalize_tool_call_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_normalize_tool_call_value(item) for item in value]
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _is_non_retryable_tool_failure(result: ToolResult) -> bool:
    if result.ok:
        return False
    normalized = result.content.casefold()
    return any(
        marker in normalized
        for marker in (
            "error (401)",
            "error (403)",
            "error (503)",
            "herramienta desconocida",
            "herramienta no disponible",
            "no está configurad",
            "no esta configurad",
        )
    )


def _complete_forced_synthesis(
    gateway: AIGateway,
    *,
    system: str,
    messages: list[dict],
    reason: str,
    conversation_id: int,
    turn_deadline: float,
    safety_identifier: str,
) -> Generator[TurnEvent, None, str]:
    """Complete once without tools and only publish a valid final answer."""
    logger.warning(
        "Assistant tool loop forced final synthesis (conversation=%s reason=%s)",
        conversation_id,
        reason,
    )
    final_system = f"{system}\n\n{TOOL_LOOP_FINALIZATION_INSTRUCTION}"
    completion_events = _complete_with_events(
        gateway,
        system=final_system,
        messages=messages,
        tools=[],
        timeout_seconds=_remaining_gateway_timeout(turn_deadline),
        safety_identifier=safety_identifier,
    )
    buffered_events: list[TurnEvent] = []
    while True:
        try:
            buffered_events.append(next(completion_events))
        except StopIteration as stop:
            completion = stop.value
            break

    if completion.stop_reason == "refusal":
        reply_text = REFUSAL_REPLY
        buffered_events = []
    elif completion.stop_reason in {"tool_use", "pause_turn"}:
        reply_text = TOOL_LOOP_LIMIT_REPLY
        buffered_events = []
    else:
        reply_text = sanitize_model_reply(extract_text(completion.content))
        if not reply_text:
            reply_text = TOOL_LOOP_LIMIT_REPLY
            buffered_events = []

    buffered_text = "".join(
        str(event.data.get("text", ""))
        for event in buffered_events
        if event.type == "text_delta"
    )
    if buffered_events and buffered_text != reply_text:
        buffered_events = []

    if buffered_events:
        yield from buffered_events
    elif reply_text:
        yield TurnEvent("text_delta", {"text": reply_text})
    return reply_text


def _execute_tool_for_current_turn(
    *,
    db: Session,
    current_user: User,
    tool_name: str,
    tool_input: dict,
    context: ToolContext,
    allowed: frozenset[str],
    authorization: ConversationToolAuthorization | None = None,
    attachment_tainted: bool = False,
) -> ToolResult:
    if attachment_tainted:
        if not context.attachment_content_seen:
            return ToolResult(content=ATTACHMENT_CONTENT_TOOL_RESULT, ok=False)
        return execute_tool(
            db,
            current_user,
            tool_name,
            tool_input,
            context,
            allowed=allowed,
        )

    execution_kwargs = {"allowed": allowed}
    if authorization is not None:
        execution_kwargs["authorization"] = authorization
    return execute_tool(
        db,
        current_user,
        tool_name,
        tool_input,
        context,
        **execution_kwargs,
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
    prepared_attachments: list[PreparedAttachment] | None = None,
) -> AssistantMessage:
    events = run_agent_turn_events(
        db,
        current_user,
        conversation,
        user_text,
        gateway,
        input_mode=input_mode,
        prepared_attachments=prepared_attachments,
    )
    while True:
        try:
            next(events)
        except StopIteration as stop:
            return stop.value


def build_history(
    conversation: AssistantConversation,
    *,
    attachment_context_message_id: int | None = None,
    attachment_context: str = "",
) -> list[dict]:
    state = load_conversation_state(conversation)
    legacy_prompt_message_ids: set[int] = set()
    confirmation_response_message_ids: set[int] = set()
    exchanges = state.get("finalized_confirmation_exchanges")
    if isinstance(exchanges, list):
        for exchange in exchanges:
            if not isinstance(exchange, dict):
                continue
            prompt_message_id = int(
                exchange.get("prompted_at_assistant_message_id") or 0
            )
            if prompt_message_id:
                legacy_prompt_message_ids.add(prompt_message_id)
            response_message_id = int(exchange.get("response_user_message_id") or 0)
            if response_message_id:
                confirmation_response_message_ids.add(response_message_id)
    # Backward compatibility for confirmations finalized before the bounded
    # exchange list existed.
    for key in ("last_consumed_confirmation", "last_cancelled_confirmation"):
        confirmation = state.get(key)
        if not isinstance(confirmation, dict):
            continue
        message_id = int(confirmation.get("prompted_at_assistant_message_id") or 0)
        if message_id:
            legacy_prompt_message_ids.add(message_id)
        response_message_id = int(
            confirmation.get("confirmed_at_user_message_id")
            or confirmation.get("cancelled_at_user_message_id")
            or 0
        )
        if response_message_id:
            confirmation_response_message_ids.add(response_message_id)
    messages: list[dict] = []
    for message in conversation.messages:
        if not message.content:
            continue
        if (
            message.id in confirmation_response_message_ids
            or message_is_confirmation_response(message)
        ):
            continue
        content = confirmation_safe_history_content(message)
        if (
            message.id in legacy_prompt_message_ids
            and not message_has_confirmation_suffix_metadata(message)
        ):
            continue
        if content:
            if (
                message.id == attachment_context_message_id
                and attachment_context
            ):
                content = f"{content}\n\n{attachment_context}"
            messages.append({"role": message.role, "content": content})
    max_messages = max(2, settings.assistant_history_max_messages)
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]


def _assistant_response_message(response: AICompletion) -> dict:
    message = {"role": "assistant", "content": response.content}
    provider_state = getattr(response, "provider_state", ())
    if provider_state:
        message["provider_state"] = provider_state
    return message


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
    timeout_seconds: float,
    safety_identifier: str,
) -> Generator[TurnEvent, None, AICompletion]:
    call_deadline = monotonic() + timeout_seconds
    complete_stream = getattr(gateway, "complete_stream", None)
    if complete_stream is None:
        completion = gateway.complete(
            system=system,
            messages=messages,
            tools=tools,
            timeout_seconds=timeout_seconds,
            safety_identifier=safety_identifier,
        )
        if monotonic() >= call_deadline:
            raise AssistantTimeoutError("Assistant gateway call timed out")
        text = sanitize_model_reply(extract_text(completion.content))
        if text:
            yield TurnEvent("text_delta", {"text": text})
        return completion

    stream = complete_stream(
        system=system,
        messages=messages,
        tools=tools,
        timeout_seconds=timeout_seconds,
        safety_identifier=safety_identifier,
    )
    while True:
        try:
            delta = next(stream)
        except StopIteration as stop:
            if monotonic() >= call_deadline:
                raise AssistantTimeoutError("Assistant gateway call timed out")
            return stop.value
        if monotonic() >= call_deadline:
            close_stream = getattr(stream, "close", None)
            if close_stream is not None:
                close_stream()
            raise AssistantTimeoutError("Assistant gateway call timed out")
        text = getattr(delta, "text", "")
        if text:
            yield TurnEvent("text_delta", {"text": text})


def _message_payload(message: AssistantMessage) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "actions": json.loads(message.actions) if message.actions else [],
        "attachments": [
            attachment_payload(attachment) for attachment in message.attachments
        ],
        "agent_key": message.agent_key,
        "routing": json.loads(message.routing) if message.routing else None,
        "created_at": message.created_at.isoformat(),
    }
