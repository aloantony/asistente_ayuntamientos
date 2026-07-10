from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class InvitationCreate(BaseModel):
    email: EmailStr


class InvitationAdminRead(BaseModel):
    id: int
    organization_id: int
    email: EmailStr
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    invited_by_user_id: int | None
    accepted_by_user_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvitationCreated(InvitationAdminRead):
    token: str


class InvitationPreviewRequest(BaseModel):
    token: str = Field(min_length=40, max_length=200)


class InvitationPreview(BaseModel):
    email: EmailStr
    organization_name: str
    expires_at: datetime
    requires_registration: bool


class InvitationAcceptRequest(InvitationPreviewRequest):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=1024)

    @field_validator("full_name", mode="before")
    @classmethod
    def strip_full_name(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class InvitationAccepted(BaseModel):
    detail: str
    organization_id: int
    organization_name: str


class InvitationRevoked(BaseModel):
    invitation_id: int
    detail: str
