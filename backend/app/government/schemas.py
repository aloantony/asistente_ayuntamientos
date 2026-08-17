from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GovernmentLevel = Literal["alcaldia", "tenencia", "concejalia", "secretaria"]
GovernmentMemberStatus = Literal["active", "archived"]


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def ensure_term_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError("term_end_date cannot be earlier than term_start_date")


class GovernmentMemberRead(BaseModel):
    id: int
    organization_id: int
    level: GovernmentLevel
    full_name: str
    role_title: str
    political_group: str | None
    email: str | None
    phone: str | None
    biography: str | None
    term_start_date: date | None
    term_end_date: date | None
    sort_order: int
    status: GovernmentMemberStatus
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GovernmentMemberCreate(BaseModel):
    organization_id: int
    level: GovernmentLevel
    full_name: str = Field(min_length=1, max_length=255)
    role_title: str = Field(min_length=1, max_length=255)
    political_group: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    biography: str | None = None
    term_start_date: date | None = None
    term_end_date: date | None = None
    sort_order: int = Field(default=0, ge=0)
    status: GovernmentMemberStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "political_group",
        "email",
        "phone",
        "biography",
        mode="before",
    )(blank_to_none)

    @model_validator(mode="after")
    def check_term_range(self):
        ensure_term_range(self.term_start_date, self.term_end_date)
        return self


class GovernmentMemberUpdate(BaseModel):
    level: GovernmentLevel | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role_title: str | None = Field(default=None, min_length=1, max_length=255)
    political_group: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    biography: str | None = None
    term_start_date: date | None = None
    term_end_date: date | None = None
    sort_order: int | None = Field(default=None, ge=0)
    status: GovernmentMemberStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "political_group",
        "email",
        "phone",
        "biography",
        mode="before",
    )(blank_to_none)

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in (
            "level",
            "full_name",
            "role_title",
            "sort_order",
            "status",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self

    @model_validator(mode="after")
    def check_term_range(self):
        # Sólo comprueba el par cuando el parche trae las dos fechas; si llega
        # una sola, la ruta la contrasta con la que ya está guardada.
        if {"term_start_date", "term_end_date"} <= self.model_fields_set:
            ensure_term_range(self.term_start_date, self.term_end_date)
        return self
