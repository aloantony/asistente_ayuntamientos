from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.municipalities.schemas import MunicipalitySummary
from app.organizations.schemas import OrganizationSummary

SidebarShortcutId = Literal[
    "ordinance_library",
    "requirements",
    "projects",
    "inventory",
    "maintenance",
    "admin",
    "municipal_ordinances",
    "municipal_facilities",
    "municipal_people",
    "municipal_roadmap",
    "map_municipalities",
    "admin_product",
    "admin_memory",
    "admin_users",
    "admin_groups",
    "admin_organizations",
    "admin_roles",
    "admin_municipalities",
    "admin_ordinances",
]


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
    sidebar_shortcut_ids: list[SidebarShortcutId] | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SidebarShortcutsUpdate(BaseModel):
    shortcut_ids: list[SidebarShortcutId] = Field(max_length=19)

    @field_validator("shortcut_ids")
    @classmethod
    def reject_duplicate_shortcuts(
        cls,
        shortcut_ids: list[SidebarShortcutId],
    ) -> list[SidebarShortcutId]:
        if len(shortcut_ids) != len(set(shortcut_ids)):
            raise ValueError("shortcut_ids must not contain duplicates")
        return shortcut_ids


class SidebarShortcutsRead(BaseModel):
    shortcut_ids: list[SidebarShortcutId] | None
