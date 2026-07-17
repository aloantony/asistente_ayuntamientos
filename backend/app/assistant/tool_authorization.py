"""Durable, one-shot authorizations for mutating assistant tools.

Tokens are keyed with ``SECRET_KEY``. Rotating that key does not affect a
request already holding its typed token, but a conversation or Agent Office
attempt recovered from durable state cannot reconstruct its token afterward.
That case is detected explicitly and requires a fresh reviewed intent.
"""

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_office.models import AgentOfficeTask, AgentOfficeTaskEvent
from app.assistant.models import AssistantConversation, AssistantMessage
from app.core.config import settings
from app.users.models import User

CONVERSATION_AUTHORIZATION_STATE_KEY = "tool_execution_authorization"
CONVERSATION_EXECUTION_LEDGER_STATE_KEY = "completed_tool_executions"
MAX_CONVERSATION_EXECUTION_LEDGER_ENTRIES = 64
MAX_EXECUTION_LEDGER_CONTENT_CHARS = 2_048
MAX_CONVERSATION_EXECUTION_LEDGER_CONTENT_CHARS = 32_768
AGENT_OFFICE_AUTHORIZATION_EVENT = "tool_authorization_issued"
AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT = "tool_authorization_claimed"
AGENT_OFFICE_EXECUTION_COMPLETED_EVENT = "tool_execution_completed"
AGENT_OFFICE_EXECUTION_STARTED_EVENT = "started"


@dataclass(frozen=True)
class ConversationToolAuthorization:
    token: str
    confirmation_id: str
    tool: str
    input_digest: str
    conversation_id: int
    user_message_id: int
    actor_id: int


@dataclass(frozen=True)
class AgentOfficeToolAuthorization:
    token: str
    task_id: int
    execution_attempt_id: int
    actor_id: int
    tool: str
    input_digest: str
    conversation_id: int | None
    user_message_id: int | None


ToolExecutionAuthorization = (
    ConversationToolAuthorization | AgentOfficeToolAuthorization
)


@dataclass(frozen=True)
class ToolAuthorizationClaim:
    status: Literal["authorized", "completed", "invalid"]
    content: str | None = None
    ok: bool = False


