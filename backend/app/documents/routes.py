from datetime import datetime, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.documents.models import Document, DocumentWorkArtifact
from app.documents.schemas import (
    DocumentRead,
    DocumentUpdate,
    DocumentWorkArtifactCreate,
    DocumentWorkArtifactRead,
    DocumentWorkArtifactUpdate,
)
from app.documents.storage import (
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidStorageKeyError,
    LocalStorageService,
    UnsupportedDocumentContentTypeError,
)
from app.projects.access import get_project_with_memberships, user_can_access_project
from app.projects.models import Project
from app.rbac.permissions import has_permission
from app.users.models import User

router = APIRouter(tags=["documents"])
storage_service = LocalStorageService()


@router.get(
    "/projects/{project_id}/documents",
    response_model=list[DocumentRead],
)
def list_project_documents(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: Annotated[bool, Query()] = False,
) -> list[Document]:
    project = get_existing_project(db, project_id)
    require_project_document_action(
        db,
        current_user,
        project,
        "documents.view",
    )

    query = (
        select(Document)
        .where(
            Document.project_id == project.id,
            Document.organization_id == project.organization_id,
        )
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    if not include_archived:
        query = query.where(Document.status == "active")

    return list(db.scalars(query))


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_project_document(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File()],
) -> Document:
    project = get_existing_project(db, project_id)
    require_project_document_action(
        db,
        current_user,
        project,
        "documents.upload",
    )

    try:
        stored_upload = storage_service.save_upload_file(
            file,
            organization_id=project.organization_id,
            project_id=project.id,
            max_bytes=settings.document_max_upload_bytes,
        )
    except UnsupportedDocumentContentTypeError:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Unsupported document content type",
        ) from None
    except DocumentTooLargeError:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Document exceeds maximum upload size",
        ) from None
    except EmptyDocumentError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty document upload",
        ) from None
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid document storage key",
        ) from None

    document = Document(
        organization_id=project.organization_id,
        project_id=project.id,
        original_filename=stored_upload.original_filename,
        stored_filename=stored_upload.stored_filename,
        storage_backend=stored_upload.storage_backend,
        storage_key=stored_upload.storage_key,
        content_type=stored_upload.content_type,
        size_bytes=stored_upload.size_bytes,
        checksum_sha256=stored_upload.checksum_sha256,
        status="active",
        uploaded_by_id=current_user.id,
    )
    db.add(document)

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        storage_service.delete_file(stored_upload.storage_key)
        raise

    db.refresh(document)
    return document


@router.get("/documents/{document_id}", response_model=DocumentRead)
def get_document(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Document:
    document = get_existing_document(db, document_id)
    require_document_action(db, current_user, document, "documents.view")
    return document


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FileResponse:
    document = get_existing_document(db, document_id)
    require_document_action(db, current_user, document, "documents.view")

    try:
        file_path = storage_service.resolve_storage_key(document.storage_key)
    except InvalidStorageKeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document file not found",
        ) from None

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document file not found",
        )

    return FileResponse(
        file_path,
        media_type=document.content_type,
        filename=document.original_filename,
    )


@router.patch("/documents/{document_id}", response_model=DocumentRead)
def update_document(
    document_id: int,
    payload: DocumentUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Document:
    document = get_existing_document(db, document_id)
    require_document_action(db, current_user, document, "documents.archive")

    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] is not None:
        document.status = updates["status"]

    db.commit()
    db.refresh(document)
    return document


@router.get(
    "/projects/{project_id}/document-work-artifacts",
    response_model=list[DocumentWorkArtifactRead],
)
def list_project_document_work_artifacts(
    project_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    include_archived: Annotated[bool, Query()] = False,
) -> list[DocumentWorkArtifact]:
    project = get_existing_project(db, project_id)
    require_project_document_action(
        db,
        current_user,
        project,
        "documents.view",
    )

    query = (
        select(DocumentWorkArtifact)
        .where(
            DocumentWorkArtifact.project_id == project.id,
            DocumentWorkArtifact.organization_id == project.organization_id,
        )
        .order_by(DocumentWorkArtifact.created_at.desc(), DocumentWorkArtifact.id.desc())
    )
    if not include_archived:
        query = query.where(DocumentWorkArtifact.status != "archived")

    return list(db.scalars(query))


