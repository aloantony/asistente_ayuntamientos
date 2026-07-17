import hashlib
import json
import logging

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assistant.models import AssistantConversation
from app.canvas.models import (
    CANVAS_DOCUMENT_STATUSES,
    CANVAS_DOCUMENT_TYPES,
    CANVAS_REVISION_SOURCES,
    AssistantCanvasDocument,
    AssistantCanvasRevision,
)
from app.canvas.schemas import MAX_CANVAS_CONTENT_CHARS
from app.organizations.access import get_accessible_organizations_query
from app.organizations.models import Organization
from app.users.models import User

logger = logging.getLogger(__name__)

ACTIVE_CANVAS_STATE_KEY = "active_canvas_document_id"
MAX_CANVAS_REVISIONS_RETURNED = 50


def get_owned_canvas_conversation(
    db: Session,
    current_user: User,
    conversation_id: int,
    *,
    for_update: bool = False,
) -> AssistantConversation:
    query = select(AssistantConversation).where(
        AssistantConversation.id == conversation_id,
        AssistantConversation.created_by_id == current_user.id,
    )
    if for_update:
        query = query.with_for_update()
    conversation = db.scalar(query.execution_options(populate_existing=True))
    if conversation is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return conversation


def get_owned_canvas_document(
    db: Session,
    current_user: User,
    document_id: int,
    *,
    for_update: bool = False,
) -> AssistantCanvasDocument:
    query = (
        select(AssistantCanvasDocument)
        .join(AssistantCanvasDocument.conversation)
        .where(
            AssistantCanvasDocument.id == document_id,
            AssistantConversation.created_by_id == current_user.id,
        )
    )
    if for_update:
        query = query.with_for_update(of=AssistantCanvasDocument)
    document = db.scalar(query.execution_options(populate_existing=True))
    if document is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Canvas document not found",
        )
    return document


def list_canvas_documents(
    db: Session,
    current_user: User,
    conversation_id: int,
    *,
    include_archived: bool = False,
) -> list[AssistantCanvasDocument]:
    get_owned_canvas_conversation(db, current_user, conversation_id)
    query = (
        select(AssistantCanvasDocument)
        .where(AssistantCanvasDocument.conversation_id == conversation_id)
        .order_by(
            AssistantCanvasDocument.updated_at.desc(),
            AssistantCanvasDocument.id.desc(),
        )
    )
    if not include_archived:
        query = query.where(AssistantCanvasDocument.status == "draft")
    return list(db.scalars(query))


def active_canvas_document_id(conversation: AssistantConversation) -> int | None:
    state = _load_conversation_state(conversation)
    value = state.get(ACTIVE_CANVAS_STATE_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def get_active_canvas_document(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
) -> AssistantCanvasDocument | None:
    document_id = active_canvas_document_id(conversation)
    if document_id is None:
        return None
    document = db.scalar(
        select(AssistantCanvasDocument)
        .where(
            AssistantCanvasDocument.id == document_id,
            AssistantCanvasDocument.conversation_id == conversation.id,
            AssistantCanvasDocument.status == "draft",
        )
        .execution_options(populate_existing=True)
    )
    if document is None or conversation.created_by_id != current_user.id:
        return None
    return document


def set_active_canvas_document(
    db: Session,
    current_user: User,
    conversation_id: int,
    document_id: int | None,
) -> int | None:
    conversation = get_owned_canvas_conversation(
        db,
        current_user,
        conversation_id,
        for_update=True,
    )
    if document_id is not None:
        document = get_owned_canvas_document(db, current_user, document_id)
        if (
            document.conversation_id != conversation.id
            or document.status != "draft"
        ):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail="Canvas document is not active in this conversation",
            )
    _set_active_canvas_state(conversation, document_id)
    db.commit()
    return document_id


