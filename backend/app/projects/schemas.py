from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

ProjectStatus = Literal["active", "paused", "completed", "archived"]


class ProjectUserSummary(BaseModel):
    id: int
    email: EmailStr
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class ProjectGroupSummary(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class ProjectRead(BaseModel):
    id: int
    name: str
    description: str | None
    status: ProjectStatus
    users: list[ProjectUserSummary]
    groups: list[ProjectGroupSummary]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: ProjectStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    status: ProjectStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)
