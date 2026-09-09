import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status as http_status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.assistant.attachments import (
    attachment_payload,
    ensure_attachment_runtime_supported,
    prepare_attachments,
)
from app.assistant.gateway import AIGateway, AssistantUnavailableError, gateway
from app.assistant.models import (
    AssistantAdminFeedback,
    AssistantConversation,
    AssistantConversationFolder,
    AssistantMessage,
    AssistantMessageAttachment,
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
    AssistantRealtimeSessionRead,
    AssistantRealtimeToolCallCreate,
    AssistantRealtimeToolCallRead,
    AssistantRealtimeTurnCreate,
    AssistantRealtimeTurnRead,
    AssistantRealtimeTurnStartCreate,
    AssistantRealtimeTurnStartRead,
    AssistantSpeechCreate,
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
from app.assistant.realtime import (
    AssistantRealtimeConflictError,
    AssistantRealtimeUnavailableError,
    create_realtime_client_secret,
    execute_realtime_tool_call,
    persist_realtime_turn,
    realtime_voice_enabled,
    start_realtime_turn,
)
from app.assistant.turn import TurnEvent, run_agent_turn, run_agent_turn_events
from app.assistant.voice import run_voice_turn_events
from app.assistant.speech import (
    SpeechSynthesisError,
    SpeechTranscriptionError,
    synthesize_speech_bytes,
    transcribe_audio_bytes,
)
from app.assistant.tools import get_available_tool_specs
from app.auth.dependencies import get_current_user, require_superuser
from app.core.config import settings
from app.core.rate_limit import (
    assistant_turn_rate_limiter,
    require_rate_limit_slot,
    speech_rate_limiter,
)
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.documents.models import Document
from app.organizations.access import get_accessible_organizations_query
from app.projects.access import user_can_access_project
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger(__name__)

MEMORY_STATUS_TRANSITIONS = {
    "proposed": frozenset({"approved", "rejected", "archived", "blocked"}),
    "approved": frozenset({"proposed", "rejected", "archived", "blocked"}),
    "rejected": frozenset({"proposed", "archived"}),
    "archived": frozenset({"proposed"}),
    "blocked": frozenset({"proposed", "rejected", "archived"}),
}
ADMIN_FEEDBACK_STATUS_TRANSITIONS = {
    "submitted": frozenset({"reviewed", "dismissed", "archived"}),
    "reviewed": frozenset({"submitted", "dismissed", "archived"}),
    "dismissed": frozenset({"submitted", "reviewed", "archived"}),
    "archived": frozenset({"submitted"}),
}
MEMORY_MATERIAL_FIELDS = frozenset({"category", "content", "sensitivity"})


def get_gateway() -> AIGateway:
    return gateway


def require_assistant_use(db: Session, current_user: User) -> None:
    if has_permission(current_user, "assistant.use", db):
        return

    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="Permission required: assistant.use",
    )


def configured_assistant_model() -> str:
    if settings.assistant_runtime == "hermes_agent":
        return settings.hermes_agent_model
    if settings.assistant_runtime == "openai_responses":
        return settings.openai_responses_model
    if settings.assistant_runtime == "groq_responses":
        return settings.groq_responses_model
    if settings.assistant_runtime == "codex_subscription":
        return settings.codex_subscription_model or "codex-subscription-default"
    return settings.assistant_model


@router.get("/status", response_model=AssistantStatusRead)
def get_assistant_status(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    agent_gateway: Annotated[AIGateway, Depends(get_gateway)],
) -> AssistantStatusRead:
    require_assistant_use(db, current_user)
    available_tools = get_available_tool_specs(db, current_user)
    return AssistantStatusRead(
        enabled=agent_gateway.enabled,
        runtime=settings.assistant_runtime,
        model=getattr(agent_gateway, "model", configured_assistant_model()),
        runtime_healthy=getattr(agent_gateway, "runtime_healthy", None),
        speech_transcription_enabled=settings.speech_transcription_runtime
        != "disabled",
        speech_synthesis_enabled=settings.speech_synthesis_runtime != "disabled",
        speech_synthesis_max_chars=settings.speech_synthesis_max_chars,
        realtime_voice_enabled=realtime_voice_enabled(),
        realtime_voice_provider="openai" if realtime_voice_enabled() else None,
        realtime_voice_model=(
            settings.assistant_realtime_model if realtime_voice_enabled() else None
        ),
        web_page_reader_enabled=any(
            tool.name == "read_web_page" for tool in available_tools
        ),
        # Full page bodies are deliberately excluded from Realtime state. Web
        # search snippets remain available in voice sessions.
        realtime_web_page_reader_enabled=False,
        tools=[tool.metadata for tool in available_tools],
    )


