from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.municipalities.schemas import MunicipalitySummary
from app.organizations.schemas import OrganizationSummary


class SessionOrganizationSummary(OrganizationSummary):
    municipality_id: int | None
    municipality: MunicipalitySummary | None


class UserRead(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    is_active: bool
    is_superuser: bool
    permissions: list[str] = Field(default_factory=list)
    organizations: list[SessionOrganizationSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
