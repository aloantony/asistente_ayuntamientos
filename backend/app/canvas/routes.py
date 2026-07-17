from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.canvas.schemas import (
    AssistantCanvasDocumentCreate,
    AssistantCanvasDocumentRead,
    AssistantCanvasDocumentSummary,
    AssistantCanvasDocumentUpdate,
    AssistantCanvasRevisionRead,
    AssistantCanvasRevisionRestore,
    AssistantCanvasSelectionRead,
    AssistantCanvasSelectionUpdate,
    AssistantCanvasWorkspaceRead,
)
from app.canvas.service import (
    active_canvas_document_id,
    create_canvas_document,
    get_owned_canvas_conversation,
    get_owned_canvas_document,
    list_canvas_documents,
    list_canvas_revisions,
    restore_canvas_revision,
    serialize_revision,
    set_active_canvas_document,
    update_canvas_document,
)
from app.db.session import get_db
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(prefix="/assistant", tags=["assistant-canvas"])


def require_canvas_access(db: Session, current_user: User) -> None:
    if has_permission(current_user, "assistant.use", db):
        return
    raise HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail="Permission required: assistant.use",
    )


@router.get(
    "/conversations/{conversation_id}/canvas",
    response_model=AssistantCanvasWorkspaceRead,
)
def get_canvas_workspace(
    conversation_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: Annotated[bool, Query()] = False,
) -> AssistantCanvasWorkspaceRead:
    require_canvas_access(db, current_user)
    conversation = get_owned_canvas_conversation(db, current_user, conversation_id)
    documents = list_canvas_documents(
        db,
        current_user,
        conversation_id,
        include_archived=include_archived,
    )
    active_id = active_canvas_document_id(conversation)
    if active_id not in {document.id for document in documents if document.status == "draft"}:
        active_id = None
    return AssistantCanvasWorkspaceRead(
        documents=[
            AssistantCanvasDocumentSummary.model_validate(document)
            for document in documents
        ],
        active_document_id=active_id,
    )


@router.post(
    "/conversations/{conversation_id}/canvas/documents",
    response_model=AssistantCanvasDocumentRead,
    status_code=http_status.HTTP_201_CREATED,
)
def create_canvas_document_route(
    conversation_id: int,
    payload: AssistantCanvasDocumentCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantCanvasDocumentRead:
    require_canvas_access(db, current_user)
    document = create_canvas_document(
        db,
        current_user,
        conversation_id=conversation_id,
        title=payload.title,
        document_type=payload.document_type,
        organization_id=payload.organization_id,
        content=payload.content,
        creation_id=payload.creation_id,
        edit_source="user",
    )
    return AssistantCanvasDocumentRead.model_validate(document)


@router.put(
    "/conversations/{conversation_id}/canvas/active",
    response_model=AssistantCanvasSelectionRead,
)
def update_active_canvas_document(
    conversation_id: int,
    payload: AssistantCanvasSelectionUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantCanvasSelectionRead:
    require_canvas_access(db, current_user)
    active_id = set_active_canvas_document(
        db,
        current_user,
        conversation_id,
        payload.document_id,
    )
    return AssistantCanvasSelectionRead(active_document_id=active_id)


@router.get(
    "/canvas/documents/{document_id}",
    response_model=AssistantCanvasDocumentRead,
)
def get_canvas_document_route(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantCanvasDocumentRead:
    require_canvas_access(db, current_user)
    document = get_owned_canvas_document(db, current_user, document_id)
    return AssistantCanvasDocumentRead.model_validate(document)


@router.patch(
    "/canvas/documents/{document_id}",
    response_model=AssistantCanvasDocumentRead,
)
def update_canvas_document_route(
    document_id: int,
    payload: AssistantCanvasDocumentUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantCanvasDocumentRead:
    require_canvas_access(db, current_user)
    updates = payload.model_dump(exclude_unset=True)
    expected_revision = updates.pop("expected_revision")
    document = update_canvas_document(
        db,
        current_user,
        document_id,
        expected_revision=expected_revision,
        edit_source="user",
        **updates,
    )
    return AssistantCanvasDocumentRead.model_validate(document)


@router.get(
    "/canvas/documents/{document_id}/revisions",
    response_model=list[AssistantCanvasRevisionRead],
)
def get_canvas_document_revisions(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[dict]:
    require_canvas_access(db, current_user)
    return [
        serialize_revision(revision)
        for revision in list_canvas_revisions(db, current_user, document_id)
    ]


@router.post(
    "/canvas/documents/{document_id}/revisions/{revision_number}/restore",
    response_model=AssistantCanvasDocumentRead,
)
def restore_canvas_document_revision(
    document_id: int,
    revision_number: int,
    payload: AssistantCanvasRevisionRestore,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AssistantCanvasDocumentRead:
    require_canvas_access(db, current_user)
    document = restore_canvas_revision(
        db,
        current_user,
        document_id,
        revision_number,
        expected_revision=payload.expected_revision,
        change_summary=payload.change_summary,
        mutation_id=payload.mutation_id,
    )
    return AssistantCanvasDocumentRead.model_validate(document)
