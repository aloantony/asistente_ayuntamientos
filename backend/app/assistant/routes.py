import json
from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status as http_status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.assistant.gateway import AIGateway, AssistantUnavailableError, gateway
from app.assistant.models import (
    AssistantAdminFeedback,
    AssistantConversation,
    AssistantConversationFolder,
    AssistantMemoryEntry,
    AssistantTransversalFeature,
    AssistantTransversalFeatureAdoption,
)
from app.assistant.schemas import (
    AdminFeedbackStatus,
    AssistantAdminFeedbackRead,
    AssistantAdminFeedbackUpdate,
    AssistantAudioTranscriptionRead,
    AssistantConversationCreate,
    AssistantConversationDetail,
    AssistantConversationFolderCreate,
    AssistantConversationFolderRead,
    AssistantConversationFolderUpdate,
    AssistantConversationRead,
    AssistantConversationUpdate,
    AssistantMemoryEntryRead,
    AssistantMemoryEntryUpdate,
    AssistantStatusRead,
    AssistantTransversalFeatureAdoptionRead,
    AssistantTransversalFeatureAdoptionUpdate,
    AssistantTransversalFeatureRead,
    AssistantTransversalFeatureUpdate,
    AssistantUserMessageCreate,
    MemoryStatus,
    TransversalFeatureAdoptionStatus,
    TransversalFeatureStatus,
)
from app.assistant.turn import TurnEvent, run_agent_turn, run_agent_turn_events
from app.assistant.speech import SpeechTranscriptionError, transcribe_audio_bytes
from app.assistant.tools import get_available_tool_specs
from app.auth.dependencies import get_current_user, require_superuser
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
        runtime=settings.assistant_runtime,
        model=(
            settings.hermes_agent_model
            if settings.assistant_runtime == "hermes_agent"
            else settings.assistant_model
        ),
        runtime_healthy=getattr(agent_gateway, "runtime_healthy", None),
        tools=[tool.metadata for tool in get_available_tool_specs(db, current_user)],
    )


@router.post("/audio-transcriptions", response_model=AssistantAudioTranscriptionRead)
async def transcribe_audio(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
) -> AssistantAudioTranscriptionRead:
    require_assistant_use(db, current_user)
    audio = await file.read(settings.speech_transcription_max_bytes + 1)
    if len(audio) > settings.speech_transcription_max_bytes:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Audio file is too large",
        )
    try:
        text = transcribe_audio_bytes(
            audio,
            language_code=settings.speech_transcription_language_code,
        )
    except SpeechTranscriptionError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audio transcription is not available",
        ) from None
    return AssistantAudioTranscriptionRead(text=text)


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


@router.get(
    "/admin-feedback",
    response_model=list[AssistantAdminFeedbackRead],
)
def list_admin_feedback(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
    status: Annotated[AdminFeedbackStatus | None, Query()] = None,
) -> list[AssistantAdminFeedback]:
    query = (
        select(AssistantAdminFeedback)
        .options(
            selectinload(AssistantAdminFeedback.submitted_by),
            selectinload(AssistantAdminFeedback.reviewed_by),
        )
        .order_by(
            AssistantAdminFeedback.updated_at.desc(),
            AssistantAdminFeedback.id.desc(),
        )
    )
    if status is not None:
        query = query.where(AssistantAdminFeedback.status == status)
    return list(db.scalars(query))


