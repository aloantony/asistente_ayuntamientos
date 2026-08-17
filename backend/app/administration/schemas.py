from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Weekday = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
ProcedureChannel = Literal["in_person", "online", "both"]
ProcedureStatus = Literal["active", "archived"]
LicenceKind = Literal["works", "opening", "occupancy", "environmental", "other"]
LicenceStatus = Literal[
    "requested",
    "in_review",
    "granted",
    "denied",
    "expired",
    "withdrawn",
]
ContractProcedure = Literal["minor", "open", "negotiated", "framework", "other"]
ContractStatus = Literal["draft", "published", "awarded", "executed", "cancelled"]
GrantStatus = Literal["open", "applied", "granted", "denied", "settled"]
TransparencyArea = Literal[
    "institutional",
    "regulatory",
    "economic",
    "contracts",
    "grants",
    "other",
]

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MINUTES_IN_DAY = 24 * 60


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class OfficeHourRead(BaseModel):
    id: int
    organization_id: int
    office_name: str
    weekday: Weekday
    opens_at: int
    closes_at: int
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OfficeHourCreate(BaseModel):
    organization_id: int
    office_name: str = Field(min_length=1, max_length=255)
    weekday: Weekday
    # Minutos desde medianoche; el cierre a las 24:00 es válido.
    opens_at: int = Field(ge=0, lt=MINUTES_IN_DAY)
    closes_at: int = Field(gt=0, le=MINUTES_IN_DAY)
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("notes", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def check_range(self):
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be later than opens_at")
        return self


class ProcedureRead(BaseModel):
    id: int
    organization_id: int
    slug: str
    name: str
    description: str | None
    channel: ProcedureChannel
    deadline_days: int | None
    fee_description: str | None
    publish_to_sede: bool
    status: ProcedureStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProcedureCreate(BaseModel):
    organization_id: int
    slug: str = Field(min_length=1, max_length=120, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    channel: ProcedureChannel = "in_person"
    deadline_days: int | None = Field(default=None, ge=0)
    fee_description: str | None = Field(default=None, max_length=255)
    publish_to_sede: bool = False
    status: ProcedureStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "description", "fee_description", mode="before"
    )(blank_to_none)


class LicenceRead(BaseModel):
    id: int
    organization_id: int
    reference: str
    kind: LicenceKind
    applicant: str
    address: str | None
    summary: str | None
    status: LicenceStatus
    requested_on: date
    resolved_on: date | None
    fee_amount: Decimal | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LicenceCreate(BaseModel):
    organization_id: int
    reference: str = Field(min_length=1, max_length=100)
    kind: LicenceKind
    applicant: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=255)
    summary: str | None = None
    status: LicenceStatus = "requested"
    requested_on: date
    resolved_on: date | None = None
    fee_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2
    )

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("address", "summary", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def check_resolution(self):
        resolved = self.status in {"granted", "denied"}
        if resolved and self.resolved_on is None:
            raise ValueError("A resolved licence needs resolved_on")
        if not resolved and self.resolved_on is not None:
            raise ValueError("Only granted or denied licences carry resolved_on")
        if self.resolved_on is not None and self.resolved_on < self.requested_on:
            raise ValueError("resolved_on cannot precede requested_on")
        return self


class ContractRead(BaseModel):
    id: int
    organization_id: int
    reference: str
    title: str
    description: str | None
    procedure_type: ContractProcedure
    status: ContractStatus
    base_amount: Decimal | None
    awarded_amount: Decimal | None
    awarded_to: str | None
    published_on: date | None
    awarded_on: date | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ContractCreate(BaseModel):
    organization_id: int
    reference: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    procedure_type: ContractProcedure = "minor"
    status: ContractStatus = "draft"
    base_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    awarded_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    awarded_to: str | None = Field(default=None, max_length=255)
    published_on: date | None = None
    awarded_on: date | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "description", "awarded_to", mode="before"
    )(blank_to_none)

    @model_validator(mode="after")
    def check_award(self):
        # Adjudicado sin adjudicatario ni importe no es información, es un hueco.
        if self.status == "awarded" and (
            self.awarded_to is None or self.awarded_amount is None
        ):
            raise ValueError("An awarded contract needs awarded_to and awarded_amount")
        return self


class GrantRead(BaseModel):
    id: int
    organization_id: int
    title: str
    funder: str | None
    description: str | None
    status: GrantStatus
    requested_amount: Decimal | None
    granted_amount: Decimal | None
    application_deadline: date | None
    resolved_on: date | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GrantCreate(BaseModel):
    organization_id: int
    title: str = Field(min_length=1, max_length=255)
    funder: str | None = Field(default=None, max_length=255)
    description: str | None = None
    status: GrantStatus = "open"
    requested_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    granted_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    application_deadline: date | None = None
    resolved_on: date | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("funder", "description", mode="before")(
        blank_to_none
    )


class TransparencyItemRead(BaseModel):
    id: int
    organization_id: int
    area: TransparencyArea
    title: str
    description: str | None
    reference_period: str | None
    published_on: date | None
    publish_to_sede: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TransparencyItemCreate(BaseModel):
    organization_id: int
    area: TransparencyArea
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    reference_period: str | None = Field(default=None, max_length=100)
    published_on: date | None = None
    publish_to_sede: bool = False

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "description", "reference_period", mode="before"
    )(blank_to_none)
