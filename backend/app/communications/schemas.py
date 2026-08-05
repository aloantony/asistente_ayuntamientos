from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NewsStatus = Literal["draft", "published", "archived"]
NoticeKind = Literal["bando", "edicto", "convocatoria", "other"]
NoticeStatus = Literal["draft", "published", "withdrawn", "expired"]
NoticeEventType = Literal["created", "updated", "published", "withdrawn"]

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class NewsRead(BaseModel):
    id: int
    organization_id: int
    slug: str
    title: str
    summary: str | None
    body: str | None
    status: NewsStatus
    published_on: date | None
    publish_to_sede: bool
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NewsCreate(BaseModel):
    organization_id: int
    slug: str = Field(min_length=1, max_length=160, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    summary: str | None = Field(default=None, max_length=500)
    body: str | None = None
    status: NewsStatus = "draft"
    published_on: date | None = None
    publish_to_sede: bool = False

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("summary", "body", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def check_published_on(self):
        if (self.status == "published") != (self.published_on is not None):
            raise ValueError(
                "published_on is required exactly when the news is published"
            )
        return self


class NoticeEventRead(BaseModel):
    id: int
    notice_id: int
    organization_id: int
    event_type: NoticeEventType
    from_status: NoticeStatus | None
    to_status: NoticeStatus | None
    note: str | None
    actor_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NoticeRead(BaseModel):
    id: int
    organization_id: int
    kind: NoticeKind
    title: str
    body: str | None
    status: NoticeStatus
    published_on: date | None
    expires_on: date | None
    publish_to_sede: bool
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NoticeDetail(NoticeRead):
    events: list[NoticeEventRead]


class NoticeCreate(BaseModel):
    """Un bando nace en borrador; publicarlo es una transición, no un campo."""

    organization_id: int
    kind: NoticeKind = "bando"
    title: str = Field(min_length=1, max_length=255)
    body: str | None = None
    expires_on: date | None = None
    publish_to_sede: bool = False

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("body", mode="before")(blank_to_none)


class NoticePublish(BaseModel):
    published_on: date
    expires_on: date | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def check_period(self):
        if self.expires_on is not None and self.expires_on < self.published_on:
            raise ValueError("expires_on cannot precede published_on")
        return self


class NoticeWithdraw(BaseModel):
    # Retirar un bando expuesto exige decir por qué: puede tener que
    # justificarse después.
    reason: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
