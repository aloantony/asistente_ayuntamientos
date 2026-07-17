from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CanvasDocumentType = Literal[
    "municipal_ordinance",
    "regulation",
    "report",
    "letter",
    "minutes",
    "other",
]
CanvasDocumentStatus = Literal["draft", "archived"]
CanvasEditSource = Literal["user", "assistant", "restore"]

MAX_CANVAS_CONTENT_CHARS = 60_000


class AssistantCanvasDocumentSummary(BaseModel):
    id: int
    conversation_id: int
    organization_id: int | None
    document_type: CanvasDocumentType
    title: str
    status: CanvasDocumentStatus
    current_revision: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantCanvasDocumentRead(AssistantCanvasDocumentSummary):
    content: str
    content_format: Literal["markdown"] = "markdown"


class AssistantCanvasWorkspaceRead(BaseModel):
    documents: list[AssistantCanvasDocumentSummary]
    active_document_id: int | None


class AssistantCanvasDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    document_type: CanvasDocumentType = "other"
    organization_id: int | None = Field(default=None, gt=0)
    content: str | None = Field(default=None, max_length=MAX_CANVAS_CONTENT_CHARS)
    creation_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title


class AssistantCanvasDocumentUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, max_length=MAX_CANVAS_CONTENT_CHARS)
    status: CanvasDocumentStatus | None = None
    change_summary: str | None = Field(default=None, max_length=1000)
    mutation_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def normalize_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title

    @model_validator(mode="after")
    def require_update(self):
        changed_fields = self.model_fields_set.intersection(
            {"title", "content", "status"}
        )
        if not changed_fields:
            raise ValueError("at least one document field must be provided")
        if any(getattr(self, field_name) is None for field_name in changed_fields):
            raise ValueError("document fields cannot be null")
        return self


class AssistantCanvasRevisionRead(BaseModel):
    id: int
    document_id: int
    revision_number: int
    title: str
    content_sha256: str
    content_excerpt: str
    change_summary: str | None
    edit_source: CanvasEditSource
    created_by_id: int | None
    source_message_id: int | None
    source_tool_call_id: str | None
    created_at: datetime


class AssistantCanvasRevisionRestore(BaseModel):
    expected_revision: int = Field(ge=1)
    change_summary: str | None = Field(default=None, max_length=1000)
    mutation_id: str | None = Field(default=None, min_length=1, max_length=255)


class AssistantCanvasSelectionUpdate(BaseModel):
    document_id: int | None = Field(default=None, gt=0)


class AssistantCanvasSelectionRead(BaseModel):
    active_document_id: int | None
