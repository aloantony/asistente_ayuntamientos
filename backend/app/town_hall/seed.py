"""Estructura de partida del Ayuntamiento.

Un ayuntamiento recién dado de alta abre la pantalla «Ayuntamiento» y encuentra
una fila de pestañas vacía: sabe que puede crear apartados, pero no cuáles. El
diseño de referencia sí lo sabe, así que la estructura de partida se toma de él
y no de una invención nuestra.

Las pestañas, los epígrafes y los apartados salen del proyecto exportado de
Claude Design (`design/exports/Fuentelcesped - Pantalla Principal
(standalone).html`): la fila de pestañas es su `aySecTabs`, el orden por defecto
de los epígrafes es su `infoDefault`, sus títulos son `infoDefTitles` y los
apartados de cada uno son sus pestañas internas. Ver
`docs/diseno-ayuntamiento-prototipo.md` §8.

Los cuatro niveles del diseño —pestaña → epígrafe → pestaña interna → contenido—
son ahora los cuatro del modelo: `nav_section` → `epigraph` → `nav_item` →
`item` (ADR-054). ADR-053 los había colapsado en tres, repartiendo los epígrafes
en pestañas hermanas; se revierte porque alejaba la pantalla del diseño, que es
lo que este seed existe para reproducir.

De las cuatro pestañas del diseño sólo se siembra **Información**: las otras
tres —Administración, Personal y Mapa general— tienen módulos propios en la
aplicación y sembrarlas como bloques vacíos bifurcaría el dominio, igual que
pasaría con Normativa.

**Normativa municipal queda fuera a propósito.** Es el único epígrafe del diseño
que ya está construido en otro sitio: la biblioteca de ordenanzas con su búsqueda
semántica y el área fija de Normativa. Sembrarlo aquí duplicaría el dominio (ver
la fase B6 del plan).

El seed es **por organización y bajo petición**, como el del inventario
(`app/assets/seed.py`): la estructura es un punto de partida que cada
ayuntamiento adapta, no un catálogo del producto. El contenido inicial sólo se
añade cuando está asociado de forma explícita al código INE del municipio; así
los datos de Fuentelcésped nunca aparecen en otro ayuntamiento.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.municipalities.models import Municipality
from app.organizations.models import Organization
from app.town_hall.models import MunicipalBlock

# La pestaña «Información», con sus epígrafes y los apartados de cada uno. Los
# epígrafes son `infoDefault` del proyecto exportado, en su orden, y sus títulos
# `infoDefTitles`; los apartados son las pestañas internas de cada epígrafe. El
# formato de cada apartado es el que pide su contenido en el diseño; el
# ayuntamiento puede cambiarlo después.
INITIAL_TOWN_HALL_STRUCTURE: tuple[dict, ...] = (
    {
        "key": "informacion",
        "title": "Información",
        "epigraphs": (
            {
                "key": "estructura",
                "title": "Estructura de Gobierno",
                "sections": (
                    ("corporacion", "Corporación Municipal", "people"),
                    ("diputacion", "Diputación provincial", "people"),
                    ("autonomica", "Administración autonómica", "people"),
                    ("estado", "Administración del Estado", "people"),
                ),
            },
            {
                "key": "datos",
                "title": "Datos del municipio",
                "sections": (
                    ("general", "Información general", "data"),
                    ("demografia", "Datos demográficos", "series"),
                    ("clima", "Registro climatológico", "series"),
                    ("agua", "Análisis de agua potable", "files"),
                    ("patrimonio", "Patrimonio", "data"),
                ),
            },
            {
                "key": "suministros",
                "title": "Suministros",
                "sections": (("suministros", "Suministros", "series"),),
            },
            {
                "key": "archivo",
                "title": "Archivo municipal",
                "sections": (
                    ("archivo", "Archivo", "files"),
                    ("fototeca", "Fototeca", "files"),
                    ("cronicas", "Crónicas", "text"),
                    ("himno", "Himno", "text"),
                ),
            },
            {
                "key": "telefonos",
                "title": "Teléfonos de interés",
                "sections": (
                    ("servicios", "Servicios e instituciones", "contacts"),
                    ("equipo", "Equipo de gobierno", "contacts"),
                    ("personal", "Personal municipal", "contacts"),
                ),
            },
            {
                "key": "organismos",
                "title": "Organismos y empresas",
                "sections": (("organismos", "Organismos y empresas", "text"),),
            },
        ),
    },
)


# Epígrafes que el producto ya sabe pintar con un módulo propio. La clave es la
# del seed, no el título: el ayuntamiento puede renombrar la tarjeta y el enlace
# con su módulo tiene que sobrevivir al cambio.
EPIGRAPH_MODULES: dict[str, str] = {
    "informacion/estructura": "government",
}


# Contenido de partida verificado para municipios concretos. La clave INE evita
# que una ficha de cliente se copie por accidente a cualquier otra organización.
# Cada elemento conserva una marca de seed propia: editarlo, renombrarlo o
# archivarlo impide que una segunda llamada lo sobrescriba o lo duplique.
INITIAL_TOWN_HALL_CONTENT_BY_INE_CODE: dict[
    str,
    dict[str, tuple[tuple[str, str, str], ...]],
] = {
    "09137": {
        "informacion/datos/general": (
            ("superficie", "Superficie", "22 kilómetros cuadrados"),
            (
                "distancia-burgos",
                "Distancia a Burgos por carretera",
                "93 kilómetros",
            ),
            ("comarca", "Comarca", "Ribera del Duero"),
            ("partido-judicial", "Partido judicial", "Aranda de Duero"),
            (
                "altitud",
                "Altitud",
                "926 metros sobre el nivel del mar",
            ),
        ),
    },
}


def read_seed_key(block: MunicipalBlock) -> str | None:
    """Marca del seed guardada en `data_json`, si la lleva."""
    if not block.data_json:
        return None

    try:
        payload = json.loads(block.data_json)
    except ValueError:
        return None

    if not isinstance(payload, dict):
        return None

    key = payload.get("seed")
    return key if isinstance(key, str) else None


def write_seed_data(block: MunicipalBlock, key: str, layout: str | None) -> None:
    """Escribe la marca del seed y, en los apartados, su formato.

    Convive con `write_layout` de las rutas: ambos conservan lo que ya hubiera
    en `data_json`, así que cambiar el formato de un apartado sembrado no borra
    su marca ni al revés.
    """
    payload: dict[str, object] = {}
    if block.data_json:
        try:
            loaded = json.loads(block.data_json)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded

    payload["seed"] = key
    if layout is not None:
        payload["layout"] = layout
    block.data_json = json.dumps(payload, ensure_ascii=False)


def _normalized(title: str) -> str:
    return " ".join(title.split()).casefold()


def _find_block(
    blocks: list[MunicipalBlock],
    *,
    seed_key: str,
    title: str,
    block_type: str,
    parent_id: int | None,
) -> MunicipalBlock | None:
    """Busca un bloque ya existente, primero por marca y luego por título.

    La marca sobrevive a un renombrado, que es el caso corriente: el
    ayuntamiento llama «Teléfonos» a lo que el seed creó como «Teléfonos de
    interés» y una segunda llamada no debe duplicarlo. El título cubre el caso
    contrario —lo creó alguien a mano antes de sembrar— y evita el duplicado
    igualmente.
    """
    candidates = [
        block
        for block in blocks
        if block.block_type == block_type and block.parent_id == parent_id
    ]

    for block in candidates:
        if read_seed_key(block) == seed_key:
            return block

    wanted = _normalized(title)
    for block in candidates:
        if read_seed_key(block) is None and _normalized(block.title) == wanted:
            return block

    return None


def ensure_initial_town_hall_structure(
    db: Session,
    organization_id: int,
    *,
    created_by_id: int | None = None,
) -> list[str]:
    """Crea la estructura que falte en una organización y devuelve sus claves.

    Es idempotente y no destructivo, como el seed del inventario: una pestaña o
    un apartado que ya existe se deja como esté —renombrado, archivado o con
    otro formato— y sólo se crea lo que falta. Nunca se reescribe lo que el
    ayuntamiento haya decidido.
    """
    created: list[str] = []
    ine_code = db.scalar(
        select(Municipality.ine_code)
        .join(Organization, Organization.municipality_id == Municipality.id)
        .where(Organization.id == organization_id)
    )
    initial_content = INITIAL_TOWN_HALL_CONTENT_BY_INE_CODE.get(
        ine_code or "",
        {},
    )

    # Se cargan de una vez, archivados incluidos: un apartado que el
    # ayuntamiento archivó no debe volver a aparecer en la siguiente llamada.
    blocks = list(
        db.scalars(
            select(MunicipalBlock).where(
                MunicipalBlock.organization_id == organization_id
            )
        )
    )

    def next_position(parent_id: int | None) -> int:
        siblings = [block for block in blocks if block.parent_id == parent_id]
        return max((block.position for block in siblings), default=-1) + 1

    def ensure(
        *,
        seed_key: str,
        title: str,
        block_type: str,
        parent: MunicipalBlock | None,
        layout: str | None = None,
        body: str | None = None,
    ) -> MunicipalBlock:
        """Devuelve el bloque, creándolo sólo si falta."""
        parent_id = parent.id if parent is not None else None
        existing = _find_block(
            blocks,
            seed_key=seed_key,
            title=title,
            block_type=block_type,
            parent_id=parent_id,
        )
        if existing is not None:
            return existing

        block = MunicipalBlock(
            organization_id=organization_id,
            parent_id=parent_id,
            block_type=block_type,
            title=title,
            body=body,
            position=next_position(parent_id),
            created_by_id=created_by_id,
            updated_by_id=created_by_id,
        )
        write_seed_data(block, seed_key, layout)
        db.add(block)
        db.flush()
        blocks.append(block)
        created.append(seed_key)
        return block

    for tab in INITIAL_TOWN_HALL_STRUCTURE:
        tab_key = str(tab["key"])
        tab_block = ensure(
            seed_key=tab_key,
            title=str(tab["title"]),
            block_type="nav_section",
            parent=None,
        )

        for epigraph in tab["epigraphs"]:
            epigraph_key = f"{tab_key}/{epigraph['key']}"
            epigraph_block = ensure(
                seed_key=epigraph_key,
                title=str(epigraph["title"]),
                block_type="epigraph",
                parent=tab_block,
            )

            for item_key, item_title, layout in epigraph["sections"]:
                section_key = f"{epigraph_key}/{item_key}"
                section_block = ensure(
                    seed_key=section_key,
                    title=item_title,
                    block_type="nav_item",
                    parent=epigraph_block,
                    layout=layout,
                )
                for content_key, content_title, content_body in initial_content.get(
                    section_key,
                    (),
                ):
                    ensure(
                        seed_key=f"{section_key}/{content_key}",
                        title=content_title,
                        block_type="item",
                        parent=section_block,
                        body=content_body,
                    )

    if created:
        db.commit()
    return created
