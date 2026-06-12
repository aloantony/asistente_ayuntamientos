import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
