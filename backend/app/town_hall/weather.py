"""Temperatura del municipio para la barra del Ayuntamiento.

Único punto de egreso del módulo, y el único del proyecto que no es de IA: por
él solo salen el nombre público del municipio (para geocodificarlo una vez) y
sus coordenadas. Nunca datos de usuarios, documentos ni conversaciones, y los
logs registran solo metadatos. Ver ADR-034.
"""

import json
import logging
from dataclasses import dataclass
from time import monotonic
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from app.core.config import settings

logger = logging.getLogger(__name__)

GEOCODING_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"


class WeatherUnavailableError(RuntimeError):
    """El proveedor no respondió o no reconoció el municipio."""


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


# Caché en memoria por organización: evita una llamada externa por cada carga
# de la pantalla. Es por proceso, como el limitador de login (`core/rate_limit`),
# y comparte su misma condición: mover a Redis antes de ir a multi-worker.
_temperature_cache: dict[int, tuple[float, float]] = {}


def get_cached_temperature(organization_id: int) -> float | None:
    entry = _temperature_cache.get(organization_id)
    if entry is None:
        return None

    expires_at, temperature = entry
    if monotonic() >= expires_at:
        del _temperature_cache[organization_id]
        return None

    return temperature


def cache_temperature(organization_id: int, temperature: float) -> None:
    _temperature_cache[organization_id] = (
        monotonic() + settings.municipal_weather_cache_seconds,
        temperature,
    )


def forget_cached_temperature(organization_id: int) -> None:
    _temperature_cache.pop(organization_id, None)


def _request_json(endpoint: str, parameters: dict[str, object]) -> dict:
    url = f"{endpoint}?{urlparse.urlencode(parameters)}"
    request = urlrequest.Request(
        url,
        headers={"User-Agent": "asistente-ayuntamientos"},
        method="GET",
    )

    try:
        with urlrequest.urlopen(
            request,
            timeout=settings.municipal_weather_timeout_seconds,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urlerror.HTTPError,
        urlerror.URLError,
        TimeoutError,
        ValueError,
    ) as exc:
        logger.warning("Municipal weather provider request failed", exc_info=True)
        raise WeatherUnavailableError("Weather provider unavailable") from exc

    if not isinstance(payload, dict):
        logger.warning("Municipal weather provider returned an unexpected payload")
        raise WeatherUnavailableError("Weather provider unavailable")

    return payload


def normalize_place_name(place_name: str) -> str:
    """Deja solo el topónimo: el buscador no entiende «Municipio, Provincia»."""
    return place_name.split(",", 1)[0].strip()


def geocode(place_name: str) -> Coordinates:
    """Resuelve un nombre de municipio a coordenadas, una sola vez."""
    normalized = normalize_place_name(place_name)
    if not normalized:
        raise WeatherUnavailableError("Municipality could not be located")

    payload = _request_json(
        GEOCODING_ENDPOINT,
        {"name": normalized, "count": 1, "language": "es", "format": "json"},
    )
    results = payload.get("results")

    if not isinstance(results, list) or not results:
        raise WeatherUnavailableError("Municipality could not be located")

    first = results[0]
    latitude = first.get("latitude") if isinstance(first, dict) else None
    longitude = first.get("longitude") if isinstance(first, dict) else None

    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        raise WeatherUnavailableError("Municipality could not be located")

    return Coordinates(latitude=float(latitude), longitude=float(longitude))


def fetch_temperature(coordinates: Coordinates) -> float:
    payload = _request_json(
        FORECAST_ENDPOINT,
        {
            "latitude": coordinates.latitude,
            "longitude": coordinates.longitude,
            "current": "temperature_2m",
        },
    )
    current = payload.get("current")
    temperature = current.get("temperature_2m") if isinstance(current, dict) else None

    if not isinstance(temperature, (int, float)):
        logger.warning("Municipal weather provider returned no temperature")
        raise WeatherUnavailableError("Weather provider unavailable")

    return float(temperature)
