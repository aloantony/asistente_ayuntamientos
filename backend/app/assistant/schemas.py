import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.organizations.schemas import OrganizationSummary

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
    speech_transcription_enabled: bool = False
    speech_synthesis_enabled: bool = False
    speech_synthesis_max_chars: int = 3000
    realtime_voice_enabled: bool = False
    realtime_voice_provider: Literal["openai"] | None = None
    realtime_voice_model: str | None = None
    tools: list["AssistantToolRead"] = []


class AssistantAudioTranscriptionRead(BaseModel):
    text: str


class AssistantSpeechCreate(BaseModel):
    text: str = Field(min_length=1, max_length=20000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantToolRead(BaseModel):
    name: str
    label: str
    read_only: bool
    domain: str
    side_effect: Literal["none", "database_write"]
    approval_policy: Literal["never", "explicit"]
    required_permission: str | None = None


class AssistantActionRead(BaseModel):
    call_id: str | None = None
    tool: str
    ok: bool
    input: dict
    result: str


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


class AssistantRealtimeSessionRead(BaseModel):
    client_secret: str
    client_secret_expires_at: int | None = None
    provider: Literal["openai"] = "openai"
    model: str
    voice: str
    realtime_url: str


class AssistantRealtimeTurnStartCreate(BaseModel):
    turn_id: str = Field(min_length=1, max_length=255)
    user_text: str = Field(min_length=1, max_length=20000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantRealtimeTurnStartRead(BaseModel):
    turn_id: str
    user_message: AssistantMessageRead
    replayed: bool = False


class AssistantRealtimeToolCallCreate(BaseModel):
    call_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    arguments: dict = Field(default_factory=dict)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantRealtimeToolCallRead(BaseModel):
    call_id: str
    ok: bool
    output: str
    action: AssistantActionRead
    user_message: AssistantMessageRead
    confirmation_prompt: str | None = None
    replayed: bool = False


class AssistantRealtimeTurnCreate(BaseModel):
    response_id: str = Field(min_length=1, max_length=255)
    response_status: Literal["completed", "cancelled", "failed", "incomplete"]
    assistant_text: str | None = Field(default=None, max_length=20000)
    interrupted: bool = False

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantRealtimeTurnRead(BaseModel):
    conversation: AssistantConversationRead
    user_message: AssistantMessageRead | None = None
    assistant_message: AssistantMessageRead | None = None
    confirmation_prompt: str | None = None
    confirmation_delivery_required: bool = False
    replayed: bool = False


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
    input_mode: Literal["text", "voice"] = "text"

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantMemoryUserSummary(BaseModel):
    id: int
    email: str
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class AssistantMemoryEntryRead(BaseModel):
    id: int
    organization_id: int
    organization: OrganizationSummary
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
    expected_updated_at: datetime
    sensitive_approval_confirmed: bool = False
    category: MemoryCategory | None = None
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    status: MemoryStatus | None = None
    sensitivity: MemorySensitivity | None = None
    review_notes: str | None = Field(default=None, max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class AssistantAdminFeedbackRead(BaseModel):
    id: int
    organization_id: int | None
    organization: OrganizationSummary | None
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
    expected_updated_at: datetime
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
