import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryCategory = Literal[
    "protocol",
    "preference",
    "context",
    "decision",
    "open_question",
]
MemoryStatus = Literal[
    "proposed",
    "approved",
    "rejected",
    "archived",
    "blocked",
]
MemorySensitivity = Literal["normal", "personal", "sensitive", "legal"]
KnowledgeProposalStatus = Literal["proposed", "approved", "rejected"]
KnowledgeSourceType = Literal[
    "official",
    "public_administration",
    "news",
    "provider",
    "blog",
    "unknown",
]
KnowledgeConfidence = Literal["low", "medium", "high"]
TransversalFeatureCategory = Literal[
    "process",
    "compliance",
    "automation",
    "documents",
    "citizen_service",
    "other",
]
TransversalFeatureStatus = Literal[
    "proposed",
    "approved",
    "developed",
    "available",
    "rejected",
    "archived",
    "blocked",
]
TransversalFeatureAdoptionStatus = Literal[
    "suggested",
    "accepted",
    "activation_pending",
    "active",
    "rejected",
    "paused",
]
AdminFeedbackCategory = Literal[
    "bug",
    "improvement",
    "missing_capability",
    "data_issue",
    "ux",
    "other",
]
AdminFeedbackStatus = Literal["submitted", "reviewed", "dismissed", "archived"]
AdminFeedbackPriority = Literal["low", "medium", "high", "urgent"]


class AssistantStatusRead(BaseModel):
    enabled: bool
    runtime: str
    model: str
    runtime_healthy: bool | None = None
    planner: "AssistantPlannerStatusRead"
    agents: list["AssistantAgentRead"] = []
    tools: list["AssistantToolRead"] = []


class AssistantAudioTranscriptionRead(BaseModel):
    text: str


class AssistantPlannerStatusRead(BaseModel):
    runtime: str
    enabled: bool
    model: str | None = None
    runtime_healthy: bool | None = None


class AssistantAgentRead(BaseModel):
    key: str
    name: str
    description: str
    tool_names: list[str]
    required_permission: str


class AssistantToolRead(BaseModel):
    name: str
    label: str
    read_only: bool
    domain: str
    required_permission: str | None = None
    risk_level: Literal["low", "medium", "high"]
    requires_confirmation: bool
    requires_review: bool
    input_schema_summary: str
    output_summary_shape: str
    user_visible_summary_template: str


class AssistantActionRead(BaseModel):
    tool: str
    ok: bool
    input: dict
    result: str
    audit: dict | None = None


class AssistantMessageRead(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    actions: list[AssistantActionRead] = []
    agent_key: str | None = None
    routing: dict | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("actions", mode="before")
    @classmethod
    def parse_actions(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return json.loads(value)
        return value

    @field_validator("routing", mode="before")
    @classmethod
    def parse_routing(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return value


class AssistantConversationRead(BaseModel):
    id: int
    title: str
    status: Literal["active", "archived"]
    folder_id: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantConversationDetail(AssistantConversationRead):
    messages: list[AssistantMessageRead]


class AssistantConversationCreate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["active", "archived"] | None = None
    folder_id: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantConversationFolderRead(BaseModel):
    id: int
    name: str
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantConversationFolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sort_order: int = 0

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantConversationFolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sort_order: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantUserMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantMemoryUserSummary(BaseModel):
    id: int
    email: str
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class AssistantMemoryEntryRead(BaseModel):
    id: int
    organization_id: int
    category: MemoryCategory
    content: str
    status: MemoryStatus
    sensitivity: MemorySensitivity
    source_conversation_id: int | None
    source_message_id: int | None
    proposed_by_id: int | None
    reviewed_by_id: int | None
    review_notes: str | None
    reviewed_at: datetime | None
    proposed_by: AssistantMemoryUserSummary | None
    reviewed_by: AssistantMemoryUserSummary | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantMemoryEntryUpdate(BaseModel):
    category: MemoryCategory | None = None
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    status: MemoryStatus | None = None
    sensitivity: MemorySensitivity | None = None
    review_notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantKnowledgeProposalRead(BaseModel):
    id: int
    organization_id: int
    title: str
    summary: str
    content: str | None
    source_url: str
    source_title: str | None
    source_type: KnowledgeSourceType
    confidence: KnowledgeConfidence
    status: KnowledgeProposalStatus
    sensitivity: MemorySensitivity
    requires_legal_review: bool
    source_conversation_id: int | None
    source_message_id: int | None
    proposed_by_id: int | None
    reviewed_by_id: int | None
    review_notes: str | None
    reviewed_at: datetime | None
    proposed_by: AssistantMemoryUserSummary | None
    reviewed_by: AssistantMemoryUserSummary | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantKnowledgeProposalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    content: str | None = Field(default=None, max_length=2000)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_title: str | None = Field(default=None, max_length=500)
    source_type: KnowledgeSourceType | None = None
    confidence: KnowledgeConfidence | None = None
    status: KnowledgeProposalStatus | None = None
    sensitivity: MemorySensitivity | None = None
    requires_legal_review: bool | None = None
    review_notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantAdminFeedbackRead(BaseModel):
    id: int
    organization_id: int | None
    category: AdminFeedbackCategory
    title: str
    description: str
    priority: AdminFeedbackPriority
    status: AdminFeedbackStatus
    source_conversation_id: int | None
    source_message_id: int | None
    submitted_by_id: int | None
    reviewed_by_id: int | None
    review_notes: str | None
    reviewed_at: datetime | None
    submitted_by: AssistantMemoryUserSummary | None
    reviewed_by: AssistantMemoryUserSummary | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantAdminFeedbackUpdate(BaseModel):
    status: AdminFeedbackStatus | None = None
    priority: AdminFeedbackPriority | None = None
    review_notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantTransversalFeatureRead(BaseModel):
    id: int
    source_requirement_id: int | None
    source_organization_id: int
    source_conversation_id: int | None
    source_message_id: int | None
    title: str
    summary: str
    rationale: str
    category: TransversalFeatureCategory
    status: TransversalFeatureStatus
    sensitivity: MemorySensitivity
    auto_activatable: bool
    proposed_by_id: int | None
    reviewed_by_id: int | None
    review_notes: str | None
    reviewed_at: datetime | None
    proposed_by: AssistantMemoryUserSummary | None
    reviewed_by: AssistantMemoryUserSummary | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantTransversalFeatureUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, min_length=1, max_length=2000)
    category: TransversalFeatureCategory | None = None
    status: TransversalFeatureStatus | None = None
    sensitivity: MemorySensitivity | None = None
    auto_activatable: bool | None = None
    review_notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantTransversalFeatureAdoptionRead(BaseModel):
    id: int
    feature_id: int
    organization_id: int
    status: TransversalFeatureAdoptionStatus
    requested_by_id: int | None
    approved_by_id: int | None
    source_conversation_id: int | None
    source_message_id: int | None
    notes: str | None
    activated_at: datetime | None
    feature: AssistantTransversalFeatureRead
    requested_by: AssistantMemoryUserSummary | None
    approved_by: AssistantMemoryUserSummary | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssistantTransversalFeatureAdoptionUpdate(BaseModel):
    status: TransversalFeatureAdoptionStatus | None = None
    notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)
