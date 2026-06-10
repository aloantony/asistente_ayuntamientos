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
from app.documents.models import Document
from app.documents.schemas import DocumentRead, DocumentUpdate
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
