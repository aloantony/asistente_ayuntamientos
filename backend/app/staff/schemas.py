from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.staff.models import STAFF_SCHEDULE_DAYS

StaffPostKind = Literal["post", "container"]
StaffWorkerStatus = Literal["active", "vacation", "leave", "archived"]
StaffAbsenceType = Literal["vacation", "personal", "sick_leave", "other"]
StaffReportType = Literal["diary", "report"]
StaffContractType = Literal["permanent", "temporary", "interim", "external", "other"]
StaffHistoryEventType = Literal[
    "created",
    "updated",
    "status_changed",
    "post_changed",
    "archived",
]
StaffScheduleDay = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def order_schedule_days(value: object) -> object:
    """Deduplica los días y los deja en orden de semana, no de escritura.

    La ficha siempre muestra la semana de lunes a domingo, así que el orden en
    que se envían los días no es información: normalizarlo aquí evita que dos
    peticiones equivalentes produzcan filas distintas.
    """
    if not isinstance(value, list) or not all(
        isinstance(day, str) for day in value
    ):
        # Lo que no sea una lista de cadenas lo rechaza después la validación
        # del literal, con un 422 que explica el campo.
        return value
    return sorted(dict.fromkeys(value), key=_schedule_day_index)


def _schedule_day_index(day: str) -> int:
    if day in STAFF_SCHEDULE_DAYS:
        return STAFF_SCHEDULE_DAYS.index(day)
    return len(STAFF_SCHEDULE_DAYS)


def ensure_date_range(start: date | None, end: date | None, field_name: str) -> None:
    if start is not None and end is not None and end < start:
        raise ValueError(f"{field_name} cannot be earlier than its start date")


class StaffPostRead(BaseModel):
    id: int
    organization_id: int
    parent_id: int | None
    kind: StaffPostKind
    label: str
    description: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StaffPostCreate(BaseModel):
    organization_id: int
    parent_id: int | None = None
    kind: StaffPostKind = "post"
    label: str = Field(min_length=1, max_length=255)
    description: str | None = None
    sort_order: int = Field(default=0, ge=0)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )


class StaffPostUpdate(BaseModel):
    parent_id: int | None = None
    kind: StaffPostKind | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    sort_order: int | None = Field(default=None, ge=0)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in ("kind", "label", "sort_order"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class StaffWorkerRead(BaseModel):
    id: int
    organization_id: int
    post_id: int | None
    full_name: str
    email: str | None
    phone: str | None
    description: str | None
    status: StaffWorkerStatus
    schedule_summary: str | None
    schedule_days: list[StaffScheduleDay]
    weekly_hours: Decimal | None
    contract_type: StaffContractType | None
    contract_start_date: date | None
    contract_end_date: date | None
    vacation_days_limit: int | None
    personal_days_limit: int | None
    bills_invoices: bool
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime
    post: StaffPostRead | None

    model_config = ConfigDict(from_attributes=True)


class StaffWorkerCreate(BaseModel):
    organization_id: int
    post_id: int | None = None
    full_name: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    description: str | None = None
    status: StaffWorkerStatus = "active"
    schedule_summary: str | None = Field(default=None, max_length=255)
    schedule_days: list[StaffScheduleDay] = Field(default_factory=list)
    weekly_hours: Decimal | None = Field(default=None, ge=0, max_digits=5, decimal_places=2)
    contract_type: StaffContractType | None = None
    contract_start_date: date | None = None
    contract_end_date: date | None = None
    vacation_days_limit: int | None = Field(default=None, ge=0)
    personal_days_limit: int | None = Field(default=None, ge=0)
    bills_invoices: bool = False

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "email",
        "phone",
        "description",
        "schedule_summary",
        mode="before",
    )(blank_to_none)
    _order_schedule_days = field_validator("schedule_days", mode="before")(
        order_schedule_days
    )

    @model_validator(mode="after")
    def check_contract_range(self):
        ensure_date_range(
            self.contract_start_date,
            self.contract_end_date,
            "contract_end_date",
        )
        return self


class StaffWorkerUpdate(BaseModel):
    post_id: int | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    description: str | None = None
    status: StaffWorkerStatus | None = None
    schedule_summary: str | None = Field(default=None, max_length=255)
    schedule_days: list[StaffScheduleDay] | None = None
    weekly_hours: Decimal | None = Field(default=None, ge=0, max_digits=5, decimal_places=2)
    contract_type: StaffContractType | None = None
    contract_start_date: date | None = None
    contract_end_date: date | None = None
    vacation_days_limit: int | None = Field(default=None, ge=0)
    personal_days_limit: int | None = Field(default=None, ge=0)
    bills_invoices: bool | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "email",
        "phone",
        "description",
        "schedule_summary",
        mode="before",
    )(blank_to_none)
    _order_schedule_days = field_validator("schedule_days", mode="before")(
        order_schedule_days
    )

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in (
            "full_name",
            "status",
            "schedule_days",
            "bills_invoices",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self

    @model_validator(mode="after")
    def check_contract_range(self):
        if {"contract_start_date", "contract_end_date"} <= self.model_fields_set:
            ensure_date_range(
                self.contract_start_date,
                self.contract_end_date,
                "contract_end_date",
            )
        return self


class StaffAbsenceRead(BaseModel):
    id: int
    worker_id: int
    organization_id: int
    absence_type: StaffAbsenceType
    start_date: date
    end_date: date
    reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StaffAbsenceCreate(BaseModel):
    absence_type: StaffAbsenceType
    start_date: date
    end_date: date
    reason: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("reason", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def check_range(self):
        ensure_date_range(self.start_date, self.end_date, "end_date")
        return self


class StaffReportRead(BaseModel):
    id: int
    worker_id: int
    organization_id: int
    report_type: StaffReportType
    report_date: date
    plan: str | None
    closing: str | None
    incident: str | None
    author_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StaffReportCreate(BaseModel):
    report_type: StaffReportType = "diary"
    report_date: date
    plan: str | None = None
    closing: str | None = None
    incident: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "plan",
        "closing",
        "incident",
        mode="before",
    )(blank_to_none)

    @model_validator(mode="after")
    def require_some_content(self):
        # Una entrada de diario sin plan, cierre ni incidencia no dice nada;
        # aceptarla sólo ensuciaría el histórico del puesto.
        if self.plan is None and self.closing is None and self.incident is None:
            raise ValueError("report must include plan, closing or incident")
        return self


class StaffInvoiceRead(BaseModel):
    id: int
    worker_id: int
    organization_id: int
    issued_on: date
    concept: str
    hours: Decimal | None
    amount: Decimal | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StaffInvoiceCreate(BaseModel):
    issued_on: date
    concept: str = Field(min_length=1, max_length=255)
    hours: Decimal | None = Field(default=None, ge=0, max_digits=7, decimal_places=2)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class StaffHistoryEventRead(BaseModel):
    id: int
    worker_id: int
    organization_id: int
    event_type: StaffHistoryEventType
    from_status: StaffWorkerStatus | None
    to_status: StaffWorkerStatus | None
    changed_fields: list[str]
    note: str | None
    actor_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
