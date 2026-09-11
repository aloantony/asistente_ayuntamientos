from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GeoEntityType = Literal["requirement", "project", "asset"]
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
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    geometry: dict | None = None
    address_text: str | None = Field(default=None, max_length=500)
    place_name: str | None = Field(default=None, max_length=255)
    cadastral_reference: str | None = Field(default=None, max_length=100)
    source: GeoLocationSource = "user_provided"
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: GeoReviewStatus = "proposed"

    model_config = ConfigDict(str_strip_whitespace=True)

    @model_validator(mode="after")
    def normalize_geometry(self):
        from app.geo.geometry import validated_geometry
        if self.geometry is not None:
            _, self.geometry, self.latitude, self.longitude = validated_geometry(self.geometry)
        elif self.latitude is None or self.longitude is None:
            raise ValueError("A point needs latitude and longitude")
        return self

    @property
    def geometry_type(self) -> str:
        return {"Point": "point", "LineString": "line", "Polygon": "polygon"}.get((self.geometry or {}).get("type"), "point")


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
    role: GeoLocationRole
    layer_key: str
    layer_label: str
    layer_color: str
    item_type: str | None = None
    condition_status: str | None = None
    title: str
    subtitle: str | None = None
    status: str
    priority: str | None = None
    organization_id: int
    organization_name: str
    detail_path: str
    location: GeoLocationRead


class MapRegistrationCreate(BaseModel):
    request_key: str = Field(min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$")
    entity_type: GeoEntityType
    organization_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10000)
    asset_type_id: int | None = Field(default=None, gt=0)
    location: GeoLocationCreate
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
