import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.jobs import get_redis_connection
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Señal de vida barata: el proceso responde."""
    return {"status": "ok"}


@router.get("/ready")
def readiness_check(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    """Señal de servicio: la base de datos y Redis responden.

    `/health` devuelve `ok` incluso con la base caída, porque el arranque se
    traga el error de SQLAlchemy y la aplicación sirve igual. Sin esta ruta, un
    despliegue sin migrar parecería sano. No revela detalles del fallo: solo
    sirve para decidir si el contenedor entra en servicio. Ver ADR-036.
    """
    checks: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except SQLAlchemyError:
        logger.warning("Readiness check failed for the database", exc_info=True)
        checks["database"] = "unavailable"

    try:
        get_redis_connection().ping()
        checks["redis"] = "ok"
    except Exception:  # pragma: no cover - depende del entorno
        logger.warning("Readiness check failed for Redis", exc_info=True)
        checks["redis"] = "unavailable"

    if any(value != "ok" for value in checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", **checks}

    return {"status": "ready", **checks}
