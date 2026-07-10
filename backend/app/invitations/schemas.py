from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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

    model_config = ConfigDict(extra="forbid")


class InvitationPreview(BaseModel):
    email: EmailStr
    organization_name: str
    expires_at: datetime


class InvitationAcceptRequest(InvitationPreviewRequest):
    pass


class InvitationAccepted(BaseModel):
    detail: str
    organization_id: int
    organization_name: str


class InvitationRevoked(BaseModel):
    invitation_id: int
    detail: str
