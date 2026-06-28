from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GeoEntityType = Literal["requirement", "project"]
GeoLocationRole = Literal["primary", "affected_area", "reference"]
GeoLocationSource = Literal[
    "user_provided",
    "assistant_extracted",
    "geocoded",
    "imported",
    "manual_review",
]
GeoReviewStatus = Literal["draft", "proposed", "reviewed", "rejected"]


class GeoLocationRead(BaseModel):
    id: int
    organization_id: int | None
    municipality_id: int | None
    label: str
    geometry_type: Literal["point", "line", "polygon"]
    geometry_json: str
    latitude: float | None
    longitude: float | None
    address_text: str | None
    place_name: str | None
    cadastral_reference: str | None
    source: GeoLocationSource
    confidence: float | None
    review_status: GeoReviewStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GeoLocationCreate(BaseModel):
    organization_id: int | None = None
    municipality_id: int | None = None
    label: str = Field(min_length=1, max_length=255)
    latitude: float
    longitude: float
    address_text: str | None = Field(default=None, max_length=500)
    place_name: str | None = Field(default=None, max_length=255)
    cadastral_reference: str | None = Field(default=None, max_length=100)
    source: GeoLocationSource = "user_provided"
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: GeoReviewStatus = "proposed"

    model_config = ConfigDict(str_strip_whitespace=True)


class EntityLocationCreate(BaseModel):
    entity_type: GeoEntityType
    entity_id: int
    role: GeoLocationRole = "primary"
    location: GeoLocationCreate


class GeoMapFilters(BaseModel):
    entity_type: GeoEntityType | None = None
    organization_id: int | None = None
    status: str | None = None
    include_archived: bool = False
    limit: int = Field(default=500, ge=1, le=500)


class GeoMapItem(BaseModel):
    entity_type: GeoEntityType
    entity_id: int
    title: str
    subtitle: str | None = None
    status: str
    priority: str | None = None
    organization_id: int
    organization_name: str
    detail_path: str
    location: GeoLocationRead