@router.post(
    "/projects/{project_id}/document-work-artifacts",
    response_model=DocumentWorkArtifactRead,
    status_code=status.HTTP_201_CREATED,
)
def create_project_document_work_artifact(
    project_id: int,
    payload: DocumentWorkArtifactCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DocumentWorkArtifact:
    project = get_existing_project(db, project_id)
    require_project_document_action(
        db,
        current_user,
        project,
        "documents.draft",
    )
    source_document_ids = normalize_source_document_ids(payload.source_document_ids)
    validate_source_documents(db, current_user, project, source_document_ids)

    artifact = DocumentWorkArtifact(
        organization_id=project.organization_id,
        project_id=project.id,
        artifact_type=payload.artifact_type,
        title=payload.title,
        content=payload.content,
        status="draft",
        source_summary=clean_optional_text(payload.source_summary),
        source_document_ids=source_document_ids,
        created_by_id=current_user.id,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


@router.get(
    "/document-work-artifacts/{artifact_id}",
    response_model=DocumentWorkArtifactRead,
)
def get_document_work_artifact(
    artifact_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DocumentWorkArtifact:
    artifact = get_existing_document_work_artifact(db, artifact_id)
    require_document_work_artifact_action(
        db,
        current_user,
        artifact,
        "documents.view",
    )
    return artifact


@router.patch(
    "/document-work-artifacts/{artifact_id}",
    response_model=DocumentWorkArtifactRead,
)
def update_document_work_artifact(
    artifact_id: int,
    payload: DocumentWorkArtifactUpdate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DocumentWorkArtifact:
    artifact = get_existing_document_work_artifact(db, artifact_id)
    updates = payload.model_dump(exclude_unset=True)
    project = artifact.project

    content_fields = {
        key: updates[key]
        for key in (
            "artifact_type",
            "title",
            "content",
            "source_summary",
            "source_document_ids",
        )
        if key in updates
    }
    if content_fields:
        require_document_work_artifact_edit(db, current_user, artifact)
        if "source_document_ids" in content_fields:
            source_document_ids = normalize_source_document_ids(
                content_fields["source_document_ids"]
            )
            validate_source_documents(db, current_user, project, source_document_ids)
            artifact.source_document_ids = source_document_ids
        if "artifact_type" in content_fields and content_fields["artifact_type"] is not None:
            artifact.artifact_type = content_fields["artifact_type"]
        if "title" in content_fields and content_fields["title"] is not None:
            artifact.title = content_fields["title"]
        if "content" in content_fields and content_fields["content"] is not None:
            artifact.content = content_fields["content"]
        if "source_summary" in content_fields:
            artifact.source_summary = clean_optional_text(content_fields["source_summary"])

    requested_status = updates.get("status")
    if requested_status is not None and requested_status != artifact.status:
        apply_document_work_status_transition(
            db,
            current_user,
            artifact,
            requested_status,
            review_notes=updates.get("review_notes"),
            export_format=updates.get("export_format"),
        )
    else:
        if "review_notes" in updates:
            require_document_work_artifact_action(
                db,
                current_user,
                artifact,
                "documents.review",
            )
            artifact.review_notes = clean_optional_text(updates["review_notes"])
        if "export_format" in updates:
            require_document_work_artifact_action(
                db,
                current_user,
                artifact,
                "documents.export",
            )
            artifact.export_format = clean_optional_text(updates["export_format"])

    db.commit()
    db.refresh(artifact)
    return artifact


def get_existing_project(db: Session, project_id: int) -> Project:
    project = get_project_with_memberships(db, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    return project


def get_existing_document(db: Session, document_id: int) -> Document:
    document = db.scalar(
        select(Document)
        .options(selectinload(Document.project))
        .where(Document.id == document_id)
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if document.project.organization_id != document.organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document project organization mismatch",
        )

    return document


def require_document_action(
    db: Session,
    current_user: User,
    document: Document,
    permission_code: str,
) -> None:
    require_project_document_action(
        db,
        current_user,
        document.project,
        permission_code,
    )


def require_project_document_action(
    db: Session,
    current_user: User,
    project: Project,
    permission_code: str,
) -> None:
    if current_user.is_superuser:
        return

    if has_permission(
        current_user,
        "documents.manage",
        db,
        organization_id=project.organization_id,
    ):
        return

    if not has_permission(
        current_user,
        permission_code,
        db,
        organization_id=project.organization_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission required: {permission_code}",
        )

    if not user_can_access_project(db, current_user, project):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project access denied",
        )


def normalize_source_document_ids(source_document_ids: list[int] | None) -> list[int]:
    if not source_document_ids:
        return []
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_document_id in source_document_ids:
        try:
            document_id = int(raw_document_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid source document id",
            ) from None
        if document_id <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid source document id",
            )
        if document_id in seen:
            continue
        normalized.append(document_id)
        seen.add(document_id)
    return normalized


def validate_source_documents(
    db: Session,
    current_user: User,
    project: Project,
    source_document_ids: list[int],
) -> None:
    for document_id in source_document_ids:
        document = get_existing_document(db, document_id)
        if document.project_id != project.id or document.organization_id != project.organization_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Source document does not belong to this project",
            )
        if document.status != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Source document is not active",
            )
        require_document_action(db, current_user, document, "documents.view")


def clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def get_existing_document_work_artifact(
    db: Session,
    artifact_id: int,
) -> DocumentWorkArtifact:
    artifact = db.scalar(
        select(DocumentWorkArtifact)
        .options(selectinload(DocumentWorkArtifact.project))
        .where(DocumentWorkArtifact.id == artifact_id)
    )
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document work artifact not found",
        )
    if artifact.project.organization_id != artifact.organization_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document work artifact project organization mismatch",
        )
    return artifact


