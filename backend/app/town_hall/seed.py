"""Estructura de partida del Ayuntamiento.

Un ayuntamiento recién dado de alta abre la pantalla «Ayuntamiento» y encuentra
una fila de pestañas vacía: sabe que puede crear apartados, pero no cuáles. El
diseño de referencia sí lo sabe, así que la estructura de partida se toma de él
y no de una invención nuestra.

Las pestañas y los apartados salen del proyecto exportado de Claude Design
(`design/exports/Fuentelcesped - Pantalla Principal (standalone).html`): el
orden por defecto de los epígrafes es su `infoDefault`, sus títulos son
`infoDefTitles` y las pestañas internas de cada uno son su registro de
`applyTabOv`. Ver `docs/diseno-ayuntamiento-prototipo.md` §8.

El diseño tiene un nivel más que el modelo: pestaña → epígrafe → pestaña interna
→ contenido, mientras que aquí son pestaña → apartado → elemento. El nivel que
se colapsa es el del epígrafe, porque cada pestaña interna suya trae un formato
distinto —la demografía es una serie, el análisis de agua son ficheros, los
teléfonos son contactos— y un apartado sólo tiene un formato. Así que el
epígrafe con pestañas internas se convierte en pestaña, y sus pestañas internas
en apartados; los epígrafes sin pestañas internas caen todos juntos en
«Información del municipio».

**Normativa municipal queda fuera a propósito.** Es el único epígrafe del diseño
que ya está construido en otro sitio: la biblioteca de ordenanzas con su búsqueda
semántica y el área fija de Normativa. Sembrarlo aquí duplicaría el dominio (ver
la fase B6 del plan).

El seed es **por organización y bajo petición**, como el del inventario
(`app/assets/seed.py`): la estructura es un punto de partida que cada
ayuntamiento adapta, no un catálogo del producto. No siembra contenido —ni un
teléfono, ni un concejal, ni un dato del padrón—: eso es información municipal
que sólo tiene el ayuntamiento.
"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.town_hall.models import MunicipalBlock

# Cada pestaña con sus apartados. El formato de cada apartado es el que pide su
# contenido en el diseño; el ayuntamiento puede cambiarlo después.
INITIAL_TOWN_HALL_STRUCTURE: tuple[dict, ...] = (
    {
        "key": "informacion",
        "title": "Información del municipio",
        "sections": (
            ("estructura", "Estructura de Gobierno", "people"),
            ("corporacion", "Corporación Municipal", "people"),
            ("suministros", "Suministros", "series"),
            ("organismos", "Organismos y empresas", "text"),
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
)


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

    for tab in INITIAL_TOWN_HALL_STRUCTURE:
        tab_key = str(tab["key"])
        tab_title = str(tab["title"])

        section_block = _find_block(
            blocks,
            seed_key=tab_key,
            title=tab_title,
            block_type="nav_section",
            parent_id=None,
        )
        if section_block is None:
            section_block = MunicipalBlock(
                organization_id=organization_id,
                parent_id=None,
                block_type="nav_section",
                title=tab_title,
                position=next_position(None),
                created_by_id=created_by_id,
                updated_by_id=created_by_id,
            )
            write_seed_data(section_block, tab_key, None)
            db.add(section_block)
            db.flush()
            blocks.append(section_block)
            created.append(tab_key)

        for item_key, item_title, layout in tab["sections"]:
            full_key = f"{tab_key}/{item_key}"
            existing = _find_block(
                blocks,
                seed_key=full_key,
                title=item_title,
                block_type="nav_item",
                parent_id=section_block.id,
            )
            if existing is not None:
                continue

            item_block = MunicipalBlock(
                organization_id=organization_id,
                parent_id=section_block.id,
                block_type="nav_item",
                title=item_title,
                position=next_position(section_block.id),
                created_by_id=created_by_id,
                updated_by_id=created_by_id,
            )
            write_seed_data(item_block, full_key, layout)
            db.add(item_block)
            db.flush()
            blocks.append(item_block)
            created.append(full_key)

    if created:
        db.commit()
    return created
