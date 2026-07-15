from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MaintenanceStatus = Literal[
    "planned",
    "scheduled",
    "in_progress",
    "completed",
    "cancelled",
]
MaintenancePriority = Literal["low", "normal", "high", "urgent"]
MaintenanceType = Literal[
    "preventive",
    "corrective",
    "inspection",
    "cleaning",
    "other",
]
MaintenanceEventType = Literal["created", "updated", "transition"]


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class MaintenanceAssetSummary(BaseModel):
    id: int
    code: str | None
    name: str
    status: Literal["active", "inactive", "retired", "archived"]

    model_config = ConfigDict(from_attributes=True)


class MaintenanceAssigneeSummary(BaseModel):
    id: int
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class MaintenanceOrderEventRead(BaseModel):
    id: int
    order_id: int
    organization_id: int
    event_type: MaintenanceEventType
    from_status: MaintenanceStatus | None
    to_status: MaintenanceStatus | None
    changed_fields: list[str]
    note: str | None
    actor_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MaintenanceOrderRead(BaseModel):
    id: int
    organization_id: int
    municipality_id: int
    asset_id: int
    title: str
    description: str | None
    maintenance_type: MaintenanceType
    priority: MaintenancePriority
    status: MaintenanceStatus
    scheduled_for: date | None
    estimated_minutes: int | None
    assigned_to_id: int | None
    created_by_id: int
    updated_by_id: int
    created_at: datetime
    updated_at: datetime
    asset: MaintenanceAssetSummary
    assigned_to: MaintenanceAssigneeSummary | None

    model_config = ConfigDict(from_attributes=True)


class MaintenanceOrderDetail(MaintenanceOrderRead):
    events: list[MaintenanceOrderEventRead] = Field(default_factory=list)


class MaintenanceOrderCreate(BaseModel):
    asset_id: int
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    maintenance_type: MaintenanceType = "other"
    priority: MaintenancePriority = "normal"
    scheduled_for: date | None = None
    estimated_minutes: int | None = Field(default=None, gt=0)
    assigned_to_id: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )


class MaintenanceOrderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    maintenance_type: MaintenanceType | None = None
    priority: MaintenancePriority | None = None
    scheduled_for: date | None = None
    estimated_minutes: int | None = Field(default=None, gt=0)
    assigned_to_id: int | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in ("title", "maintenance_type", "priority"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MaintenanceOrderTransition(BaseModel):
    status: MaintenanceStatus
    note: str | None = Field(default=None, max_length=2000)
    scheduled_for: date | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("note", mode="before")(blank_to_none)
