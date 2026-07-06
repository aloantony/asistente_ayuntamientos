from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocumentStatus = Literal["active", "archived"]
DocumentWorkArtifactType = Literal[
    "report",
    "note",
    "comparison",
    "communication",
    "checklist",
]
DocumentWorkArtifactStatus = Literal[
    "draft",
    "in_review",
    "approved",
    "changes_requested",
    "export_requested",
    "archived",
]


class DocumentRead(BaseModel):
    id: int
    organization_id: int
    project_id: int
    original_filename: str
    stored_filename: str
    storage_backend: str
    storage_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    status: DocumentStatus
    uploaded_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentUpdate(BaseModel):
    status: DocumentStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class DocumentWorkArtifactCreate(BaseModel):
    artifact_type: DocumentWorkArtifactType
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    source_summary: str | None = None
    source_document_ids: list[int] = Field(default_factory=list)

    model_config = ConfigDict(str_strip_whitespace=True)


class DocumentWorkArtifactUpdate(BaseModel):
    artifact_type: DocumentWorkArtifactType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    source_summary: str | None = None
    source_document_ids: list[int] | None = None
    status: DocumentWorkArtifactStatus | None = None
    review_notes: str | None = None
    export_format: str | None = Field(default=None, max_length=30)

    model_config = ConfigDict(str_strip_whitespace=True)


class DocumentWorkArtifactRead(BaseModel):
    id: int
    organization_id: int
    project_id: int
    artifact_type: DocumentWorkArtifactType
    title: str
    content: str
    status: DocumentWorkArtifactStatus
    source_summary: str | None
    source_document_ids: list[int]
    review_notes: str | None
    export_format: str | None
    created_by_id: int | None
    reviewed_by_id: int | None
    export_requested_by_id: int | None
    exported_by_id: int | None
    source_conversation_id: int | None
    source_message_id: int | None
    reviewed_at: datetime | None
    export_requested_at: datetime | None
    exported_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
