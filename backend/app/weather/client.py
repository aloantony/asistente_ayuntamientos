"""Minimal Open-Meteo client with a fixed host allowlist.

No hay tabla detrás. El tiempo que hace ahora no es un dato del ayuntamiento:
es una lectura de fuera que caduca en minutos, y guardarla obligaría a decidir
cuándo purgarla y a explicar por qué la base afirma que hacen 12 grados desde
hace tres semanas. Se pide en vivo y se cachea en Redis.

El host lo fija el código, nunca la petición, pero eso no basta: un DNS
comprometido podría resolver `api.open-meteo.com` a una dirección interna. Por
eso se resuelve primero, se exige que todas las direcciones sean públicas y se
conecta contra la dirección validada conservando el SNI, igual que hace
`assistant/web_reader.py`.
"""

import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlencode

# Allowlist de un solo elemento: cualquier otro host es un error de programa.
ALLOWED_HOST = "api.open-meteo.com"
FORECAST_PATH = "/v1/forecast"
DEFAULT_TIMEOUT_SECONDS = 6.0
MAX_RESPONSE_BYTES = 256 * 1024


class WeatherUnavailableError(RuntimeError):
    """La consulta al proveedor no se pudo completar."""


@dataclass(frozen=True)
class MunicipalWeather:
    temperature_c: float
    apparent_temperature_c: float | None
    relative_humidity: int | None
    wind_speed_kmh: float | None
    weather_code: int | None
    is_day: bool | None
    observed_at: str
    latitude: float
    longitude: float
    provider: str = "open-meteo"


def _require_public_ip(address) -> None:
    mapped = getattr(address, "ipv4_mapped", None)
    candidate = mapped or address
    if (
        not candidate.is_global
        or candidate.is_private
        or candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
        or getattr(candidate, "sixtofour", None) is not None
        or getattr(candidate, "teredo", None) is not None
    ):
        raise WeatherUnavailableError(
            "El proveedor meteorológico resolvió a una dirección no pública"
        )


def _resolve_public_address(host: str, port: int, timeout: float) -> str:
    if host != ALLOWED_HOST:
        raise WeatherUnavailableError(f"Host no permitido: {host}")
    try:
        records = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise WeatherUnavailableError(
            "No se pudo resolver el proveedor meteorológico"
        ) from error

    first_public: str | None = None
    for record in records[:16]:
        raw_address = str(record[4][0])
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as error:
            raise WeatherUnavailableError(
                "El proveedor meteorológico devolvió una dirección no válida"
            ) from error
        # Se comprueban todas, no solo la elegida: si el DNS mezcla una interna
        # entre las respuestas, la petición no debe salir.
        _require_public_ip(address)
        if first_public is None:
            first_public = str(address)
    if first_public is None:
        raise WeatherUnavailableError(
            "El proveedor meteorológico no tiene direcciones"
        )
    return first_public


def fetch_current_weather(
    latitude: float,
    longitude: float,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> MunicipalWeather:
    port = 443
    address = _resolve_public_address(ALLOWED_HOST, port, timeout)
    query = urlencode(
        {
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "current": ",".join(
                (
                    "temperature_2m",
                    "apparent_temperature",
                    "relative_humidity_2m",
                    "wind_speed_10m",
                    "weather_code",
                    "is_day",
                )
            ),
            "timezone": "Europe/Madrid",
        }
    )

    connection = http.client.HTTPSConnection(
        ALLOWED_HOST,
        port=port,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        # Conectar contra la dirección ya validada, con el SNI y la validación
        # de certificado del host real.
        raw_socket = socket.create_connection((address, port), timeout)
        try:
            connection.sock = connection._context.wrap_socket(
                raw_socket,
                server_hostname=ALLOWED_HOST,
            )
        except Exception:
            raw_socket.close()
            raise
        connection.request(
            "GET",
            f"{FORECAST_PATH}?{query}",
            headers={"Accept": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise WeatherUnavailableError(
                f"El proveedor meteorológico respondió {response.status}"
            )
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        raise WeatherUnavailableError(
            "No se pudo consultar el proveedor meteorológico"
        ) from error
    finally:
        connection.close()

    if len(body) > MAX_RESPONSE_BYTES:
        raise WeatherUnavailableError(
            "La respuesta del proveedor meteorológico es demasiado grande"
        )
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise WeatherUnavailableError(
            "El proveedor meteorológico devolvió una respuesta ilegible"
        ) from error

    return _parse_current(payload, latitude, longitude)


def _parse_current(
    payload: object,
    latitude: float,
    longitude: float,
) -> MunicipalWeather:
    if not isinstance(payload, dict):
        raise WeatherUnavailableError("Respuesta meteorológica inesperada")
    current = payload.get("current")
    if not isinstance(current, dict):
        raise WeatherUnavailableError("Respuesta meteorológica sin lectura actual")

    temperature = current.get("temperature_2m")
    observed_at = current.get("time")
    if not isinstance(temperature, (int, float)) or not isinstance(observed_at, str):
        # Sin temperatura ni instante la lectura no dice nada; mejor que el
        # bloque no se dibuje a que muestre un hueco.
        raise WeatherUnavailableError("Lectura meteorológica incompleta")

    def optional_number(key: str) -> float | None:
        value = current.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    humidity = current.get("relative_humidity_2m")
    weather_code = current.get("weather_code")
    is_day = current.get("is_day")

    return MunicipalWeather(
        temperature_c=float(temperature),
        apparent_temperature_c=optional_number("apparent_temperature"),
        relative_humidity=int(humidity) if isinstance(humidity, (int, float)) else None,
        wind_speed_kmh=optional_number("wind_speed_10m"),
        weather_code=int(weather_code)
        if isinstance(weather_code, (int, float))
        else None,
        is_day=bool(is_day) if isinstance(is_day, (int, float)) else None,
        observed_at=observed_at,
        latitude=latitude,
        longitude=longitude,
    )
