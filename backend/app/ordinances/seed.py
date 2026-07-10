from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ordinances.models import OfficialLegalSource

INITIAL_OFFICIAL_LEGAL_SOURCES = (
    {
        "name": "BOE Datos Abiertos",
        "base_url": "https://www.boe.es/datosabiertos/api/api.php",
        "domain": "boe.es",
        "source_type": "boe",
        "status": "active",
        "notes": "Fuente estatal auxiliar; no es fuente primaria de ordenanzas municipales.",
    },
    {
        "name": "Boletín Oficial de la Provincia de Burgos",
        "base_url": "http://bopbur.diputaciondeburgos.es/",
        "domain": "bopbur.diputaciondeburgos.es",
        "source_type": "bop",
        "status": "active",
        "notes": (
            "Fuente primaria del MVP de Burgos. Su transporte HTTP legacy solo "
            "se consume mediante el proxy de archivo firmado."
        ),
    },
)


def ensure_initial_official_legal_sources(db: Session) -> list[str]:
    """Seed official legal sources needed by ordinance import jobs.

    Returns the domains that were created in this run. Existing sources are left
    untouched so local curation/status changes are preserved.
    """
    created: list[str] = []
    changed = False
    for source_data in INITIAL_OFFICIAL_LEGAL_SOURCES:
        domain = source_data["domain"]
        existing = db.scalar(
            select(OfficialLegalSource).where(OfficialLegalSource.domain == domain)
        )
        if existing is not None:
            if (
                domain == "bopbur.diputaciondeburgos.es"
                and existing.base_url
                == "https://bopbur.diputaciondeburgos.es/"
            ):
                existing.base_url = source_data["base_url"]
                if existing.notes == (
                    "Fuente primaria del MVP de ordenanzas para pueblos de la "
                    "provincia de Burgos."
                ):
                    existing.notes = source_data["notes"]
                changed = True
            continue
        db.add(OfficialLegalSource(**source_data))
        created.append(domain)
    if created or changed:
        db.commit()
    return created