def require_document_work_artifact_action(
    db: Session,
    current_user: User,
    artifact: DocumentWorkArtifact,
    permission_code: str,
) -> None:
    require_project_document_action(
        db,
        current_user,
        artifact.project,
        permission_code,
    )


def require_document_work_artifact_edit(
    db: Session,
    current_user: User,
    artifact: DocumentWorkArtifact,
) -> None:
    if artifact.status in {"approved", "export_requested", "archived"}:
        require_document_work_artifact_action(
            db,
            current_user,
            artifact,
            "documents.review",
        )
        return
    require_document_work_artifact_action(
        db,
        current_user,
        artifact,
        "documents.draft",
    )


def apply_document_work_status_transition(
    db: Session,
    current_user: User,
    artifact: DocumentWorkArtifact,
    requested_status: str,
    *,
    review_notes: str | None = None,
    export_format: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    if requested_status == "export_requested":
        require_document_work_artifact_action(
            db,
            current_user,
            artifact,
            "documents.export",
        )
        if artifact.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Document work artifact must be approved before export can be requested",
            )
        artifact.status = "export_requested"
        artifact.export_format = clean_optional_text(export_format)
        artifact.export_requested_by_id = current_user.id
        artifact.export_requested_at = now
        return

    if requested_status in {"in_review", "approved", "changes_requested"}:
        require_document_work_artifact_action(
            db,
            current_user,
            artifact,
            "documents.review",
        )
        artifact.status = requested_status
        artifact.review_notes = clean_optional_text(review_notes)
        artifact.reviewed_by_id = current_user.id
        artifact.reviewed_at = now
        return

    if requested_status == "draft":
        require_document_work_artifact_edit(db, current_user, artifact)
        artifact.status = "draft"
        return

    if requested_status == "archived":
        require_document_work_artifact_action(
            db,
            current_user,
            artifact,
            "documents.review",
        )
        artifact.status = "archived"
        return

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Invalid document work artifact status",
    )
