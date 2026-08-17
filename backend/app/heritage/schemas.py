from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HeritageKind = Literal[
    "building",
    "archaeological",
    "natural",
    "movable",
    "intangible",
    "other",
]
ProtectionLevel = Literal["none", "local", "regional", "bic", "unesco"]
ConservationState = Literal["good", "fair", "poor", "ruin", "unknown"]
ArchiveKind = Literal[
    "document",
    "photograph",
    "map",
    "book",
    "audio",
    "video",
    "other",
]
DigitisationState = Literal["not_digitised", "in_progress", "digitised"]

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MIN_YEAR = 1
MAX_YEAR = 2200


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class HeritageAssetRead(BaseModel):
    id: int
    organization_id: int
    slug: str
    name: str
    kind: HeritageKind
    period: str | None
    description: str | None
    protection_level: ProtectionLevel
    protection_reference: str | None
    conservation_state: ConservationState
    last_survey_date: date | None
    location_id: int | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HeritageAssetCreate(BaseModel):
    organization_id: int
    slug: str = Field(min_length=1, max_length=140, pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=255)
    kind: HeritageKind = "building"
    # Texto libre: «siglo XVI», «finales del XIX», «indeterminada». Forzar un
    # año sería inventar precisión que la fuente no tiene.
    period: str | None = Field(default=None, max_length=120)
    description: str | None = None
    protection_level: ProtectionLevel = "none"
    protection_reference: str | None = Field(default=None, max_length=255)
    conservation_state: ConservationState = "unknown"
    last_survey_date: date | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "period",
        "description",
        "protection_reference",
        "notes",
        mode="before",
    )(blank_to_none)

    @model_validator(mode="after")
    def check_protection(self):
        if self.protection_level != "none" and self.protection_reference is None:
            raise ValueError(
                "A protected asset needs the reference of its declaration"
            )
        return self


class ArchiveItemRead(BaseModel):
    id: int
    organization_id: int
    reference: str
    title: str
    kind: ArchiveKind
    description: str | None
    start_year: int | None
    end_year: int | None
    physical_location: str | None
    conservation_state: ConservationState
    digitisation_state: DigitisationState
    document_id: int | None
    heritage_asset_id: int | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ArchiveItemCreate(BaseModel):
    organization_id: int
    reference: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    kind: ArchiveKind = "document"
    description: str | None = None
    # Años sueltos: de una caja se conoce el periodo, casi nunca el día.
    start_year: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    end_year: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    physical_location: str | None = Field(default=None, max_length=255)
    conservation_state: ConservationState = "unknown"
    digitisation_state: DigitisationState = "not_digitised"
    document_id: int | None = None
    heritage_asset_id: int | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "description",
        "physical_location",
        "notes",
        mode="before",
    )(blank_to_none)

    @model_validator(mode="after")
    def check_years_and_digitisation(self):
        if (
            self.start_year is not None
            and self.end_year is not None
            and self.end_year < self.start_year
        ):
            raise ValueError("end_year cannot precede start_year")
        # Digitalizado significa que el fichero existe; si no, es una promesa.
        if self.digitisation_state == "digitised" and self.document_id is None:
            raise ValueError("A digitised item needs its document")
        return self
