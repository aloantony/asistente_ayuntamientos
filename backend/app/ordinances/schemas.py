from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.documents.schemas import DocumentStatus
from app.municipalities.schemas import MunicipalitySummary

OrdinanceType = Literal[
    "ordinance",
    "regulation",
    "bylaw",
    "tax_ordinance",
    "urban_planning",
    "other",
]
OrdinanceStatus = Literal[
    "active",
    "repealed",
    "partially_repealed",
    "superseded",
    "unknown",
    "archived",
]

OPTIONAL_TEXT_FIELDS = (
    "subtopic",
    "summary",
    "source_url",
    "official_bulletin",
    "bulletin_number",
    "text_content",
    "notes",
    "legal_review_notes",
)


class OrdinanceDocumentSummary(BaseModel):
    id: int
    original_filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus

    model_config = ConfigDict(from_attributes=True)


class OrdinanceBaseRead(BaseModel):
    id: int
    municipality_id: int
    document_id: int | None
    title: str
    topic: str
    subtopic: str | None
    ordinance_type: OrdinanceType
    summary: str | None
    source_url: str | None
    official_bulletin: str | None
    bulletin_number: str | None
    approval_date: date | None
    publication_date: date | None
    effective_date: date | None
    status: OrdinanceStatus
    notes: str | None
    legal_review_notes: str | None
    created_by_id: int | None
    updated_by_id: int | None
    municipality: MunicipalitySummary
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrdinanceListRead(OrdinanceBaseRead):
    """List item without text_content: full legal texts only travel on the
    detail endpoint, never on paginated listings."""


class OrdinanceRead(OrdinanceBaseRead):
    text_content: str | None
    document: OrdinanceDocumentSummary | None


class OrdinanceCreate(BaseModel):
    municipality_id: int
    document_id: int | None = None
    title: str = Field(min_length=1, max_length=500)
    topic: str = Field(min_length=1, max_length=255)
    subtopic: str | None = Field(default=None, max_length=255)
    ordinance_type: OrdinanceType
    summary: str | None = None
    source_url: str | None = Field(default=None, max_length=2000)
    official_bulletin: str | None = Field(default=None, max_length=255)
    bulletin_number: str | None = Field(default=None, max_length=100)
    approval_date: date | None = None
    publication_date: date | None = None
    effective_date: date | None = None
    status: OrdinanceStatus = "unknown"
    text_content: str | None = None
    notes: str | None = None
    legal_review_notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator(*OPTIONAL_TEXT_FIELDS, mode="before")
    @classmethod
    def blank_strings_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class OrdinanceUpdate(BaseModel):
    municipality_id: int | None = None
    document_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    topic: str | None = Field(default=None, min_length=1, max_length=255)
    subtopic: str | None = Field(default=None, max_length=255)
    ordinance_type: OrdinanceType | None = None
    summary: str | None = None
    source_url: str | None = Field(default=None, max_length=2000)
    official_bulletin: str | None = Field(default=None, max_length=255)
    bulletin_number: str | None = Field(default=None, max_length=100)
    approval_date: date | None = None
    publication_date: date | None = None
    effective_date: date | None = None
    status: OrdinanceStatus | None = None
    text_content: str | None = None
    notes: str | None = None
    legal_review_notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator(*OPTIONAL_TEXT_FIELDS, mode="before")
    @classmethod
    def blank_strings_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value
