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
OrdinanceCurationStatus = Literal[
    "approved",
    "pending_review",
    "needs_changes",
    "rejected",
]
OfficialLegalSourceType = Literal["boe", "bop", "autonomic", "municipal", "other"]
OfficialLegalSourceStatus = Literal["active", "archived"]
OrdinanceImportJobStatus = Literal[
    "draft",
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
]
OrdinanceImportItemStatus = Literal[
    "discovered",
    "fetching",
    "extracted",
    "pending_review",
    "approved",
    "rejected",
    "duplicate",
    "failed",
]
OrdinanceReviewDecision = Literal["approve", "needs_changes", "reject"]
OrdinanceReviewReportStatus = Literal[
    "agent_reviewed",
    "human_approved",
    "human_rejected",
    "superseded",
]
OrdinanceLegalChunkReviewStatus = Literal[
    "pending_review",
    "approved",
    "rejected",
]
OrdinanceEmbeddingStatus = Literal["pending", "ready", "failed", "disabled"]

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
    curation_status: OrdinanceCurationStatus
    import_job_id: int | None
    source_hash: str | None
    extraction_status: str
    confidence_score: float | None
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
    curation_status: OrdinanceCurationStatus = "approved"
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
    curation_status: OrdinanceCurationStatus | None = None
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


class OfficialLegalSourceRead(BaseModel):
    id: int
    name: str
    base_url: str
    domain: str
    source_type: OfficialLegalSourceType
    status: OfficialLegalSourceStatus
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OfficialLegalSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=2000)
    domain: str = Field(min_length=1, max_length=255)
    source_type: OfficialLegalSourceType
    status: OfficialLegalSourceStatus = "active"
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class OfficialLegalSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=2000)
    domain: str | None = Field(default=None, min_length=1, max_length=255)
    source_type: OfficialLegalSourceType | None = None
    status: OfficialLegalSourceStatus | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class OrdinanceImportSourceInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    municipality_id: int | None = None
    official_source_id: int | None = None
    title: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(str_strip_whitespace=True)


class OrdinanceImportJobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    topic: str | None = Field(default=None, max_length=255)
    subtopic: str | None = Field(default=None, max_length=255)
    search_query: str | None = None
    municipality_ids: list[int] = Field(default_factory=list)
    official_source_ids: list[int] = Field(default_factory=list)
    source_urls: list[OrdinanceImportSourceInput] = Field(default_factory=list)
    review_criteria: str = Field(min_length=1)

    model_config = ConfigDict(str_strip_whitespace=True)


class OrdinanceImportJobRead(BaseModel):
    id: int
    title: str
    description: str | None
    topic: str | None
    subtopic: str | None
    search_query: str | None
    municipality_ids: list[int]
    official_source_ids: list[int]
    source_urls: list[OrdinanceImportSourceInput]
    review_criteria: str
    source_policy: Literal["official_only"]
    status: OrdinanceImportJobStatus
    created_by_id: int | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    item_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrdinanceReviewChecklistItem(BaseModel):
    key: str
    label: str
    passed: bool
    detail: str | None = None


class OrdinanceReviewReportRead(BaseModel):
    id: int
    ordinance_id: int
    import_item_id: int | None
    status: OrdinanceReviewReportStatus
    proposed_decision: OrdinanceReviewDecision
    confidence_score: float
    checklist: list[OrdinanceReviewChecklistItem]
    summary: str | None
    doubts: str | None
    reviewed_by_agent: bool
    reviewed_by_id: int | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrdinanceImportItemRead(BaseModel):
    id: int
    job_id: int
    municipality_id: int | None
    official_source_id: int | None
    ordinance_id: int | None
    source_url: str
    source_title: str | None
    status: OrdinanceImportItemStatus
    source_hash: str | None
    extracted_metadata: dict | None
    confidence_score: float | None
    error_message: str | None
    ordinance: OrdinanceRead | None = None
    review_reports: list[OrdinanceReviewReportRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrdinanceImportJobDetail(OrdinanceImportJobRead):
    items: list[OrdinanceImportItemRead]


class OrdinanceImportEnqueueRead(BaseModel):
    job_id: int
    status: OrdinanceImportJobStatus
    queue_job_id: str | None


class OrdinanceImportItemReviewUpdate(BaseModel):
    decision: OrdinanceReviewDecision
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class OrdinanceLegalChunkRead(BaseModel):
    id: int
    ordinance_id: int
    import_item_id: int | None
    chunk_index: int
    heading: str | None
    citation: str | None
    text: str
    source_url: str | None
    source_locator: str | None
    review_status: OrdinanceLegalChunkReviewStatus
    embedding_model: str | None
    embedding_status: OrdinanceEmbeddingStatus
    embedded_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrdinanceComparisonEntry(BaseModel):
    municipality_id: int
    municipality_name: str
    ordinance_id: int
    title: str
    topic: str
    subtopic: str | None
    status: OrdinanceStatus
    curation_status: OrdinanceCurationStatus
    publication_date: date | None
    effective_date: date | None
    source_url: str | None
    summary: str | None
    confidence_score: float | None


class OrdinanceComparisonRow(BaseModel):
    topic: str
    subtopic: str | None
    entries: list[OrdinanceComparisonEntry]


class OrdinanceComparisonRead(BaseModel):
    municipality_ids: list[int]
    include_pending: bool
    rows: list[OrdinanceComparisonRow]


class OrdinanceSemanticSearchResult(BaseModel):
    chunk_id: int
    ordinance_id: int
    title: str
    municipality_name: str
    citation: str | None
    text: str
    source_url: str | None
    score: float