@router.post("/audio-transcriptions", response_model=AssistantAudioTranscriptionRead)
async def transcribe_audio(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
) -> AssistantAudioTranscriptionRead:
    require_assistant_use(db, current_user)
    require_rate_limit_slot(
        speech_rate_limiter,
        str(current_user.id),
        detail="Too many speech requests",
    )
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


@router.post("/speech")
def synthesize_speech(
    payload: AssistantSpeechCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    require_assistant_use(db, current_user)
    require_rate_limit_slot(
        speech_rate_limiter,
        str(current_user.id),
        detail="Too many speech requests",
    )
    if len(payload.text) > settings.speech_synthesis_max_chars:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Speech text is too long",
        )
    try:
        audio = synthesize_speech_bytes(payload.text)
    except SpeechSynthesisError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Speech synthesis is not available",
        ) from None
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/memory", response_model=list[AssistantMemoryEntryRead])
def list_memory_entries(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status: MemoryStatus | None = "proposed",
    organization_id: int | None = None,
    reviewable_only: bool = False,
) -> list[AssistantMemoryEntryRead]:
    require_assistant_use(db, current_user)
    reviewable_organization_ids = get_memory_permission_organization_ids(
        db, current_user, "assistant.memory.review"
    )
    organization_ids = reviewable_organization_ids
    if status == "approved" and not reviewable_only:
        organization_ids = sorted(
            set(reviewable_organization_ids)
            | set(
                get_memory_permission_organization_ids(
                    db, current_user, "assistant.memory.view"
                )
            )
        )
    if organization_id is not None:
        organization_ids = [
            permitted_id
            for permitted_id in organization_ids
            if permitted_id == organization_id
        ]
    query = (
        select(AssistantMemoryEntry)
        .options(
            selectinload(AssistantMemoryEntry.organization),
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

    entries = list(db.scalars(paginate(db, query, page, response)))
    reviewable_organization_id_set = set(reviewable_organization_ids)
    return [
        serialize_memory_entry_for_access(
            entry,
            include_review_metadata=(
                entry.organization_id in reviewable_organization_id_set
            ),
        )
        for entry in entries
    ]


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
    entry = get_reviewable_memory_entry(
        db,
        current_user,
        entry_id,
        for_update=True,
    )
    ensure_expected_updated_at(
        entry.updated_at,
        payload.expected_updated_at,
        detail="Assistant memory entry was modified by another reviewer",
    )

    updates = payload.model_dump(exclude_unset=True)
    updates.pop("expected_updated_at")
    sensitive_approval_confirmed = updates.pop(
        "sensitive_approval_confirmed",
        False,
    )
    requested_status = updates.get("status")
    material_change = any(
        field in updates
        and updates[field] is not None
        and updates[field] != getattr(entry, field)
        for field in MEMORY_MATERIAL_FIELDS
    )
    if entry.status == "approved" and material_change and requested_status is None:
        requested_status = "proposed"

    if requested_status is not None:
        ensure_status_transition(
            entry.status,
            requested_status,
            MEMORY_STATUS_TRANSITIONS,
            detail="Invalid assistant memory status transition",
        )
    effective_sensitivity = updates.get("sensitivity") or entry.sensitivity
    if (
        requested_status == "approved"
        and effective_sensitivity != "normal"
        and not sensitive_approval_confirmed
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Sensitive assistant memory approval requires explicit "
                "confirmation"
            ),
        )

    if "content" in updates and updates["content"] is not None:
        entry.content = updates["content"]
    if "category" in updates and updates["category"] is not None:
        entry.category = updates["category"]
    if "sensitivity" in updates and updates["sensitivity"] is not None:
        entry.sensitivity = updates["sensitivity"]
    if "review_notes" in updates:
        entry.review_notes = updates["review_notes"]
    if requested_status is not None:
        entry.status = requested_status
        if requested_status == "proposed":
            entry.reviewed_by_id = None
            entry.reviewed_at = None
        else:
            entry.reviewed_by_id = current_user.id
            entry.reviewed_at = datetime.now(timezone.utc)

    db.commit()
    return get_reviewable_memory_entry(db, current_user, entry_id)


@router.get(
    "/admin-feedback",
    response_model=list[AssistantAdminFeedbackRead],
)
def list_admin_feedback(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(require_superuser)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    status: Annotated[AdminFeedbackStatus | None, Query()] = None,
) -> list[AssistantAdminFeedback]:
    query = (
        select(AssistantAdminFeedback)
        .options(
            selectinload(AssistantAdminFeedback.organization),
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
    return list(db.scalars(paginate(db, query, page, response)))


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
    feedback = get_existing_admin_feedback(db, feedback_id, for_update=True)
    ensure_expected_updated_at(
        feedback.updated_at,
        payload.expected_updated_at,
        detail="Assistant admin feedback was modified by another reviewer",
    )
    updates = payload.model_dump(exclude_unset=True)
    updates.pop("expected_updated_at")
    requested_status = updates.get("status")
    if requested_status is not None:
        ensure_status_transition(
            feedback.status,
            requested_status,
            ADMIN_FEEDBACK_STATUS_TRANSITIONS,
            detail="Invalid assistant admin feedback status transition",
        )
    if "priority" in updates and updates["priority"] is not None:
        feedback.priority = updates["priority"]
    if "review_notes" in updates:
        feedback.review_notes = updates["review_notes"]
    if requested_status is not None:
        feedback.status = requested_status
        if requested_status == "submitted":
            feedback.reviewed_by_id = None
            feedback.reviewed_at = None
        else:
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
) -> AssistantConversationDetail:
    require_assistant_use(db, current_user)

    conversation = AssistantConversation(
        title=payload.title or "Conversación",
        created_by_id=current_user.id,
    )
    db.add(conversation)
    db.commit()
    return serialize_conversation_detail(
        db,
        current_user,
        get_own_conversation(db, current_user, conversation.id),
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=AssistantConversationDetail,
)
def get_conversation(
    conversation_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversationDetail:
    require_assistant_use(db, current_user)
    return serialize_conversation_detail(
        db,
        current_user,
        get_own_conversation(db, current_user, conversation_id),
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=AssistantConversationDetail,
)
def update_conversation(
    conversation_id: int,
    payload: AssistantConversationUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantConversationDetail:
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
    return serialize_conversation_detail(
        db,
        current_user,
        get_own_conversation(db, current_user, conversation_id),
    )


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
) -> AssistantConversationDetail:
    require_assistant_use(db, current_user)
    require_rate_limit_slot(
        assistant_turn_rate_limiter,
        str(current_user.id),
        detail="Too many assistant requests",
    )
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

    ensure_attachment_runtime_supported(bool(payload.attachment_ids))
    prepared_attachments = prepare_attachments(
        db,
        current_user,
        payload.attachment_ids,
    )

    try:
        run_agent_turn(
            db,
            current_user,
            conversation,
            payload.content,
            agent_gateway,
            input_mode=payload.input_mode,
            prepared_attachments=prepared_attachments,
        )
    except AssistantRealtimeConflictError as error:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    except AssistantUnavailableError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assistant request failed",
        ) from None

    return serialize_conversation_detail(
        db,
        current_user,
        get_own_conversation(db, current_user, conversation_id),
    )


@router.post("/conversations/{conversation_id}/messages/stream")
def send_message_stream(
    conversation_id: int,
    payload: AssistantUserMessageCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    agent_gateway: Annotated[AIGateway, Depends(get_gateway)],
) -> StreamingResponse:
    require_assistant_use(db, current_user)
    require_rate_limit_slot(
        assistant_turn_rate_limiter,
        str(current_user.id),
        detail="Too many assistant requests",
    )
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

    ensure_attachment_runtime_supported(bool(payload.attachment_ids))
    prepared_attachments = prepare_attachments(
        db,
        current_user,
        payload.attachment_ids,
    )

    def event_stream():
        try:
            for event in run_agent_turn_events(
                db,
                current_user,
                conversation,
                payload.content,
                agent_gateway,
                input_mode=payload.input_mode,
                prepared_attachments=prepared_attachments,
            ):
                yield format_sse_event(event)
        except AssistantUnavailableError:
            yield format_sse_event(
                TurnEvent("error", {"detail": "Assistant request failed"})
            )
        except AssistantRealtimeConflictError as error:
            yield format_sse_event(TurnEvent("error", {"detail": str(error)}))
        except Exception as error:
            yield format_sse_event(
                recover_unexpected_stream_error(
                    db,
                    conversation_id=conversation_id,
                    stream_kind="text",
                    error=error,
                )
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{conversation_id}/voice-turns/stream")
async def send_voice_turn_stream(
    conversation_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    agent_gateway: Annotated[AIGateway, Depends(get_gateway)],
    file: Annotated[UploadFile, File()],
) -> StreamingResponse:
    require_assistant_use(db, current_user)
    require_rate_limit_slot(
        assistant_turn_rate_limiter,
        str(current_user.id),
        detail="Too many assistant requests",
    )
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

    audio = await file.read(settings.speech_transcription_max_bytes + 1)
    if len(audio) > settings.speech_transcription_max_bytes:
        raise HTTPException(
            status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Audio file is too large",
        )

    def event_stream():
        try:
            for event in run_voice_turn_events(
                db,
                current_user,
                conversation,
                audio,
                agent_gateway,
            ):
                yield format_sse_event(event)
        except AssistantUnavailableError:
            yield format_sse_event(
                TurnEvent("error", {"detail": "Assistant request failed"})
            )
        except AssistantRealtimeConflictError as error:
            yield format_sse_event(TurnEvent("error", {"detail": str(error)}))
        except Exception as error:
            yield format_sse_event(
                recover_unexpected_stream_error(
                    db,
                    conversation_id=conversation_id,
                    stream_kind="voice",
                    error=error,
                )
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/conversations/{conversation_id}/realtime/session",
    response_model=AssistantRealtimeSessionRead,
)
def create_realtime_voice_session(
    conversation_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantRealtimeSessionRead:
    require_assistant_use(db, current_user)
    conversation = get_own_conversation(db, current_user, conversation_id)

    if conversation.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conversation is archived",
        )

    try:
        client_secret = create_realtime_client_secret(db, current_user, conversation)
    except AssistantRealtimeUnavailableError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Realtime voice is not configured",
        ) from None
    except AssistantRealtimeConflictError as error:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    return AssistantRealtimeSessionRead(
        client_secret=client_secret["value"],
        client_secret_expires_at=client_secret.get("expires_at"),
        model=settings.assistant_realtime_model,
        voice=settings.assistant_realtime_voice,
        realtime_url=settings.assistant_realtime_url,
    )


@router.post(
    "/conversations/{conversation_id}/realtime/turns/start",
    response_model=AssistantRealtimeTurnStartRead,
)
def start_realtime_voice_turn(
    conversation_id: int,
    payload: AssistantRealtimeTurnStartCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_assistant_use(db, current_user)
    conversation = get_own_conversation(db, current_user, conversation_id)

    if conversation.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conversation is archived",
        )

    try:
        user_message, replayed = start_realtime_turn(
            db,
            current_user,
            conversation,
            payload,
        )
    except AssistantRealtimeConflictError as error:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    return {
        "turn_id": payload.turn_id,
        "user_message": user_message,
        "replayed": replayed,
    }


@router.post(
    "/conversations/{conversation_id}/realtime/turns/{turn_id}/tool-calls",
    response_model=AssistantRealtimeToolCallRead,
)
def execute_realtime_voice_tool_call(
    conversation_id: int,
    turn_id: str,
    payload: AssistantRealtimeToolCallCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_assistant_use(db, current_user)
    conversation = get_own_conversation(db, current_user, conversation_id)

    if conversation.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conversation is archived",
        )

    try:
        action, output, user_message, confirmation_prompt, replayed = (
            execute_realtime_tool_call(
                db,
                current_user,
                conversation,
                turn_id,
                payload,
            )
        )
    except AssistantRealtimeConflictError as error:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    return {
        "call_id": action["call_id"],
        "ok": action["ok"],
        "output": output,
        "action": action,
        "user_message": user_message,
        "confirmation_prompt": confirmation_prompt,
        "replayed": replayed,
    }


@router.post(
    "/conversations/{conversation_id}/realtime/turns/{turn_id}/complete",
    response_model=AssistantRealtimeTurnRead,
)
def persist_realtime_voice_turn(
    conversation_id: int,
    turn_id: str,
    payload: AssistantRealtimeTurnCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    require_assistant_use(db, current_user)
    conversation = get_own_conversation(db, current_user, conversation_id)

    if conversation.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conversation is archived",
        )

    try:
        (
            user_message,
            assistant_message,
            confirmation_prompt,
            confirmation_delivery_required,
            replayed,
        ) = persist_realtime_turn(
            db,
            current_user,
            conversation,
            turn_id,
            payload,
        )
    except AssistantRealtimeConflictError as error:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from None
    db.refresh(conversation)
    return {
        "conversation": conversation,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "confirmation_prompt": confirmation_prompt,
        "confirmation_delivery_required": confirmation_delivery_required,
        "replayed": replayed,
    }


def format_sse_event(event: TurnEvent) -> str:
    data = json.dumps(event.data, ensure_ascii=False)
    return f"event: {event.type}\ndata: {data}\n\n"


def recover_unexpected_stream_error(
    db: Session,
    *,
    conversation_id: int,
    stream_kind: str,
    error: Exception,
) -> TurnEvent:
    """Roll back failed stream work and return a safe terminal event."""
    logger.error(
        "Unexpected assistant stream failure "
        "(conversation=%s stream=%s error_type=%s)",
        conversation_id,
        stream_kind,
        type(error).__name__,
    )
    try:
        db.rollback()
    except Exception as rollback_error:
        logger.error(
            "Assistant stream rollback failed "
            "(conversation=%s stream=%s error_type=%s)",
            conversation_id,
            stream_kind,
            type(rollback_error).__name__,
        )
    return TurnEvent("error", {"detail": "Assistant request failed"})


def serialize_conversation_detail(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
) -> AssistantConversationDetail:
    """Serialize current attachment visibility without changing history rows."""

    project_visibility: dict[tuple[int, int], bool] = {}
    messages: list[dict] = []
    for message in conversation.messages:
        visible_attachments: list[dict] = []
        for attachment in message.attachments:
            document = attachment.document
            project = document.project
            if project.organization_id != document.organization_id:
                continue
            cache_key = (project.id, project.organization_id)
            if cache_key not in project_visibility:
                visible = True
                if not current_user.is_superuser:
                    can_manage = has_permission(
                        current_user,
                        "documents.manage",
                        db,
                        organization_id=project.organization_id,
                    )
                    can_view = can_manage or has_permission(
                        current_user,
                        "documents.view",
                        db,
                        organization_id=project.organization_id,
                    )
                    visible = can_view and (
                        can_manage
                        or user_can_access_project(db, current_user, project)
                    )
                project_visibility[cache_key] = visible
            if project_visibility[cache_key]:
                visible_attachments.append(attachment_payload(attachment))

        messages.append(
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "actions": message.actions,
                "attachments": visible_attachments,
                "agent_key": message.agent_key,
                "routing": message.routing,
                "created_at": message.created_at,
            }
        )

    return AssistantConversationDetail.model_validate(
        {
            "id": conversation.id,
            "title": conversation.title,
            "status": conversation.status,
            "folder_id": conversation.folder_id,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
            "messages": messages,
        }
    )


def get_own_conversation(
    db: Session,
    current_user: User,
    conversation_id: int,
) -> AssistantConversation:
    conversation = db.scalar(
        select(AssistantConversation)
        .options(
            selectinload(AssistantConversation.messages)
            .selectinload(AssistantMessage.attachments)
            .selectinload(AssistantMessageAttachment.document)
            .selectinload(Document.project)
        )
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


def get_reviewable_memory_entry(
    db: Session,
    current_user: User,
    entry_id: int,
    *,
    for_update: bool = False,
) -> AssistantMemoryEntry:
    organization_ids = get_memory_permission_organization_ids(
        db,
        current_user,
        "assistant.memory.review",
    )
    query = (
        select(AssistantMemoryEntry)
        .options(
            selectinload(AssistantMemoryEntry.organization),
            selectinload(AssistantMemoryEntry.proposed_by),
            selectinload(AssistantMemoryEntry.reviewed_by),
        )
        .where(
            AssistantMemoryEntry.id == entry_id,
            AssistantMemoryEntry.organization_id.in_(organization_ids),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    entry = db.scalar(query)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Assistant memory entry not found",
        )
    return entry


def serialize_memory_entry_for_access(
    entry: AssistantMemoryEntry,
    *,
    include_review_metadata: bool,
) -> AssistantMemoryEntryRead:
    serialized = AssistantMemoryEntryRead.model_validate(entry)
    if include_review_metadata:
        return serialized
    return serialized.model_copy(
        update={
            "source_conversation_id": None,
            "source_message_id": None,
            "proposed_by_id": None,
            "reviewed_by_id": None,
            "review_notes": None,
            "proposed_by": None,
            "reviewed_by": None,
        }
    )


def get_existing_admin_feedback(
    db: Session,
    feedback_id: int,
    *,
    for_update: bool = False,
) -> AssistantAdminFeedback:
    query = (
        select(AssistantAdminFeedback)
        .options(
            selectinload(AssistantAdminFeedback.organization),
            selectinload(AssistantAdminFeedback.submitted_by),
            selectinload(AssistantAdminFeedback.reviewed_by),
        )
        .where(AssistantAdminFeedback.id == feedback_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    feedback = db.scalar(query)
    if feedback is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Assistant admin feedback not found",
        )
    return feedback


def ensure_expected_updated_at(
    actual: datetime,
    expected: datetime,
    *,
    detail: str,
) -> None:
    if actual != expected:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=detail,
        )


def ensure_status_transition(
    current_status: str,
    requested_status: str,
    transitions: dict[str, frozenset[str]],
    *,
    detail: str,
) -> None:
    if requested_status == current_status:
        return
    if requested_status not in transitions[current_status]:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=detail,
        )


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
