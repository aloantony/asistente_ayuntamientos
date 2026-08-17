"""Taxonomía de partida del inventario municipal.

El diseño de referencia trae el árbol de capas que un ayuntamiento pequeño
necesita el primer día: vías, agua, alumbrado, mobiliario, parques, cementerio,
vehículos. Sin él, un municipio recién dado de alta abre el mapa y encuentra un
formulario vacío al que no sabe qué contestar.

El seed es **por organización y bajo petición**, no automático al arrancar: la
taxonomía es un punto de partida, no un catálogo del producto. Cada ayuntamiento
la adapta —renombra, archiva, añade— y esos cambios no se pisan: lo que ya
existe se respeta y sólo se crea lo que falta.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.assets.models import MunicipalAssetCategory, MunicipalAssetType

# Colores tomados del `LAYER_DEFS` del diseño. Cada categoría trae los tipos
# con los que se suele empezar; ninguno es obligatorio ni definitivo.
INITIAL_ASSET_TAXONOMY: tuple[dict, ...] = (
    {
        "code": "vias",
        "name": "Vías y aceras",
        "color": "#8c7b6b",
        "sort_order": 10,
        "types": (
            ("calzada", "Calzada"),
            ("acera", "Acera"),
            ("camino-rural", "Camino rural"),
            ("senalizacion", "Señalización"),
        ),
    },
    {
        "code": "agua",
        "name": "Abastecimiento de agua",
        "color": "#2f7fb5",
        "sort_order": 20,
        "types": (
            ("deposito", "Depósito"),
            ("sondeo", "Sondeo"),
            ("red-distribucion", "Red de distribución"),
            ("hidrante", "Hidrante"),
            ("punto-recarga", "Punto de recarga de agua"),
        ),
    },
    {
        "code": "saneamiento",
        "name": "Saneamiento",
        "color": "#6b7f8c",
        "sort_order": 30,
        "types": (
            ("colector", "Colector"),
            ("arqueta", "Arqueta"),
            ("depuradora", "Depuradora"),
        ),
    },
    {
        "code": "alumbrado",
        "name": "Alumbrado público",
        "color": "#d8a13a",
        "sort_order": 40,
        "types": (
            ("luminaria-forja", "Luminaria de forja"),
            ("luminaria-industrial", "Luminaria industrial"),
            ("cuadro-electrico", "Cuadro eléctrico"),
        ),
    },
    {
        "code": "telecomunicaciones",
        "name": "Telecomunicaciones",
        "color": "#7a68b5",
        "sort_order": 50,
        "types": (
            ("antena", "Antena"),
            ("fibra", "Punto de fibra"),
        ),
    },
    {
        "code": "mobiliario",
        "name": "Mobiliario urbano",
        "color": "#b5744a",
        "sort_order": 60,
        "types": (
            ("banco", "Banco"),
            ("papelera", "Papelera"),
            ("fuente", "Fuente"),
            ("marquesina", "Marquesina"),
        ),
    },
    {
        "code": "parques",
        "name": "Parques y jardines",
        "color": "#4f9e63",
        "sort_order": 70,
        "types": (
            ("zona-verde", "Zona verde"),
            ("juego-infantil", "Juego infantil"),
            ("arbolado", "Arbolado"),
        ),
    },
    {
        "code": "residuos",
        "name": "Residuos",
        "color": "#7f8c6b",
        "sort_order": 80,
        "types": (
            ("contenedor", "Contenedor"),
            ("punto-limpio", "Punto limpio"),
        ),
    },
    {
        "code": "seguridad",
        "name": "Seguridad y emergencias",
        "color": "#c0553a",
        "sort_order": 90,
        "types": (
            ("videovigilancia", "Videovigilancia"),
            ("extintor", "Extintor"),
            ("punto-encuentro", "Punto de encuentro"),
        ),
    },
    {
        "code": "deportivas",
        "name": "Instalaciones deportivas",
        "color": "#3a8fb5",
        "sort_order": 100,
        "types": (
            ("pista", "Pista deportiva"),
            ("fronton", "Frontón"),
            ("piscina", "Piscina"),
        ),
    },
    {
        "code": "espacios-publicos",
        "name": "Espacios públicos",
        "color": "#a3894f",
        "sort_order": 110,
        "types": (
            ("plaza", "Plaza"),
            ("edificio-municipal", "Edificio municipal"),
            ("local-social", "Local social"),
        ),
    },
    {
        "code": "cementerio",
        "name": "Cementerio",
        "color": "#6b6b6b",
        "sort_order": 120,
        "types": (
            ("sepultura", "Sepultura"),
            ("nicho", "Nicho"),
            ("columbario", "Columbario"),
        ),
    },
    {
        "code": "vehiculos",
        "name": "Vehículos y aperos",
        "color": "#8c5a3a",
        "sort_order": 130,
        "types": (
            ("vehiculo", "Vehículo"),
            ("apero", "Apero"),
            ("maquinaria", "Maquinaria"),
        ),
    },
)


def ensure_initial_asset_taxonomy(
    db: Session,
    organization_id: int,
    *,
    created_by_id: int | None = None,
) -> list[str]:
    """Crea la taxonomía que falte en una organización y devuelve sus códigos.

    Es idempotente y no destructivo: una categoría que ya existe se deja como
    esté —renombrada, archivada o con otro color— y sus tipos ausentes se
    completan. Nunca se reescribe lo que el ayuntamiento haya decidido.
    """
    created: list[str] = []

    for category_data in INITIAL_ASSET_TAXONOMY:
        category = db.scalar(
            select(MunicipalAssetCategory).where(
                MunicipalAssetCategory.organization_id == organization_id,
                MunicipalAssetCategory.code == category_data["code"],
            )
        )
        if category is None:
            category = MunicipalAssetCategory(
                organization_id=organization_id,
                code=category_data["code"],
                name=category_data["name"],
                color=category_data["color"],
                sort_order=category_data["sort_order"],
                created_by_id=created_by_id,
                updated_by_id=created_by_id,
            )
            db.add(category)
            db.flush()
            created.append(category_data["code"])

        # Los tipos se completan aunque la categoría ya existiera: un
        # ayuntamiento puede haberla creado a mano y faltarle el detalle.
        existing_codes = set(
            db.scalars(
                select(MunicipalAssetType.code).where(
                    MunicipalAssetType.category_id == category.id
                )
            )
        )
        for order, (type_code, type_name) in enumerate(category_data["types"], start=1):
            if type_code in existing_codes:
                continue
            db.add(
                MunicipalAssetType(
                    organization_id=organization_id,
                    category_id=category.id,
                    code=type_code,
                    name=type_name,
                    sort_order=order * 10,
                    created_by_id=created_by_id,
                    updated_by_id=created_by_id,
                )
            )
            created.append(f"{category_data['code']}/{type_code}")

    if created:
        db.commit()
    return created
