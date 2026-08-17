from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SessionKind = Literal["ordinary", "extraordinary", "urgent", "constitutive"]
SessionStatus = Literal["convened", "held", "cancelled"]
MinutesStatus = Literal["pending", "draft", "approved"]


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class AgendaItemRead(BaseModel):
    id: int
    session_id: int
    organization_id: int
    position: int
    title: str
    description: str | None
    votes_in_favour: int | None
    votes_against: int | None
    abstentions: int | None
    outcome: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AgendaItemCreate(BaseModel):
    position: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    # Nulos mientras no se vota: un punto informativo no tiene votación, y cero
    # votos a favor no es lo mismo que no haberse votado.
    votes_in_favour: int | None = Field(default=None, ge=0)
    votes_against: int | None = Field(default=None, ge=0)
    abstentions: int | None = Field(default=None, ge=0)
    outcome: str | None = Field(default=None, max_length=120)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("description", "outcome", mode="before")(
        blank_to_none
    )


class SessionRead(BaseModel):
    id: int
    organization_id: int
    kind: SessionKind
    status: SessionStatus
    held_on: date
    summary: str | None
    minutes_status: MinutesStatus
    minutes_document_id: int | None
    publish_to_sede: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SessionDetail(SessionRead):
    agenda_items: list[AgendaItemRead]


class SessionCreate(BaseModel):
    organization_id: int
    kind: SessionKind = "ordinary"
    status: SessionStatus = "convened"
    held_on: date
    summary: str | None = None
    publish_to_sede: bool = False

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("summary", mode="before")(blank_to_none)


class MinutesUpdate(BaseModel):
    """Estado del acta. Aprobarla exige que exista el documento."""

    minutes_status: MinutesStatus
    minutes_document_id: int | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_document(self):
        if self.minutes_status == "approved" and self.minutes_document_id is None:
            raise ValueError("An approved minutes record needs its document")
        return self
