from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

RequirementPriority = Literal["low", "medium", "high", "urgent"]
RequirementStatus = Literal[
    "draft",
    "submitted",
    "in_review",
    "needs_clarification",
    "accepted",
    "rejected",
    "converted",
    "archived",
]
RequirementSourceType = Literal[
    "manual",
    "conversation",
    "phone_call",
    "meeting",
    "other",
]
RequirementMessageType = Literal[
    "note",
    "question",
    "answer",
    "clarification",
    "decision",
]


class RequirementOrganizationSummary(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class RequirementProjectSummary(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class RequirementUserSummary(BaseModel):
    id: int
    email: EmailStr
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class RequirementRead(BaseModel):
    id: int
    organization_id: int
    project_id: int | None
    title: str
    summary: str | None
    problem: str | None
    current_process: str | None
    desired_process: str | None
    affected_users: str | None
    involved_documents: str | None
    data_sensitivity_notes: str | None
    legal_notes: str | None
    acceptance_criteria: str | None
    open_questions: str | None
    priority: RequirementPriority
    status: RequirementStatus
    source_type: RequirementSourceType
    created_by_id: int | None
    reviewed_by_id: int | None
    organization: RequirementOrganizationSummary
    project: RequirementProjectSummary | None
    created_by: RequirementUserSummary | None
    reviewed_by: RequirementUserSummary | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RequirementCreate(BaseModel):
    organization_id: int
    project_id: int | None = None
    title: str = Field(min_length=1, max_length=255)
    summary: str | None = None
    problem: str | None = None
    current_process: str | None = None
    desired_process: str | None = None
    affected_users: str | None = None
    involved_documents: str | None = None
    data_sensitivity_notes: str | None = None
    legal_notes: str | None = None
    acceptance_criteria: str | None = None
    open_questions: str | None = None
    priority: RequirementPriority = "medium"
    status: RequirementStatus = "draft"
    source_type: RequirementSourceType = "manual"

    model_config = ConfigDict(str_strip_whitespace=True)


class RequirementUpdate(BaseModel):
    project_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    summary: str | None = None
    problem: str | None = None
    current_process: str | None = None
    desired_process: str | None = None
    affected_users: str | None = None
    involved_documents: str | None = None
    data_sensitivity_notes: str | None = None
    legal_notes: str | None = None
    acceptance_criteria: str | None = None
    open_questions: str | None = None
    priority: RequirementPriority | None = None
    status: RequirementStatus | None = None
    source_type: RequirementSourceType | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class RequirementMessageRead(BaseModel):
    id: int
    requirement_id: int
    author_id: int | None
    body: str
    message_type: RequirementMessageType
    author: RequirementUserSummary | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RequirementMessageCreate(BaseModel):
    body: str = Field(min_length=1)
    message_type: RequirementMessageType = "note"

    model_config = ConfigDict(str_strip_whitespace=True)