def create_canvas_document(
    db: Session,
    current_user: User,
    *,
    conversation_id: int,
    title: str,
    document_type: str,
    content: str | None,
    organization_id: int | None,
    edit_source: str,
    source_message_id: int | None = None,
    source_tool_call_id: str | None = None,
    creation_id: str | None = None,
    commit: bool = True,
) -> AssistantCanvasDocument:
    conversation = get_owned_canvas_conversation(
        db,
        current_user,
        conversation_id,
        for_update=True,
    )
    normalized_title = _normalize_title(title)
    _validate_document_type(document_type)
    _validate_edit_source(edit_source)
    _validate_content(content or "")
    _ensure_accessible_organization(db, current_user, organization_id)

    initial_content = content
    if initial_content is None or not initial_content.strip():
        initial_content = default_canvas_content(document_type, normalized_title)
    creation_payload_sha256 = (
        _canonical_payload_sha256(
            {
                "conversation_id": conversation.id,
                "title": normalized_title,
                "document_type": document_type,
                "content": initial_content,
                "organization_id": organization_id,
                "edit_source": edit_source,
            }
        )
        if creation_id
        else None
    )

    if creation_id:
        existing = db.scalar(
            select(AssistantCanvasDocument).where(
                AssistantCanvasDocument.conversation_id == conversation.id,
                AssistantCanvasDocument.creation_id == creation_id,
            )
        )
        if existing is not None:
            if existing.creation_payload_sha256 != creation_payload_sha256:
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail="Canvas creation ID was reused with a different payload",
                )
            if existing.status == "draft" and conversation.status == "active":
                _set_active_canvas_state(conversation, existing.id)
            _finish_canvas_write(db, existing, commit=commit)
            return existing

    _require_active_conversation(conversation)

    document = AssistantCanvasDocument(
        conversation_id=conversation.id,
        organization_id=organization_id,
        document_type=document_type,
        title=normalized_title,
        content=initial_content,
        status="draft",
        current_revision=1,
        creation_id=creation_id,
        creation_payload_sha256=creation_payload_sha256,
        created_by_id=current_user.id,
        updated_by_id=current_user.id,
        source_message_id=source_message_id,
    )
    db.add(document)
    db.flush()
    _add_revision(
        db,
        document,
        revision_number=1,
        title=document.title,
        content=document.content,
        edit_source=edit_source,
        created_by_id=current_user.id,
        source_message_id=source_message_id,
        source_tool_call_id=source_tool_call_id,
        mutation_id=creation_id,
        mutation_payload_sha256=creation_payload_sha256,
        change_summary="Creación del borrador",
    )
    _set_active_canvas_state(conversation, document.id)
    _finish_canvas_write(db, document, commit=commit)
    return document


