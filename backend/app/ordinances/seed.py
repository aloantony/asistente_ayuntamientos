from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ordinances.models import OfficialLegalSource

CASTILLA_LEON_PROVINCIAL_BOP_SOURCES = (
    {
        "name": "Boletín Oficial de la Provincia de Ávila",
        "base_url": "https://www.diputacionavila.es/boletin-oficial/",
        "domain": "diputacionavila.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Burgos",
        "base_url": "https://bopbur.diputaciondeburgos.es/",
        "domain": "bopbur.diputaciondeburgos.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial con conector determinista BOPBUR para importación "
            "supervisada de ordenanzas municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de León",
        "base_url": "https://bop.dipuleon.es/",
        "domain": "bop.dipuleon.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Palencia",
        "base_url": "https://www.diputaciondepalencia.es/servicios/boletin-oficial-provincia",
        "domain": "diputaciondepalencia.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Salamanca",
        "base_url": "https://sede.diputaciondesalamanca.gob.es/BOP/",
        "domain": "diputaciondesalamanca.gob.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Segovia",
        "base_url": "https://www.dipsegovia.es/bop",
        "domain": "dipsegovia.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Soria",
        "base_url": "https://bop.dipsoria.es/",
        "domain": "bop.dipsoria.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Valladolid",
        "base_url": "https://bop.sede.diputaciondevalladolid.es/",
        "domain": "diputaciondevalladolid.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
    {
        "name": "Boletín Oficial de la Provincia de Zamora",
        "base_url": "https://www.diputaciondezamora.es/opencms/servicios/BOP/bop/index.html",
        "domain": "diputaciondezamora.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente provincial para importación supervisada de ordenanzas "
            "municipales de Castilla y León."
        ),
    },
)

INITIAL_OFFICIAL_LEGAL_SOURCES = (
    {
        "name": "BOE Datos Abiertos",
        "base_url": "https://www.boe.es/datosabiertos/api/api.php",
        "domain": "boe.es",
        "source_type": "boe",
        "status": "active",
        "notes": "Fuente estatal auxiliar; no es fuente primaria de ordenanzas municipales.",
    },
    *CASTILLA_LEON_PROVINCIAL_BOP_SOURCES,
)


def ensure_initial_official_legal_sources(db: Session) -> list[str]:
    """Seed official legal sources needed by ordinance import jobs.

    Returns the domains that were created in this run. Existing sources are left
    untouched so local curation/status changes are preserved.
    """
    created: list[str] = []
    for source_data in INITIAL_OFFICIAL_LEGAL_SOURCES:
        domain = source_data["domain"]
        existing = db.scalar(
            select(OfficialLegalSource).where(OfficialLegalSource.domain == domain)
        )
        if existing is not None:
            continue
        db.add(OfficialLegalSource(**source_data))
        created.append(domain)
    if created:
        db.commit()
    return created
