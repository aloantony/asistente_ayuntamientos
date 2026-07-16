"""One-shot, typed authorizations for mutating assistant tools."""

import hashlib
import json
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_office.models import AgentOfficeTask, AgentOfficeTaskEvent
from app.assistant.models import AssistantConversation, AssistantMessage
from app.users.models import User

CONVERSATION_AUTHORIZATION_STATE_KEY = "tool_execution_authorization"
AGENT_OFFICE_AUTHORIZATION_EVENT = "tool_authorization_issued"
AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT = "tool_authorization_claimed"


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
    actor_id: int
    tool: str
    input_digest: str
    conversation_id: int | None
    user_message_id: int | None


ToolExecutionAuthorization = (
    ConversationToolAuthorization | AgentOfficeToolAuthorization
)


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
    token = secrets.token_urlsafe(24)
    authorization = ConversationToolAuthorization(
        token=token,
        confirmation_id=confirmation_id,
        tool=tool,
        input_digest=input_digest,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        actor_id=actor_id,
    )
    state[CONVERSATION_AUTHORIZATION_STATE_KEY] = {
        "token_digest": _token_digest(token),
        "confirmation_id": confirmation_id,
        "tool": tool,
        "input_digest": input_digest,
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "actor_id": actor_id,
    }
    return authorization


def claim_conversation_tool_authorization(
    db: Session,
    authorization: ConversationToolAuthorization,
) -> bool:
    conversation = db.scalar(
        select(AssistantConversation)
        .where(AssistantConversation.id == authorization.conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        db.rollback()
        return False
    state = _load_conversation_state(conversation)
    stored = state.get(CONVERSATION_AUTHORIZATION_STATE_KEY)
    if not isinstance(stored, dict):
        db.rollback()
        return False
    stored_token_digest = stored.get("token_digest")
    if not isinstance(stored_token_digest, str) or not secrets.compare_digest(
        stored_token_digest,
        _token_digest(authorization.token),
    ):
        db.rollback()
        return False

    valid = (
        stored.get("confirmation_id") == authorization.confirmation_id
        and stored.get("tool") == authorization.tool
        and stored.get("input_digest") == authorization.input_digest
        and int(stored.get("conversation_id") or 0)
        == authorization.conversation_id
        and int(stored.get("user_message_id") or 0)
        == authorization.user_message_id
        and int(stored.get("actor_id") or 0) == authorization.actor_id
    )
    # Possession of the issued token permits one claim attempt only. A bad
    # payload/context cannot preserve it for a later replay.
    state.pop(CONVERSATION_AUTHORIZATION_STATE_KEY, None)
    _dump_conversation_state(conversation, state)
    db.commit()
    return valid


def issue_agent_office_tool_authorization(
    db: Session,
    *,
    task_id: int,
    actor_id: int,
    tool: str,
    input_digest: str,
) -> AgentOfficeToolAuthorization:
    task = db.scalar(
        select(AgentOfficeTask)
        .where(AgentOfficeTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
    task_status = task.status
    if task_status not in {"approved", "queued", "running"}:
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

    token = secrets.token_urlsafe(24)
    payload = {
        "token_digest": _token_digest(token),
        "task_id": task.id,
        "actor_id": actor.id,
        "tool": tool,
        "input_digest": input_digest,
        "conversation_id": task.source_conversation_id,
        "user_message_id": task.source_message_id,
    }
    db.add(
        AgentOfficeTaskEvent(
            task_id=task.id,
            event_type=AGENT_OFFICE_AUTHORIZATION_EVENT,
            message="One-shot tool authorization issued for supervised task.",
            payload_json=json.dumps(payload, ensure_ascii=False),
            created_by_id=actor.id,
        )
    )
    db.commit()
    return AgentOfficeToolAuthorization(
        token=token,
        task_id=task.id,
        actor_id=actor.id,
        tool=tool,
        input_digest=input_digest,
        conversation_id=task.source_conversation_id,
        user_message_id=task.source_message_id,
    )


def claim_agent_office_tool_authorization(
    db: Session,
    authorization: AgentOfficeToolAuthorization,
) -> bool:
    task = db.scalar(
        select(AgentOfficeTask)
        .where(AgentOfficeTask.id == authorization.task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        db.rollback()
        return False
    issued_events = db.scalars(
        select(AgentOfficeTaskEvent)
        .where(
            AgentOfficeTaskEvent.task_id == task.id,
            AgentOfficeTaskEvent.event_type
            == AGENT_OFFICE_AUTHORIZATION_EVENT,
        )
        .order_by(AgentOfficeTaskEvent.id.desc())
    ).all()
    token_digest = _token_digest(authorization.token)
    issued_event = next(
        (
            event
            for event in issued_events
            if event.payload.get("token_digest") == token_digest
        ),
        None,
    )
    if issued_event is None:
        db.rollback()
        return False
    claimed_token_digests = {
        event.payload.get("token_digest")
        for event in db.scalars(
            select(AgentOfficeTaskEvent)
            .where(
                AgentOfficeTaskEvent.task_id == task.id,
                AgentOfficeTaskEvent.event_type
                == AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
            )
            .order_by(AgentOfficeTaskEvent.id.desc())
        ).all()
    }
    if token_digest in claimed_token_digests:
        db.rollback()
        return False

    issued = issued_event.payload
    actor = db.get(User, authorization.actor_id)
    valid = (
        actor is not None
        and actor.is_active
        and task.status in {"approved", "queued", "running"}
        and task.requested_action == authorization.tool
        and authorization.actor_id in {task.requested_by_id, task.approved_by_id}
        and (
            not task.requires_human_approval
            or task.approval_policy != "before_execution"
            or task.approved_by_id is not None
        )
        and int(issued.get("task_id") or 0) == authorization.task_id
        and int(issued.get("actor_id") or 0) == authorization.actor_id
        and issued.get("tool") == authorization.tool
        and issued.get("input_digest") == authorization.input_digest
        and issued.get("conversation_id") == authorization.conversation_id
        and issued.get("user_message_id") == authorization.user_message_id
        and task.source_conversation_id == authorization.conversation_id
        and task.source_message_id == authorization.user_message_id
    )
    db.add(
        AgentOfficeTaskEvent(
            task_id=task.id,
            event_type=AGENT_OFFICE_AUTHORIZATION_CLAIM_EVENT,
            message="One-shot tool authorization claimed.",
            payload_json=json.dumps(
                {"token_digest": token_digest, "accepted": valid},
                ensure_ascii=False,
            ),
            created_by_id=actor.id if actor is not None else None,
        )
    )
    db.commit()
    return valid


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