def tool_input_digest(tool_name: str, tool_input: dict) -> str:
    canonical_payload = json.dumps(
        {"tool": tool_name, "input": tool_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def issue_conversation_tool_authorization(
    state: dict,
    *,
    confirmation_id: str,
    tool: str,
    input_digest: str,
    conversation_id: int,
    user_message_id: int,
    actor_id: int,
) -> ConversationToolAuthorization:
    binding = {
        "confirmation_id": confirmation_id,
        "tool": tool,
        "input_digest": input_digest,
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "actor_id": actor_id,
    }
    token = _authorization_token("conversation", binding)
    state[CONVERSATION_AUTHORIZATION_STATE_KEY] = {
        "token_digest": _token_digest(token),
        "token_key_version": _authorization_key_version(),
        **binding,
    }
    return ConversationToolAuthorization(token=token, **binding)


def recover_conversation_tool_authorization(
    state: dict,
    *,
    confirmation_id: str,
    tool: str,
    input_digest: str,
    conversation_id: int,
    user_message_id: int,
    actor_id: int,
) -> ConversationToolAuthorization | None:
    """Rebuild a committed intent after a worker died before its effect.

    Recovery is possible because conversation tokens are deterministic.  The
    stored digest and complete binding are still compared before returning the
    typed authorization, so a different payload cannot reuse the intent.
    """
    stored = state.get(CONVERSATION_AUTHORIZATION_STATE_KEY)
    if not isinstance(stored, dict):
        return None
    if stored.get("token_key_version") != _authorization_key_version():
        raise ValueError(
            "SECRET_KEY rotation invalidated the pending conversation intent"
        )
    binding = {
        "confirmation_id": confirmation_id,
        "tool": tool,
        "input_digest": input_digest,
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "actor_id": actor_id,
    }
    token = _authorization_token("conversation", binding)
    token_digest = _token_digest(token)
    authorization = ConversationToolAuthorization(token=token, **binding)
    if stored.get("token_digest") != token_digest or not (
        _conversation_binding_matches(stored, authorization)
    ):
        return None
    return authorization


def claim_conversation_tool_authorization(
    db: Session,
    authorization: ConversationToolAuthorization,
) -> ToolAuthorizationClaim:
    conversation = db.scalar(
        select(AssistantConversation)
        .where(AssistantConversation.id == authorization.conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")

    state = _load_conversation_state(conversation)
    token_digest = _token_digest(authorization.token)
    completed = _find_conversation_execution(state, token_digest)
    if completed is not None:
        if not _conversation_binding_matches(completed, authorization):
            db.rollback()
            return ToolAuthorizationClaim(status="invalid")
        return ToolAuthorizationClaim(
            status="completed",
            content=str(completed.get("content") or ""),
            ok=bool(completed.get("ok")),
        )

    stored = state.get(CONVERSATION_AUTHORIZATION_STATE_KEY)
    if not isinstance(stored, dict):
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")
    stored_token_digest = stored.get("token_digest")
    if not isinstance(stored_token_digest, str) or not secrets.compare_digest(
        stored_token_digest,
        token_digest,
    ):
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")
    if not _conversation_binding_matches(stored, authorization):
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")
    return ToolAuthorizationClaim(status="authorized")


def issue_agent_office_tool_authorization(
    db: Session,
    *,
    task_id: int,
    actor_id: int,
    tool: str,
    input_digest: str,
) -> AgentOfficeToolAuthorization:
    task = _lock_agent_office_task(db, task_id)
    if task is None:
        db.rollback()
        raise ValueError(f"Agent office task not found: {task_id}")
    actor = db.get(User, actor_id)
    if actor is None or not actor.is_active:
        db.rollback()
        raise ValueError("Agent office tool actor is not active")
    if actor_id not in {task.requested_by_id, task.approved_by_id}:
        db.rollback()
        raise ValueError("Agent office tool actor is not bound to this task")
    if task.status != "running":
        task_status = task.status
        db.rollback()
        raise ValueError(
            f"Agent office task cannot authorize tools from status {task_status}"
        )
    if task.requested_action != tool:
        db.rollback()
        raise ValueError("Agent office authorization tool does not match task action")
    if (
        task.requires_human_approval
        and task.approval_policy == "before_execution"
        and task.approved_by_id is None
    ):
        db.rollback()
        raise ValueError("Agent office task requires human approval")

    started_event = db.scalar(
        select(AgentOfficeTaskEvent)
        .where(
            AgentOfficeTaskEvent.task_id == task.id,
            AgentOfficeTaskEvent.event_type
            == AGENT_OFFICE_EXECUTION_STARTED_EVENT,
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
        .limit(1)
    )
    if started_event is None:
        db.rollback()
        raise ValueError("Agent office task has no execution attempt")

    binding = {
        "task_id": task.id,
        "execution_attempt_id": started_event.id,
        "actor_id": actor.id,
        "tool": tool,
        "input_digest": input_digest,
        "conversation_id": task.source_conversation_id,
        "user_message_id": task.source_message_id,
    }
    token = _authorization_token("agent-office", binding)
    token_digest = _token_digest(token)
    existing = _find_agent_event(
        db,
        task.id,
        AGENT_OFFICE_AUTHORIZATION_EVENT,
        execution_attempt_id=started_event.id,
    )
    if existing is not None:
        payload = existing.payload
        if (
            payload.get("token_key_version")
            != _authorization_key_version()
        ):
            db.rollback()
            raise ValueError(
                "SECRET_KEY rotation invalidated the pending Agent Office intent"
            )
        if (
            payload.get("token_digest") != token_digest
            or not _agent_binding_matches(payload, binding)
        ):
            db.rollback()
            raise ValueError("Agent office execution intent changed")
        return AgentOfficeToolAuthorization(token=token, **binding)

    db.add(
        AgentOfficeTaskEvent(
            task_id=task.id,
            event_type=AGENT_OFFICE_AUTHORIZATION_EVENT,
            message="One-shot tool authorization issued for supervised task.",
            payload_json=json.dumps(
                {
                    "token_digest": token_digest,
                    "token_key_version": _authorization_key_version(),
                    **binding,
                },
                ensure_ascii=False,
            ),
            created_by_id=actor.id,
        )
    )
    db.flush()
    return AgentOfficeToolAuthorization(token=token, **binding)


def claim_agent_office_tool_authorization(
    db: Session,
    authorization: AgentOfficeToolAuthorization,
) -> ToolAuthorizationClaim:
    task = _lock_agent_office_task(db, authorization.task_id)
    if task is None:
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")
    token_digest = _token_digest(authorization.token)
    completed_event = _find_agent_event(
        db,
        task.id,
        AGENT_OFFICE_EXECUTION_COMPLETED_EVENT,
        token_digest=token_digest,
    )
    if completed_event is not None:
        payload = completed_event.payload
        if not _agent_authorization_matches(payload, authorization):
            db.rollback()
            return ToolAuthorizationClaim(status="invalid")
        return ToolAuthorizationClaim(
            status="completed",
            content=str(payload.get("content") or ""),
            ok=bool(payload.get("ok")),
        )

    issued_event = _find_agent_event(
        db,
        task.id,
        AGENT_OFFICE_AUTHORIZATION_EVENT,
        token_digest=token_digest,
    )
    if issued_event is None:
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")
    # Claims written by the pre-ledger implementation are indeterminate. They
    # must never be replayed because their database effect may already exist.
    legacy_claim = _find_agent_event(
        db,
        task.id,
        AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
        token_digest=token_digest,
    )
    if legacy_claim is not None:
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")

    actor = db.get(User, authorization.actor_id)
    valid = (
        actor is not None
        and actor.is_active
        and task.status == "running"
        and task.requested_action == authorization.tool
        and authorization.actor_id in {task.requested_by_id, task.approved_by_id}
        and (
            not task.requires_human_approval
            or task.approval_policy != "before_execution"
            or task.approved_by_id is not None
        )
        and task.source_conversation_id == authorization.conversation_id
        and task.source_message_id == authorization.user_message_id
        and _agent_authorization_matches(issued_event.payload, authorization)
    )
    if not valid:
        db.rollback()
        return ToolAuthorizationClaim(status="invalid")
    return ToolAuthorizationClaim(status="authorized")


def complete_tool_authorization(
    db: Session,
    authorization: ToolExecutionAuthorization,
    *,
    content: str,
    ok: bool,
) -> None:
    if isinstance(authorization, ConversationToolAuthorization):
        _complete_conversation_tool_authorization(
            db,
            authorization,
            content=content,
            ok=ok,
        )
        return
    _complete_agent_office_tool_authorization(
        db,
        authorization,
        content=content,
        ok=ok,
    )


def lock_conversation_tool_turn(
    db: Session,
    *,
    conversation_id: int,
    user_message_id: int,
) -> bool:
    conversation = db.scalar(
        select(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        db.rollback()
        return False
    latest_user_message_id = db.scalar(
        select(AssistantMessage.id)
        .where(
            AssistantMessage.conversation_id == conversation_id,
            AssistantMessage.role == "user",
        )
        .order_by(AssistantMessage.id.desc())
        .limit(1)
    )
    if latest_user_message_id != user_message_id:
        db.rollback()
        return False
    return True


def _complete_conversation_tool_authorization(
    db: Session,
    authorization: ConversationToolAuthorization,
    *,
    content: str,
    ok: bool,
) -> None:
    conversation = db.get(AssistantConversation, authorization.conversation_id)
    if conversation is None:
        raise ValueError("Conversation authorization lost its locked conversation")
    state = _load_conversation_state(conversation)
    token_digest = _token_digest(authorization.token)
    existing = _find_conversation_execution(state, token_digest)
    if existing is not None:
        if not _conversation_binding_matches(existing, authorization):
            raise ValueError("Conversation execution ledger binding changed")
        return
    issued = state.get(CONVERSATION_AUTHORIZATION_STATE_KEY)
    if not isinstance(issued, dict) or (
        issued.get("token_digest") != token_digest
        or not _conversation_binding_matches(issued, authorization)
    ):
        raise ValueError("Conversation execution authorization changed")
    ledger = state.get(CONVERSATION_EXECUTION_LEDGER_STATE_KEY)
    if not isinstance(ledger, list):
        ledger = []
    result_payload = _durable_result_payload(content)
    ledger.append(
        {
            "token_digest": token_digest,
            "confirmation_id": authorization.confirmation_id,
            "tool": authorization.tool,
            "input_digest": authorization.input_digest,
            "conversation_id": authorization.conversation_id,
            "user_message_id": authorization.user_message_id,
            "actor_id": authorization.actor_id,
            "ok": ok,
            **result_payload,
        }
    )
    ledger = ledger[-MAX_CONVERSATION_EXECUTION_LEDGER_ENTRIES:]
    while (
        len(ledger) > 1
        and sum(len(str(entry.get("content") or "")) for entry in ledger)
        > MAX_CONVERSATION_EXECUTION_LEDGER_CONTENT_CHARS
    ):
        ledger.pop(0)
    state[CONVERSATION_EXECUTION_LEDGER_STATE_KEY] = ledger
    state.pop(CONVERSATION_AUTHORIZATION_STATE_KEY, None)
    _dump_conversation_state(conversation, state)
    db.flush()


def _complete_agent_office_tool_authorization(
    db: Session,
    authorization: AgentOfficeToolAuthorization,
    *,
    content: str,
    ok: bool,
) -> None:
    task = db.get(AgentOfficeTask, authorization.task_id)
    if task is None or task.status != "running":
        raise ValueError("Agent office execution lost its locked running task")
    token_digest = _token_digest(authorization.token)
    existing = _find_agent_event(
        db,
        task.id,
        AGENT_OFFICE_EXECUTION_COMPLETED_EVENT,
        token_digest=token_digest,
    )
    if existing is not None:
        if not _agent_authorization_matches(existing.payload, authorization):
            raise ValueError("Agent office execution ledger binding changed")
        return
    issued = _find_agent_event(
        db,
        task.id,
        AGENT_OFFICE_AUTHORIZATION_EVENT,
        token_digest=token_digest,
    )
    if issued is None or not _agent_authorization_matches(
        issued.payload,
        authorization,
    ):
        raise ValueError("Agent office execution authorization changed")
    binding = {
        "task_id": authorization.task_id,
        "execution_attempt_id": authorization.execution_attempt_id,
        "actor_id": authorization.actor_id,
        "tool": authorization.tool,
        "input_digest": authorization.input_digest,
        "conversation_id": authorization.conversation_id,
        "user_message_id": authorization.user_message_id,
    }
    result_payload = _durable_result_payload(content)
    db.add_all(
        [
            AgentOfficeTaskEvent(
                task_id=task.id,
                event_type=AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
                message="One-shot tool authorization claimed with its effect.",
                payload_json=json.dumps(
                    {"token_digest": token_digest, "accepted": True, **binding},
                    ensure_ascii=False,
                ),
                created_by_id=authorization.actor_id,
            ),
            AgentOfficeTaskEvent(
                task_id=task.id,
                event_type=AGENT_OFFICE_EXECUTION_COMPLETED_EVENT,
                message="Authorized tool effect and result completed atomically.",
                payload_json=json.dumps(
                    {
                        "token_digest": token_digest,
                        **binding,
                        "ok": ok,
                        **result_payload,
                    },
                    ensure_ascii=False,
                ),
                created_by_id=authorization.actor_id,
            ),
        ]
    )
    db.flush()


def _find_conversation_execution(state: dict, token_digest: str) -> dict | None:
    ledger = state.get(CONVERSATION_EXECUTION_LEDGER_STATE_KEY)
    if not isinstance(ledger, list):
        return None
    return next(
        (
            entry
            for entry in reversed(ledger)
            if isinstance(entry, dict)
            and entry.get("token_digest") == token_digest
        ),
        None,
    )


def _conversation_binding_matches(
    value: dict,
    authorization: ConversationToolAuthorization,
) -> bool:
    return (
        value.get("confirmation_id") == authorization.confirmation_id
        and value.get("tool") == authorization.tool
        and value.get("input_digest") == authorization.input_digest
        and int(value.get("conversation_id") or 0)
        == authorization.conversation_id
        and int(value.get("user_message_id") or 0)
        == authorization.user_message_id
        and int(value.get("actor_id") or 0) == authorization.actor_id
    )


def _agent_authorization_matches(
    value: dict,
    authorization: AgentOfficeToolAuthorization,
) -> bool:
    return _agent_binding_matches(
        value,
        {
            "task_id": authorization.task_id,
            "execution_attempt_id": authorization.execution_attempt_id,
            "actor_id": authorization.actor_id,
            "tool": authorization.tool,
            "input_digest": authorization.input_digest,
            "conversation_id": authorization.conversation_id,
            "user_message_id": authorization.user_message_id,
        },
    )


def _agent_binding_matches(value: dict, binding: dict) -> bool:
    return all(value.get(key) == expected for key, expected in binding.items())


def _find_agent_event(
    db: Session,
    task_id: int,
    event_type: str,
    *,
    token_digest: str | None = None,
    execution_attempt_id: int | None = None,
) -> AgentOfficeTaskEvent | None:
    events = db.scalars(
        select(AgentOfficeTaskEvent)
        .where(
            AgentOfficeTaskEvent.task_id == task_id,
            AgentOfficeTaskEvent.event_type == event_type,
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
    ).all()
    return next(
        (
            event
            for event in events
            if (
                token_digest is None
                or event.payload.get("token_digest") == token_digest
            )
            and (
                execution_attempt_id is None
                or event.payload.get("execution_attempt_id")
                == execution_attempt_id
            )
        ),
        None,
    )


def _lock_agent_office_task(
    db: Session,
    task_id: int,
) -> AgentOfficeTask | None:
    return db.scalar(
        select(AgentOfficeTask)
        .where(AgentOfficeTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _authorization_token(namespace: str, binding: dict) -> str:
    payload = json.dumps(
        {"namespace": namespace, **binding},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _authorization_key_version() -> str:
    return hashlib.sha256(settings.secret_key.encode("utf-8")).hexdigest()[:12]


def _durable_result_payload(content: str) -> dict:
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if len(content) <= MAX_EXECUTION_LEDGER_CONTENT_CHARS:
        stored_content = content
        content_complete = True
    else:
        stored_content = json.dumps(
            {
                "status": "completed",
                "recovered": True,
                "result_sha256": content_digest,
                "detail": (
                    "El efecto ya se confirmó. El resultado completo excedía "
                    "el límite del ledger y no se reejecutó."
                ),
            },
            ensure_ascii=False,
        )
        content_complete = False
    return {
        "content": stored_content,
        "content_sha256": content_digest,
        "content_complete": content_complete,
    }


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_conversation_state(conversation: AssistantConversation) -> dict:
    if not conversation.state:
        return {}
    try:
        state = json.loads(conversation.state)
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def _dump_conversation_state(
    conversation: AssistantConversation,
    state: dict,
) -> None:
    conversation.state = json.dumps(state, ensure_ascii=False) if state else None
