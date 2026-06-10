from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.organizations.schemas import OrganizationSummary


class UserRead(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    is_active: bool
    is_superuser: bool
    permissions: list[str] = Field(default_factory=list)
    organizations: list[OrganizationSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