def update_canvas_document(
    db: Session,
    current_user: User,
    document_id: int,
    *,
    expected_revision: int,
    title: str | None = None,
    content: str | None = None,
    status: str | None = None,
    change_summary: str | None = None,
    edit_source: str,
    source_message_id: int | None = None,
    source_tool_call_id: str | None = None,
    mutation_id: str | None = None,
    make_active: bool = False,
    force_revision: bool = False,
    mutation_operation: str = "update",
    mutation_context: dict | None = None,
    commit: bool = True,
) -> AssistantCanvasDocument:
    initial_document = get_owned_canvas_document(
        db,
        current_user,
        document_id,
    )
    conversation = get_owned_canvas_conversation(
        db,
        current_user,
        initial_document.conversation_id,
        for_update=True,
    )
    document = get_owned_canvas_document(
        db,
        current_user,
        document_id,
        for_update=True,
    )
    _validate_edit_source(edit_source)

    normalized_title = _normalize_title(title) if title is not None else None
    if content is not None:
        _validate_content(content)
    if status is not None and status not in CANVAS_DOCUMENT_STATUSES:
        raise ValueError(f"invalid canvas document status: {status}")
    normalized_change_summary = _normalize_change_summary(change_summary)
    mutation_payload_sha256 = (
        _canonical_payload_sha256(
            {
                "operation": mutation_operation,
                "context": mutation_context or {},
                "document_id": document.id,
                "expected_revision": expected_revision,
                "title_provided": title is not None,
                "title": normalized_title,
                "content_provided": content is not None,
                "content": content,
                "status_provided": status is not None,
                "status": status,
                "change_summary": normalized_change_summary,
                "edit_source": edit_source,
                "make_active": make_active,
                "force_revision": force_revision,
            }
        )
        if mutation_id
        else None
    )

    if mutation_id:
        existing_revision = db.scalar(
            select(AssistantCanvasRevision).where(
                AssistantCanvasRevision.document_id == document.id,
                AssistantCanvasRevision.mutation_id == mutation_id,
            )
        )
        if existing_revision is not None:
            if (
                existing_revision.mutation_payload_sha256
                != mutation_payload_sha256
            ):
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail="Canvas mutation ID was reused with a different payload",
                )
            if (
                make_active
                and document.status == "draft"
                and conversation.status == "active"
            ):
                _set_active_canvas_state(conversation, document.id)
            _finish_canvas_write(db, document, commit=commit)
            return document

    _require_active_conversation(conversation)

    if document.status == "archived" and (
        title is not None
        or content is not None
        or edit_source == "restore"
        or status != "archived"
    ):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Canvas document is archived",
        )

    if document.current_revision != expected_revision:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Canvas document revision conflict",
        )

    next_title = document.title if normalized_title is None else normalized_title
    next_content = document.content if content is None else content
    _validate_content(next_content)

    content_changed = next_title != document.title or next_content != document.content
    status_changed = status is not None and status != document.status
    revision_needed = (
        content_changed
        or status_changed
        or force_revision
        or mutation_id is not None
    )
    if revision_needed:
        document.current_revision += 1
        document.title = next_title
        document.content = next_content
        document.updated_by_id = current_user.id
        _add_revision(
            db,
            document,
            revision_number=document.current_revision,
            title=document.title,
            content=document.content,
            edit_source=edit_source,
            created_by_id=current_user.id,
            source_message_id=source_message_id,
            source_tool_call_id=source_tool_call_id,
            mutation_id=mutation_id,
            mutation_payload_sha256=mutation_payload_sha256,
            change_summary=normalized_change_summary,
        )

    if status_changed:
        document.status = status
        document.updated_by_id = current_user.id

    if document.status == "archived":
        if active_canvas_document_id(conversation) == document.id:
            _set_active_canvas_state(conversation, None)
    elif make_active:
        _set_active_canvas_state(conversation, document.id)

    _finish_canvas_write(db, document, commit=commit)
    return document


def list_canvas_revisions(
    db: Session,
    current_user: User,
    document_id: int,
) -> list[AssistantCanvasRevision]:
    document = get_owned_canvas_document(db, current_user, document_id)
    return list(
        db.scalars(
            select(AssistantCanvasRevision)
            .where(AssistantCanvasRevision.document_id == document.id)
            .order_by(AssistantCanvasRevision.revision_number.desc())
            .limit(MAX_CANVAS_REVISIONS_RETURNED)
        )
    )


def restore_canvas_revision(
    db: Session,
    current_user: User,
    document_id: int,
    revision_number: int,
    *,
    expected_revision: int,
    change_summary: str | None = None,
    source_message_id: int | None = None,
    source_tool_call_id: str | None = None,
    mutation_id: str | None = None,
    make_active: bool = False,
    commit: bool = True,
) -> AssistantCanvasDocument:
    document = get_owned_canvas_document(
        db,
        current_user,
        document_id,
    )
    source_revision = db.scalar(
        select(AssistantCanvasRevision).where(
            AssistantCanvasRevision.document_id == document.id,
            AssistantCanvasRevision.revision_number == revision_number,
        )
    )
    if source_revision is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Canvas document revision not found",
        )
    summary = change_summary or f"Restaurada la revisión {revision_number}"
    return update_canvas_document(
        db,
        current_user,
        document.id,
        expected_revision=expected_revision,
        title=source_revision.title,
        content=source_revision.content,
        change_summary=summary,
        edit_source="restore",
        source_message_id=source_message_id,
        source_tool_call_id=source_tool_call_id,
        mutation_id=mutation_id,
        make_active=make_active,
        force_revision=True,
        mutation_operation="restore",
        mutation_context={"revision_number": revision_number},
        commit=commit,
    )


def serialize_revision(revision: AssistantCanvasRevision) -> dict:
    compact = " ".join(revision.content.split())
    excerpt = compact[:180] + ("…" if len(compact) > 180 else "")
    return {
        "id": revision.id,
        "document_id": revision.document_id,
        "revision_number": revision.revision_number,
        "title": revision.title,
        "content_sha256": revision.content_sha256,
        "content_excerpt": excerpt,
        "change_summary": revision.change_summary,
        "edit_source": revision.edit_source,
        "created_by_id": revision.created_by_id,
        "source_message_id": revision.source_message_id,
        "source_tool_call_id": revision.source_tool_call_id,
        "created_at": revision.created_at.isoformat(),
    }


