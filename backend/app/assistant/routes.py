from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assistant.gateway import AIGateway, AssistantUnavailableError, gateway
from app.assistant.models import AssistantConversation, AssistantMemoryEntry
from app.assistant.schemas import (
    AssistantConversationCreate,
    AssistantConversationDetail,
    AssistantConversationRead,
    AssistantConversationUpdate,
    AssistantMemoryEntryRead,
    AssistantMemoryEntryUpdate,
    AssistantStatusRead,
    AssistantUserMessageCreate,
    MemoryStatus,
)
from app.assistant.service import run_agent_turn
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.organizations.access import get_accessible_organizations_query
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/assistant", tags=["assistant"])


def get_gateway() -> AIGateway:
    return gateway


def require_assistant_use(db: Session, current_user: User) -> None:
    if has_permission(current_user, "assistant.use", db):
        return

    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="Permission required: assistant.use",
    )


@router.get("/status", response_model=AssistantStatusRead)
def get_assistant_status(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    agent_gateway: Annotated[AIGateway, Depends(get_gateway)],
) -> AssistantStatusRead:
    require_assistant_use(db, current_user)
    return AssistantStatusRead(
        enabled=agent_gateway.enabled,
        model=settings.assistant_model,
    )


@router.get("/memory", response_model=list[AssistantMemoryEntryRead])
def list_memory_entries(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    status: MemoryStatus | None = "proposed",
    organization_id: int | None = None,
) -> list[AssistantMemoryEntry]:
    require_assistant_use(db, current_user)
    permission_code = (
        "assistant.memory.view" if status == "approved" else "assistant.memory.review"
    )
    organization_ids = get_memory_permission_organization_ids(
        db,
        current_user,
        permission_code,
    )
    if organization_id is not None:
        organization_ids = [
            permitted_id
            for permitted_id in organization_ids
            if permitted_id == organization_id
        ]
    if not organization_ids:
        return []

    query = (
        select(AssistantMemoryEntry)
        .options(
            selectinload(AssistantMemoryEntry.proposed_by),
            selectinload(AssistantMemoryEntry.reviewed_by),
        )
        .where(AssistantMemoryEntry.organization_id.in_(organization_ids))
        .order_by(
            AssistantMemoryEntry.updated_at.desc(),
            AssistantMemoryEntry.id.desc(),
        )
    )
    if status is not None:
        query = query.where(AssistantMemoryEntry.status == status)

    return list(db.scalars(query))


@router.patch(
    "/memory/{entry_id}",
    response_model=AssistantMemoryEntryRead,
)
def update_memory_entry(
    entry_id: int,
    payload: AssistantMemoryEntryUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantMemoryEntry:
    require_assistant_use(db, current_user)
    entry = get_existing_memory_entry(db, entry_id)
    if not has_permission(
        current_user,
        "assistant.memory.review",
        db,
        organization_id=entry.organization_id,
    ):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Permission required: assistant.memory.review",
        )

    updates = payload.model_dump(exclude_unset=True)
    if "content" in updates and updates["content"] is not None:
        entry.content = updates["content"]
    if "category" in updates and updates["category"] is not None:
        entry.category = updates["category"]
    if "sensitivity" in updates and updates["sensitivity"] is not None:
        entry.sensitivity = updates["sensitivity"]
    if "review_notes" in updates:
        entry.review_notes = updates["review_notes"]
    if "status" in updates and updates["status"] is not None:
        entry.status = updates["status"]
        entry.reviewed_by_id = current_user.id
        entry.reviewed_at = datetime.now(timezone.utc)

    db.commit()
    return get_existing_memory_entry(db, entry_id)


@router.get("/conversations", response_model=list[AssistantConversationRead])
def list_conversations(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: bool = False,
) -> list[AssistantConversation]:
    require_assistant_use(db, current_user)

    query = (
        select(AssistantConversation)
        .where(AssistantConversation.created_by_id == current_user.id)
        .order_by(AssistantConversation.updated_at.desc(), AssistantConversation.id.desc())
    )
    if not include_archived:
        query = query.where(AssistantConversation.status == "active")

    return list(db.scalars(query))


@router.post(
    "/conversations",
    response_model=AssistantConversationDetail,
    status_code=http_status.HTTP_201_CREATED,
)
def create_conversation(
    payload: AssistantConversationCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversation:
    require_assistant_use(db, current_user)

    conversation = AssistantConversation(
        title=payload.title or "Conversación",
        created_by_id=current_user.id,
    )
    db.add(conversation)
    db.commit()
    return get_own_conversation(db, current_user, conversation.id)


@router.get(
    "/conversations/{conversation_id}",
    response_model=AssistantConversationDetail,
)
def get_conversation(
    conversation_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversation:
    require_assistant_use(db, current_user)
    return get_own_conversation(db, current_user, conversation_id)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=AssistantConversationDetail,
)
def update_conversation(
    conversation_id: int,
    payload: AssistantConversationUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversation:
    require_assistant_use(db, current_user)
    conversation = get_own_conversation(db, current_user, conversation_id)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if value is not None:
            setattr(conversation, field, value)

    db.commit()
    return get_own_conversation(db, current_user, conversation_id)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=AssistantConversationDetail,
)
def send_message(
    conversation_id: int,
    payload: AssistantUserMessageCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    agent_gateway: Annotated[AIGateway, Depends(get_gateway)],
) -> AssistantConversation:
    require_assistant_use(db, current_user)
    conversation = get_own_conversation(db, current_user, conversation_id)

    if conversation.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conversation is archived",
        )
    if not agent_gateway.enabled:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assistant is not configured",
        )

    try:
        run_agent_turn(
            db,
            current_user,
            conversation,
            payload.content,
            agent_gateway,
        )
    except AssistantUnavailableError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assistant request failed",
        ) from None

    return get_own_conversation(db, current_user, conversation_id)


def get_own_conversation(
    db: Session,
    current_user: User,
    conversation_id: int,
) -> AssistantConversation:
    conversation = db.scalar(
        select(AssistantConversation)
        .options(selectinload(AssistantConversation.messages))
        .where(AssistantConversation.id == conversation_id)
        # The conversation is usually already in the identity map when this
        # runs after a commit; repopulate so the response includes the
        # messages persisted in this request (expire_on_commit is False).
        .execution_options(populate_existing=True)
    )
    # 404 for other users' conversations: they are personal and their
    # existence should not leak across users.
    if conversation is None or conversation.created_by_id != current_user.id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )

    return conversation


def get_memory_permission_organization_ids(
    db: Session,
    current_user: User,
    permission_code: str,
) -> list[int]:
    organizations = db.scalars(
        get_accessible_organizations_query(current_user)
    ).all()
    return [
        organization.id
        for organization in organizations
        if has_permission(
            current_user,
            permission_code,
            db,
            organization_id=organization.id,
        )
    ]


def get_existing_memory_entry(db: Session, entry_id: int) -> AssistantMemoryEntry:
    entry = db.scalar(
        select(AssistantMemoryEntry)
        .options(
            selectinload(AssistantMemoryEntry.proposed_by),
            selectinload(AssistantMemoryEntry.reviewed_by),
        )
        .where(AssistantMemoryEntry.id == entry_id)
        .execution_options(populate_existing=True)
    )
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Assistant memory entry not found",
        )
    return entry
