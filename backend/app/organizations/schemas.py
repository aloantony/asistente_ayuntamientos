from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.municipalities.schemas import MunicipalitySummary

OrganizationStatus = Literal["active", "paused", "archived"]


class OrganizationSummary(BaseModel):
    id: int
    name: str
    status: OrganizationStatus

    model_config = ConfigDict(from_attributes=True)


class OrganizationUserSummary(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    is_active: bool
    is_superuser: bool

    model_config = ConfigDict(from_attributes=True)


class OrganizationRead(BaseModel):
    id: int
    name: str
    description: str | None
    municipality_id: int | None
    municipality: MunicipalitySummary | None
    status: OrganizationStatus
    users: list[OrganizationUserSummary]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    municipality_id: int | None = None
    status: OrganizationStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True)


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    municipality_id: int | None = None
    status: OrganizationStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class OrganizationMembershipResponse(BaseModel):
    organization_id: int
    user_id: int
    detail: str
