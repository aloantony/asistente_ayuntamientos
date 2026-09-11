from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MunicipalityStatus = Literal["active", "archived"]
MunicipalityType = Literal["municipality", "minor_local_entity", "district", "other"]
RuralUrbanProfile = Literal["rural", "semi_rural", "urban", "mixed", "unknown"]

OPTIONAL_TEXT_FIELDS = (
    "ine_code",
    "postal_codes",
    "economic_profile",
    "tourism_profile",
    "geographic_notes",
    "administrative_notes",
)


class MunicipalitySummary(BaseModel):
    ine_code: str | None = None
    id: int
    name: str
    province: str
    autonomous_community: str

    model_config = ConfigDict(from_attributes=True)


class ReferenceDatasetVersionRead(BaseModel):
    dataset_key: str
    title: str
    version_label: str
    reference_date: date
    catalog_url: str
    download_url: str
    member_name: str
    archive_sha256: str
    content_sha256: str
    license_name: str
    license_url: str
    attribution: str
    retrieved_at: datetime
    national_row_count: int
    target_row_count: int

    model_config = ConfigDict(from_attributes=True)


class MunicipalityOfficialGeographyRead(BaseModel):
    source_municipality_code: str
    relationship_id: int
    geographic_code: str
    source_province_code: str
    source_province_name: str
    source_municipality_name: str
    source_population: int
    surface_km2: float
    perimeter_m: float
    capital_ine_code: str
    capital_name: str
    capital_population: int
    mtn25_sheet: str
    longitude: float
    latitude: float
    coordinate_origin: str
    altitude_m: float
    altitude_origin: str
    crs: str
    dataset_version: ReferenceDatasetVersionRead

    model_config = ConfigDict(from_attributes=True)


class MunicipalityRead(BaseModel):
    id: int
    name: str
    province: str
    autonomous_community: str
    country: str
    ine_code: str | None
    ine_check_digit: str | None
    directory_reference_date: date | None
    directory_source_url: str | None
    directory_source_sha256: str | None
    population: int | None
    population_reference_year: int | None
    population_source_url: str | None
    population_source_sha256: str | None
    surface_km2: float | None
    density: float | None
    postal_codes: str | None
    municipality_type: MunicipalityType
    rural_urban_profile: RuralUrbanProfile
    economic_profile: str | None
    tourism_profile: str | None
    geographic_notes: str | None
    administrative_notes: str | None
    status: MunicipalityStatus
    official_geography: MunicipalityOfficialGeographyRead | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MunicipalityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    province: str = Field(min_length=1, max_length=255)
    autonomous_community: str = Field(min_length=1, max_length=255)
    country: str = Field(default="España", min_length=1, max_length=100)
    ine_code: str | None = Field(default=None, max_length=20)
    population: int | None = Field(default=None, ge=0)
    surface_km2: float | None = Field(default=None, ge=0)
    density: float | None = Field(default=None, ge=0)
    postal_codes: str | None = None
    municipality_type: MunicipalityType = "municipality"
    rural_urban_profile: RuralUrbanProfile = "unknown"
    economic_profile: str | None = None
    tourism_profile: str | None = None
    geographic_notes: str | None = None
    administrative_notes: str | None = None
    status: MunicipalityStatus = "active"

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator(*OPTIONAL_TEXT_FIELDS, mode="before")
    @classmethod
    def blank_strings_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class MunicipalityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    province: str | None = Field(default=None, min_length=1, max_length=255)
    autonomous_community: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    country: str | None = Field(default=None, min_length=1, max_length=100)
    ine_code: str | None = Field(default=None, max_length=20)
    population: int | None = Field(default=None, ge=0)
    surface_km2: float | None = Field(default=None, ge=0)
    density: float | None = Field(default=None, ge=0)
    postal_codes: str | None = None
    municipality_type: MunicipalityType | None = None
    rural_urban_profile: RuralUrbanProfile | None = None
    economic_profile: str | None = None
    tourism_profile: str | None = None
    geographic_notes: str | None = None
    administrative_notes: str | None = None
    status: MunicipalityStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator(*OPTIONAL_TEXT_FIELDS, mode="before")
    @classmethod
    def blank_strings_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value
