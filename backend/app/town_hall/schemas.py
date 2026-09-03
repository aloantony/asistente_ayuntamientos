from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

# Cuatro niveles, los mismos que el diseño de referencia: la pestaña
# (`nav_section`) es la entrada de la fila, el epígrafe (`epigraph`) es la
# tarjeta plegable que cuelga de ella, el apartado (`nav_item`) es la pestaña
# interna del epígrafe y el elemento (`item`) lleva el contenido.
# `section` sigue reservado para más adelante. Ver ADR-054.
CreatableBlockType = Literal["nav_section", "epigraph", "nav_item", "item"]
BlockStatus = Literal["active", "archived"]

# Cómo se presenta el contenido de un apartado. `text` es la prosa por defecto;
# `contacts` son listas de nombre y número, como los teléfonos del prototipo;
# `data` son pares dato/valor en rejilla, como la ficha general del municipio;
# `series` son series temporales que se dibujan (padrón, clima, análisis de agua):
# cada elemento es una serie, su `body` es la unidad y sus puntos van en `points`.
# Las series se agrupan por unidad al pintarlas, nunca en dos ejes verticales.
# `people` son personas con cargo, partido y los campos que añada el usuario,
# como la corporación y la estructura de gobierno. En `text` y `contacts` el
# elemento guarda la etiqueta en `title` y el valor en `body`; en `people` el
# nombre va en `title` y los campos en `fields`.
SectionLayout = Literal["text", "contacts", "people", "files", "data", "series"]
SECTION_LAYOUTS: tuple[str, ...] = get_args(SectionLayout)

# Cota de adjuntos por elemento, por la misma razón que la de campos.
MAX_ITEM_ATTACHMENTS = 30

# Una serie histórica larga (padrón desde 1842) cabe de sobra en 300 puntos.
MAX_SERIES_POINTS = 300

# Cota de los campos libres por persona: evita que un `data_json` crezca sin
# medida desde el formulario.
MAX_ITEM_FIELDS = 20

# Qué puede colgar de qué. El elemento es hoja: no admite hijos.
BLOCK_PARENT_TYPES: dict[str, str | None] = {
    "nav_section": None,
    "epigraph": "nav_section",
    "nav_item": "epigraph",
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


class MunicipalNavEpigraphRead(BaseModel):
    """La tarjeta plegable del diseño: título, y dentro sus apartados."""

    id: int
    title: str
    position: int
    # Módulo propio que pinta esta tarjeta, si el producto ya tiene uno para
    # ella (la corporación municipal, por ejemplo). Sin módulo la tarjeta
    # enseña el contenido genérico de sus apartados.
    module: str | None = None
    items: list[MunicipalNavItemRead] = []


class MunicipalNavSectionRead(BaseModel):
    id: int
    title: str
    position: int
    epigraphs: list[MunicipalNavEpigraphRead] = []


class TownHallRead(BaseModel):
    organization_id: int
    organization_name: str
    profile: MunicipalProfileRead
    nav: list[MunicipalNavSectionRead] = []


class TownHallStructureSeedResult(BaseModel):
    organization_id: int
    # Claves de lo creado en esta llamada; vacía si ya estaba todo.
    created: list[str] = []


class MunicipalContentField(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(default="", max_length=2000)

    model_config = ConfigDict(str_strip_whitespace=True)


class MunicipalSeriesPoint(BaseModel):
    # La abscisa es una etiqueta (un año, un mes), no un número: así vale para
    # el padrón y para los doce meses del clima sin dos modelos distintos.
    x: str = Field(min_length=1, max_length=20)
    y: float

    model_config = ConfigDict(str_strip_whitespace=True)


class MunicipalAttachmentRead(BaseModel):
    index: int
    name: str
    content_type: str
    size_bytes: int


class MunicipalContentItemRead(BaseModel):
    id: int
    title: str
    body: str | None
    position: int
    fields: list[MunicipalContentField] = []
    attachments: list[MunicipalAttachmentRead] = []
    points: list[MunicipalSeriesPoint] = []


class MunicipalContentRead(BaseModel):
    block_id: int
    title: str
    parent_title: str | None = None
    layout: SectionLayout = "text"
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
    # El formato solo lo llevan los apartados.
    layout: SectionLayout | None = None
    # Los campos libres solo los llevan los elementos.
    fields: list[MunicipalContentField] | None = Field(
        default=None,
        max_length=MAX_ITEM_FIELDS,
    )
    # Los puntos de una serie, también solo en elementos.
    points: list[MunicipalSeriesPoint] | None = Field(
        default=None,
        max_length=MAX_SERIES_POINTS,
    )
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
