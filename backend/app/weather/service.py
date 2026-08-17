"""Cache de la lectura meteorológica en Redis.

La ventana es de 30 minutos: Open-Meteo actualiza cada cuarto de hora y el
bloque de la barra superior se pinta en cada carga de página, así que sin caché
un municipio con varias personas trabajando generaría decenas de peticiones por
minuto a un servicio gratuito. Si Redis no está disponible, la consulta sigue
adelante sin caché en lugar de fallar: el tiempo es un adorno informativo, no
un dato del que dependa ninguna decisión.
"""

import json
import logging
from dataclasses import asdict

from app.weather.client import (
    MunicipalWeather,
    WeatherUnavailableError,
    fetch_current_weather,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30 * 60
CACHE_PREFIX = "weather:open-meteo:v1"


def cache_key(latitude: float, longitude: float) -> str:
    """Redondear a 3 decimales agrupa el municipio en ~100 m.

    Dos peticiones del mismo ayuntamiento comparten entrada aunque el centroide
    se recalcule con una precisión ligeramente distinta.
    """
    return f"{CACHE_PREFIX}:{latitude:.3f}:{longitude:.3f}"


def get_municipal_weather(
    latitude: float,
    longitude: float,
    *,
    redis_client=None,
) -> MunicipalWeather:
    key = cache_key(latitude, longitude)

    if redis_client is not None:
        try:
            cached = redis_client.get(key)
        except Exception:
            logger.warning("Weather cache read failed", exc_info=True)
            cached = None
        if cached:
            try:
                return MunicipalWeather(**json.loads(cached))
            except (TypeError, ValueError):
                # Una entrada de un formato anterior se ignora y se repuebla.
                logger.info("Discarding unreadable weather cache entry")

    weather = fetch_current_weather(latitude, longitude)

    if redis_client is not None:
        try:
            redis_client.setex(key, CACHE_TTL_SECONDS, json.dumps(asdict(weather)))
        except Exception:
            logger.warning("Weather cache write failed", exc_info=True)

    return weather


__all__ = [
    "CACHE_TTL_SECONDS",
    "MunicipalWeather",
    "WeatherUnavailableError",
    "cache_key",
    "get_municipal_weather",
]
