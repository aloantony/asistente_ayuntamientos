from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Tres niveles: el epígrafe es la entrada de la píldora (`nav_section`), el
# apartado cuelga de él (`nav_item`) y el elemento lleva el contenido (`item`).
# `epigraph` y `section` siguen reservados para más adelante.
CreatableBlockType = Literal["nav_section", "nav_item", "item"]
BlockStatus = Literal["active", "archived"]

# Qué puede colgar de qué. El elemento es hoja: no admite hijos.
BLOCK_PARENT_TYPES: dict[str, str | None] = {
    "nav_section": None,
    "nav_item": "nav_section",
    "item": "nav_item",
}


class MunicipalProfileRead(BaseModel):
    display_name: str | None = None
    weather_enabled: bool = False
    weather_location: str | None = None
    has_shield: bool = False


class MunicipalProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    weather_enabled: bool | None = None
    weather_location: str | None = Field(default=None, max_length=255)

    model_config = ConfigDict(str_strip_whitespace=True)


class MunicipalWeatherRead(BaseModel):
    temperature_celsius: float
    location: str


class MunicipalNavItemRead(BaseModel):
    id: int
    title: str
    position: int


class MunicipalNavSectionRead(BaseModel):
    id: int
    title: str
    position: int
    items: list[MunicipalNavItemRead] = []


class TownHallRead(BaseModel):
    organization_id: int
    organization_name: str
    profile: MunicipalProfileRead
    nav: list[MunicipalNavSectionRead] = []


class MunicipalContentItemRead(BaseModel):
    id: int
    title: str
    body: str | None
    position: int

    model_config = ConfigDict(from_attributes=True)


class MunicipalContentRead(BaseModel):
    block_id: int
    title: str
    parent_title: str | None = None
    items: list[MunicipalContentItemRead] = []


class MunicipalBlockCreate(BaseModel):
    block_type: CreatableBlockType
    parent_id: int | None = None
    title: str = Field(min_length=1, max_length=255)

    model_config = ConfigDict(str_strip_whitespace=True)


class MunicipalBlockUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    # El cuerpo solo lo llevan los elementos; vacío se guarda como nulo.
    body: str | None = None
    status: BlockStatus | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class MunicipalBlockRead(BaseModel):
    id: int
    organization_id: int
    parent_id: int | None
    block_type: str
    title: str
    position: int
    status: str

    model_config = ConfigDict(from_attributes=True)


class MunicipalBlockPlacement(BaseModel):
    id: int
    parent_id: int | None = None
    position: int = Field(ge=0)


class MunicipalBlockReorder(BaseModel):
    placements: list[MunicipalBlockPlacement] = Field(min_length=1)