@router.patch(
    "/admin-feedback/{feedback_id}",
    response_model=AssistantAdminFeedbackRead,
)
def update_admin_feedback(
    feedback_id: int,
    payload: AssistantAdminFeedbackUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> AssistantAdminFeedback:
    feedback = get_existing_admin_feedback(db, feedback_id)
    updates = payload.model_dump(exclude_unset=True)
    if "priority" in updates and updates["priority"] is not None:
        feedback.priority = updates["priority"]
    if "review_notes" in updates:
        feedback.review_notes = updates["review_notes"]
    if "status" in updates and updates["status"] is not None:
        feedback.status = updates["status"]
        feedback.reviewed_by_id = current_user.id
        feedback.reviewed_at = datetime.now(timezone.utc)

    db.commit()
    return get_existing_admin_feedback(db, feedback_id)


@router.get(
    "/transversal-features",
    response_model=list[AssistantTransversalFeatureRead],
)
def list_transversal_features(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
    status: Annotated[TransversalFeatureStatus | None, Query()] = None,
) -> list[AssistantTransversalFeature]:
    query = (
        select(AssistantTransversalFeature)
        .options(
            selectinload(AssistantTransversalFeature.proposed_by),
            selectinload(AssistantTransversalFeature.reviewed_by),
        )
        .order_by(
            AssistantTransversalFeature.updated_at.desc(),
            AssistantTransversalFeature.id.desc(),
        )
    )
    if status is not None:
        query = query.where(AssistantTransversalFeature.status == status)

    return list(db.scalars(query))


@router.patch(
    "/transversal-features/{feature_id}",
    response_model=AssistantTransversalFeatureRead,
)
def update_transversal_feature(
    feature_id: int,
    payload: AssistantTransversalFeatureUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> AssistantTransversalFeature:
    feature = get_existing_transversal_feature(db, feature_id)
    updates = payload.model_dump(exclude_unset=True)

    for field in (
        "title",
        "summary",
        "rationale",
        "category",
        "sensitivity",
        "auto_activatable",
        "review_notes",
    ):
        if field in updates:
            setattr(feature, field, updates[field])

    if "status" in updates and updates["status"] is not None:
        feature.status = updates["status"]
        feature.reviewed_by_id = current_user.id
        feature.reviewed_at = datetime.now(timezone.utc)

    db.commit()
    return get_existing_transversal_feature(db, feature_id)


@router.get(
    "/transversal-feature-adoptions",
    response_model=list[AssistantTransversalFeatureAdoptionRead],
)
def list_transversal_feature_adoptions(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
    status: Annotated[TransversalFeatureAdoptionStatus | None, Query()] = None,
    organization_id: int | None = None,
    feature_id: int | None = None,
) -> list[AssistantTransversalFeatureAdoption]:
    query = (
        select(AssistantTransversalFeatureAdoption)
        .options(
            selectinload(AssistantTransversalFeatureAdoption.feature).selectinload(
                AssistantTransversalFeature.proposed_by
            ),
            selectinload(AssistantTransversalFeatureAdoption.feature).selectinload(
                AssistantTransversalFeature.reviewed_by
            ),
            selectinload(AssistantTransversalFeatureAdoption.requested_by),
            selectinload(AssistantTransversalFeatureAdoption.approved_by),
        )
        .order_by(
            AssistantTransversalFeatureAdoption.updated_at.desc(),
            AssistantTransversalFeatureAdoption.id.desc(),
        )
    )
    if status is not None:
        query = query.where(AssistantTransversalFeatureAdoption.status == status)
    if organization_id is not None:
        query = query.where(
            AssistantTransversalFeatureAdoption.organization_id == organization_id
        )
    if feature_id is not None:
        query = query.where(AssistantTransversalFeatureAdoption.feature_id == feature_id)

    return list(db.scalars(query))


@router.patch(
    "/transversal-feature-adoptions/{adoption_id}",
    response_model=AssistantTransversalFeatureAdoptionRead,
)
def update_transversal_feature_adoption(
    adoption_id: int,
    payload: AssistantTransversalFeatureAdoptionUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
) -> AssistantTransversalFeatureAdoption:
    adoption = get_existing_transversal_feature_adoption(db, adoption_id)
    updates = payload.model_dump(exclude_unset=True)

    if "notes" in updates:
        adoption.notes = updates["notes"]
    if "status" in updates and updates["status"] is not None:
        adoption.status = updates["status"]
        if adoption.status == "active":
            adoption.approved_by_id = current_user.id
            adoption.activated_at = datetime.now(timezone.utc)

    db.commit()
    return get_existing_transversal_feature_adoption(db, adoption_id)


@router.get(
    "/conversation-folders",
    response_model=list[AssistantConversationFolderRead],
)
def list_conversation_folders(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[AssistantConversationFolder]:
    require_assistant_use(db, current_user)
    return list(
        db.scalars(
            select(AssistantConversationFolder)
            .where(AssistantConversationFolder.created_by_id == current_user.id)
            .order_by(
                AssistantConversationFolder.sort_order,
                AssistantConversationFolder.name,
                AssistantConversationFolder.id,
            )
        )
    )


@router.post(
    "/conversation-folders",
    response_model=AssistantConversationFolderRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_conversation_folder(
    payload: AssistantConversationFolderCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversationFolder:
    require_assistant_use(db, current_user)
    ensure_conversation_folder_name_available(db, current_user, payload.name)
    folder = AssistantConversationFolder(
        name=payload.name,
        sort_order=payload.sort_order,
        created_by_id=current_user.id,
    )
    db.add(folder)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise_conversation_folder_name_conflict()
    return get_own_conversation_folder(db, current_user, folder.id)


@router.patch(
    "/conversation-folders/{folder_id}",
    response_model=AssistantConversationFolderRead,
)
def update_conversation_folder(
    folder_id: int,
    payload: AssistantConversationFolderUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversationFolder:
    require_assistant_use(db, current_user)
    folder = get_own_conversation_folder(db, current_user, folder_id)
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] is not None:
        ensure_conversation_folder_name_available(
            db,
            current_user,
            updates["name"],
            exclude_folder_id=folder_id,
        )
        folder.name = updates["name"]
    if "sort_order" in updates and updates["sort_order"] is not None:
        folder.sort_order = updates["sort_order"]
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise_conversation_folder_name_conflict()
    return get_own_conversation_folder(db, current_user, folder_id)


@router.delete(
    "/conversation-folders/{folder_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
def delete_conversation_folder(
    folder_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    require_assistant_use(db, current_user)
    folder = get_own_conversation_folder(db, current_user, folder_id)
    db.query(AssistantConversation).filter(
        AssistantConversation.created_by_id == current_user.id,
        AssistantConversation.folder_id == folder.id,
    ).update({AssistantConversation.folder_id: None}, synchronize_session=False)
    db.delete(folder)
    db.commit()


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
    if "title" in updates and updates["title"] is not None:
        conversation.title = updates["title"]
    if "status" in updates and updates["status"] is not None:
        conversation.status = updates["status"]
    if "folder_id" in updates:
        folder_id = updates["folder_id"]
        if folder_id is None:
            conversation.folder_id = None
        else:
            folder = get_own_conversation_folder(db, current_user, folder_id)
            conversation.folder_id = folder.id

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


@router.post("/conversations/{conversation_id}/messages/stream")
def send_message_stream(
    conversation_id: int,
    payload: AssistantUserMessageCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    agent_gateway: Annotated[AIGateway, Depends(get_gateway)],
) -> StreamingResponse:
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

    def event_stream():
        try:
            for event in run_agent_turn_events(
                db,
                current_user,
                conversation,
                payload.content,
                agent_gateway,
            ):
                yield format_sse_event(event)
        except AssistantUnavailableError:
            yield format_sse_event(
                TurnEvent("error", {"detail": "Assistant request failed"})
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def format_sse_event(event: TurnEvent) -> str:
    data = json.dumps(event.data, ensure_ascii=False)
    return f"event: {event.type}\ndata: {data}\n\n"


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


def get_own_conversation_folder(
    db: Session,
    current_user: User,
    folder_id: int,
) -> AssistantConversationFolder:
    folder = db.scalar(
        select(AssistantConversationFolder)
        .where(AssistantConversationFolder.id == folder_id)
        .execution_options(populate_existing=True)
    )
    if folder is None or folder.created_by_id != current_user.id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Conversation folder not found",
        )
    return folder


def raise_conversation_folder_name_conflict() -> None:
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail="Assistant conversation folder already exists",
    )


def ensure_conversation_folder_name_available(
    db: Session,
    current_user: User,
    name: str,
    exclude_folder_id: int | None = None,
) -> None:
    query = select(AssistantConversationFolder).where(
        AssistantConversationFolder.created_by_id == current_user.id,
        AssistantConversationFolder.name == name,
    )
    if exclude_folder_id is not None:
        query = query.where(AssistantConversationFolder.id != exclude_folder_id)
    if db.scalar(query) is not None:
        raise_conversation_folder_name_conflict()


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


def get_existing_admin_feedback(
    db: Session,
    feedback_id: int,
) -> AssistantAdminFeedback:
    feedback = db.scalar(
        select(AssistantAdminFeedback)
        .options(
            selectinload(AssistantAdminFeedback.submitted_by),
            selectinload(AssistantAdminFeedback.reviewed_by),
        )
        .where(AssistantAdminFeedback.id == feedback_id)
        .execution_options(populate_existing=True)
    )
    if feedback is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Assistant admin feedback not found",
        )
    return feedback


def get_existing_transversal_feature(
    db: Session,
    feature_id: int,
) -> AssistantTransversalFeature:
    feature = db.scalar(
        select(AssistantTransversalFeature)
        .options(
            selectinload(AssistantTransversalFeature.proposed_by),
            selectinload(AssistantTransversalFeature.reviewed_by),
        )
        .where(AssistantTransversalFeature.id == feature_id)
        .execution_options(populate_existing=True)
    )
    if feature is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Transversal feature not found",
        )
    return feature


def get_existing_transversal_feature_adoption(
    db: Session,
    adoption_id: int,
) -> AssistantTransversalFeatureAdoption:
    adoption = db.scalar(
        select(AssistantTransversalFeatureAdoption)
        .options(
            selectinload(AssistantTransversalFeatureAdoption.feature).selectinload(
                AssistantTransversalFeature.proposed_by
            ),
            selectinload(AssistantTransversalFeatureAdoption.feature).selectinload(
                AssistantTransversalFeature.reviewed_by
            ),
            selectinload(AssistantTransversalFeatureAdoption.requested_by),
            selectinload(AssistantTransversalFeatureAdoption.approved_by),
        )
        .where(AssistantTransversalFeatureAdoption.id == adoption_id)
        .execution_options(populate_existing=True)
    )
    if adoption is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Transversal feature adoption not found",
        )
    return adoption
