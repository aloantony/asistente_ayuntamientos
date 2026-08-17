from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.geo.schemas import GeoLocationRead

AssetTaxonomyStatus = Literal["active", "archived"]
AssetStatus = Literal["active", "inactive", "retired", "archived"]
AssetConditionStatus = Literal["good", "fair", "poor", "unknown"]

CODE_PATTERN = r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$"
COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


def normalize_code(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    return value


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class AssetCategoryRead(BaseModel):
    id: int
    organization_id: int
    code: str
    name: str
    description: str | None
    color: str | None
    sort_order: int
    status: AssetTaxonomyStatus
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AssetCategoryCreate(BaseModel):
    organization_id: int
    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    sort_order: int = Field(default=0, ge=0)
    status: AssetTaxonomyStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True)

    _normalize_code = field_validator("code", mode="before")(normalize_code)
    _blank_optional_text = field_validator(
        "description", "color", mode="before"
    )(blank_to_none)


class AssetCategoryUpdate(BaseModel):
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=CODE_PATTERN,
    )
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    sort_order: int | None = Field(default=None, ge=0)
    status: AssetTaxonomyStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    _normalize_code = field_validator("code", mode="before")(normalize_code)
    _blank_optional_text = field_validator(
        "description", "color", mode="before"
    )(blank_to_none)

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in ("code", "name", "sort_order", "status"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class AssetTypeRead(BaseModel):
    id: int
    organization_id: int
    category_id: int
    code: str
    name: str
    description: str | None
    sort_order: int
    status: AssetTaxonomyStatus
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime
    category: AssetCategoryRead

    model_config = ConfigDict(from_attributes=True)


class AssetTypeCreate(BaseModel):
    organization_id: int
    category_id: int
    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    sort_order: int = Field(default=0, ge=0)
    status: AssetTaxonomyStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True)

    _normalize_code = field_validator("code", mode="before")(normalize_code)
    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )


class AssetTypeUpdate(BaseModel):
    category_id: int | None = None
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=CODE_PATTERN,
    )
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    sort_order: int | None = Field(default=None, ge=0)
    status: AssetTaxonomyStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    _normalize_code = field_validator("code", mode="before")(normalize_code)
    _blank_optional_text = field_validator("description", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in (
            "category_id",
            "code",
            "name",
            "sort_order",
            "status",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MunicipalAssetRead(BaseModel):
    id: int
    organization_id: int
    municipality_id: int
    asset_type_id: int
    location_id: int | None
    code: str | None
    name: str
    description: str | None
    status: AssetStatus
    condition_status: AssetConditionStatus
    material: str | None
    dimensions: str | None
    installed_on: date | None
    last_inspected_on: date | None
    notes: str | None
    created_by_id: int | None
    updated_by_id: int | None
    created_at: datetime
    updated_at: datetime
    asset_type: AssetTypeRead
    location: GeoLocationRead | None

    model_config = ConfigDict(from_attributes=True)


class MunicipalAssetCreate(BaseModel):
    organization_id: int
    asset_type_id: int
    code: str | None = Field(default=None, min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: AssetStatus = "active"
    condition_status: AssetConditionStatus = "unknown"
    material: str | None = Field(default=None, max_length=255)
    dimensions: str | None = Field(default=None, max_length=500)
    installed_on: date | None = None
    last_inspected_on: date | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "code",
        "description",
        "material",
        "dimensions",
        "notes",
        mode="before",
    )(blank_to_none)


class MunicipalAssetUpdate(BaseModel):
    asset_type_id: int | None = None
    code: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: AssetStatus | None = None
    condition_status: AssetConditionStatus | None = None
    material: str | None = Field(default=None, max_length=255)
    dimensions: str | None = Field(default=None, max_length=500)
    installed_on: date | None = None
    last_inspected_on: date | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "code",
        "description",
        "material",
        "dimensions",
        "notes",
        mode="before",
    )(blank_to_none)

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field_name in ("asset_type_id", "name", "status", "condition_status"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class AssetTaxonomySeedRequest(BaseModel):
    organization_id: int

    model_config = ConfigDict(extra="forbid")
