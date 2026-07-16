"""Document domain invariants shared by HTTP and future ingestion paths."""

from app.documents.models import Document
from app.documents.storage import StoredUpload
from app.projects.models import Project


class DocumentProjectScopeError(ValueError):
    pass


def document_from_stored_upload(
    *,
    project: Project,
    stored_upload: StoredUpload,
    uploaded_by_id: int | None,
) -> Document:
    """Build a document whose tenant identity is derived from its project."""

    if not project.id or not project.organization_id:
        raise DocumentProjectScopeError("Persisted project scope is required")
    expected_storage_prefix = (
        f"organizations/{project.organization_id}/projects/{project.id}/"
    )
    if not stored_upload.storage_key.startswith(expected_storage_prefix):
        raise DocumentProjectScopeError(
            "Stored upload scope does not match the target project"
        )

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
        uploaded_by_id=uploaded_by_id,
    )
    validate_document_project_scope(document, project)
    return document


def validate_document_project_scope(document: Document, project: Project) -> None:
    if document.project_id != project.id:
        raise DocumentProjectScopeError("Document project does not match project")
    if document.organization_id != project.organization_id:
        raise DocumentProjectScopeError(
            "Document organization does not match project organization"
        )
