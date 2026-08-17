from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MunicipalDataSource = Literal["municipal", "ine", "aemet", "other"]
WaterTreatment = Literal["none", "chlorination", "filtration", "osmosis", "other"]
WaterMeterStatus = Literal["active", "inactive", "removed"]

MIN_YEAR = 1900
MAX_YEAR = 2200


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class PadronRecordRead(BaseModel):
    id: int
    organization_id: int
    reference_year: int
    population: int
    men: int | None
    women: int | None
    births: int | None
    deaths: int | None
    source: MunicipalDataSource
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PadronRecordCreate(BaseModel):
    organization_id: int
    reference_year: int = Field(ge=MIN_YEAR, le=MAX_YEAR)
    population: int = Field(ge=0)
    men: int | None = Field(default=None, ge=0)
    women: int | None = Field(default=None, ge=0)
    births: int | None = Field(default=None, ge=0)
    deaths: int | None = Field(default=None, ge=0)
    source: MunicipalDataSource = "municipal"
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("notes", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def check_breakdown(self):
        if (
            self.men is not None
            and self.women is not None
            and self.men + self.women > self.population
        ):
            raise ValueError("men + women cannot exceed population")
        return self


class ClimateRecordRead(BaseModel):
    id: int
    organization_id: int
    reference_year: int
    reference_month: int | None
    avg_temperature_c: Decimal | None
    min_temperature_c: Decimal | None
    max_temperature_c: Decimal | None
    precipitation_mm: Decimal | None
    source: MunicipalDataSource
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ClimateRecordCreate(BaseModel):
    organization_id: int
    reference_year: int = Field(ge=MIN_YEAR, le=MAX_YEAR)
    # Sin mes, la fila resume el año entero.
    reference_month: int | None = Field(default=None, ge=1, le=12)
    avg_temperature_c: Decimal | None = Field(
        default=None, max_digits=5, decimal_places=2
    )
    min_temperature_c: Decimal | None = Field(
        default=None, max_digits=5, decimal_places=2
    )
    max_temperature_c: Decimal | None = Field(
        default=None, max_digits=5, decimal_places=2
    )
    precipitation_mm: Decimal | None = Field(
        default=None, ge=0, max_digits=7, decimal_places=2
    )
    source: MunicipalDataSource = "aemet"
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("notes", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def check_temperature_range(self):
        if (
            self.min_temperature_c is not None
            and self.max_temperature_c is not None
            and self.min_temperature_c > self.max_temperature_c
        ):
            raise ValueError(
                "min_temperature_c cannot be greater than max_temperature_c"
            )
        return self


class HouseholdStatRead(BaseModel):
    id: int
    organization_id: int
    reference_year: int
    total_dwellings: int
    primary_dwellings: int | None
    secondary_dwellings: int | None
    empty_dwellings: int | None
    source: MunicipalDataSource
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HouseholdStatCreate(BaseModel):
    organization_id: int
    reference_year: int = Field(ge=MIN_YEAR, le=MAX_YEAR)
    total_dwellings: int = Field(ge=0)
    primary_dwellings: int | None = Field(default=None, ge=0)
    secondary_dwellings: int | None = Field(default=None, ge=0)
    empty_dwellings: int | None = Field(default=None, ge=0)
    source: MunicipalDataSource = "ine"
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("notes", mode="before")(blank_to_none)


class UtilitySupplyRead(BaseModel):
    id: int
    organization_id: int
    name: str
    origin: str | None
    treatment: WaterTreatment
    last_analysis_date: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UtilitySupplyCreate(BaseModel):
    organization_id: int
    name: str = Field(min_length=1, max_length=255)
    origin: str | None = Field(default=None, max_length=255)
    treatment: WaterTreatment = "none"
    last_analysis_date: date | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("origin", "notes", mode="before")(
        blank_to_none
    )


class WaterMeterRead(BaseModel):
    id: int
    organization_id: int
    municipality_id: int
    supply_id: int | None
    location_id: int | None
    code: str
    address: str | None
    status: WaterMeterStatus
    installed_on: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WaterMeterCreate(BaseModel):
    organization_id: int
    supply_id: int | None = None
    code: str = Field(min_length=1, max_length=100)
    address: str | None = Field(default=None, max_length=255)
    status: WaterMeterStatus = "active"
    installed_on: date | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("address", "notes", mode="before")(
        blank_to_none
    )


class WaterMeterReadingRead(BaseModel):
    id: int
    meter_id: int
    organization_id: int
    read_on: date
    reading_m3: Decimal
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WaterMeterReadingCreate(BaseModel):
    read_on: date
    reading_m3: Decimal = Field(ge=0, max_digits=12, decimal_places=3)
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("notes", mode="before")(blank_to_none)