def default_canvas_content(document_type: str, title: str) -> str:
    if document_type == "municipal_ordinance":
        return (
            f"# {title}\n\n"
            "> Borrador de trabajo. No aprobado ni publicado.\n\n"
            "## Exposición de motivos\n\n"
            "[Desarrollar]\n\n"
            "## Articulado\n\n"
            "### Artículo 1. Objeto\n\n"
            "[Desarrollar]\n\n"
            "## Disposiciones finales\n\n"
            "[Desarrollar]\n"
        )
    return (
        f"# {title}\n\n"
        "> Borrador de trabajo. No aprobado ni publicado.\n\n"
        "[Empieza a redactar aquí]\n"
    )


def _add_revision(
    db: Session,
    document: AssistantCanvasDocument,
    *,
    revision_number: int,
    title: str,
    content: str,
    edit_source: str,
    created_by_id: int | None,
    source_message_id: int | None,
    source_tool_call_id: str | None,
    mutation_id: str | None,
    mutation_payload_sha256: str | None,
    change_summary: str | None,
) -> AssistantCanvasRevision:
    revision = AssistantCanvasRevision(
        document_id=document.id,
        revision_number=revision_number,
        title=title,
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        change_summary=change_summary,
        edit_source=edit_source,
        mutation_id=mutation_id,
        mutation_payload_sha256=mutation_payload_sha256,
        source_tool_call_id=source_tool_call_id,
        created_by_id=created_by_id,
        source_message_id=source_message_id,
    )
    db.add(revision)
    return revision


def _finish_canvas_write(
    db: Session,
    document: AssistantCanvasDocument,
    *,
    commit: bool,
) -> None:
    db.flush()
    if commit:
        db.commit()
    db.refresh(document)


def _ensure_accessible_organization(
    db: Session,
    current_user: User,
    organization_id: int | None,
) -> None:
    if organization_id is None:
        return
    organization = db.scalar(
        get_accessible_organizations_query(current_user).where(
            Organization.id == organization_id
        )
    )
    if organization is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )


def _require_active_conversation(conversation: AssistantConversation) -> None:
    if conversation.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conversation is archived",
        )


def _normalize_title(title: str) -> str:
    normalized = str(title).strip()
    if not normalized:
        raise ValueError("title cannot be empty")
    if len(normalized) > 255:
        raise ValueError("title cannot exceed 255 characters")
    return normalized


def _validate_content(content: str) -> None:
    if len(content) > MAX_CANVAS_CONTENT_CHARS:
        raise ValueError(
            f"content cannot exceed {MAX_CANVAS_CONTENT_CHARS} characters"
        )


def _validate_document_type(document_type: str) -> None:
    if document_type not in CANVAS_DOCUMENT_TYPES:
        raise ValueError(f"invalid canvas document type: {document_type}")


def _validate_edit_source(edit_source: str) -> None:
    if edit_source not in CANVAS_REVISION_SOURCES:
        raise ValueError(f"invalid canvas edit source: {edit_source}")


def _normalize_change_summary(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:1000] or None


def _canonical_payload_sha256(payload: dict) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _load_conversation_state(conversation: AssistantConversation) -> dict:
    if not conversation.state:
        return {}
    try:
        state = json.loads(conversation.state)
    except json.JSONDecodeError:
        logger.warning(
            "Assistant conversation state is malformed while managing canvas: %s",
            conversation.id,
        )
        return {}
    return state if isinstance(state, dict) else {}


def _set_active_canvas_state(
    conversation: AssistantConversation,
    document_id: int | None,
) -> None:
    state = _load_conversation_state(conversation)
    if document_id is None:
        state.pop(ACTIVE_CANVAS_STATE_KEY, None)
    else:
        state[ACTIVE_CANVAS_STATE_KEY] = document_id
    conversation.state = json.dumps(state, ensure_ascii=False) if state else None
