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


class AssistantStatusRead(BaseModel):
    enabled: bool
    model: str


class AssistantActionRead(BaseModel):
    tool: str
    ok: bool
    input: dict
    result: str


class AssistantMessageRead(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    actions: list[AssistantActionRead] = []
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


class AssistantConversationRead(BaseModel):
    id: int
    title: str
    status: Literal["active", "archived"]
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
