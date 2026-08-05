from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.municipalities.models import Municipality, MunicipalityGeographySnapshot
from app.rbac.permissions import has_permission
from app.users.models import User
from app.weather.service import WeatherUnavailableError, get_municipal_weather

router = APIRouter(prefix="/municipalities", tags=["weather"])


class MunicipalWeatherRead(BaseModel):
    temperature_c: float
    apparent_temperature_c: float | None
    relative_humidity: int | None
    wind_speed_kmh: float | None
    weather_code: int | None
    is_day: bool | None
    observed_at: str
    latitude: float
    longitude: float
    provider: str


def _redis_client():
    """Redis es opcional: sin él la consulta sigue, solo que sin caché."""
    try:
        from app.core.jobs import get_redis_connection

        return get_redis_connection()
    except Exception:  # pragma: no cover - depende del entorno
        return None


@router.get(
    "/{municipality_id}/weather",
    response_model=MunicipalWeatherRead,
)
def get_municipality_weather(
    municipality_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalWeatherRead:
    municipality = db.get(Municipality, municipality_id)
    if municipality is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Municipality not found",
        )
    if not (
        has_permission(current_user, "municipalities.view", db)
        or has_permission(current_user, "municipalities.manage", db)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: municipalities.view",
        )

    snapshot = db.scalar(
        select(MunicipalityGeographySnapshot).where(
            MunicipalityGeographySnapshot.municipality_id == municipality.id,
            MunicipalityGeographySnapshot.is_current.is_(True),
        )
    )
    if snapshot is None:
        # Sin centroide oficial no se inventa una coordenada: el bloque del
        # tiempo simplemente no se dibuja.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Municipality has no official coordinates",
        )

    try:
        weather = get_municipal_weather(
            float(snapshot.latitude),
            float(snapshot.longitude),
            redis_client=_redis_client(),
        )
    except WeatherUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Weather provider unavailable",
        ) from error

    return MunicipalWeatherRead(**vars(weather))
