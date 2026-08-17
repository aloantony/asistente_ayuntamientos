import json
import socket

import pytest

from app.weather import client as weather_client
from app.weather import service as weather_service
from app.weather.client import (
    ALLOWED_HOST,
    MunicipalWeather,
    WeatherUnavailableError,
)


def addrinfo(address: str, family=socket.AF_INET):
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


def test_only_the_allowlisted_host_is_resolved():
    with pytest.raises(WeatherUnavailableError) as error:
        weather_client._resolve_public_address("evil.example.com", 443, 5.0)

    assert "Host no permitido" in str(error.value)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "169.254.169.254",
        "172.16.0.1",
        "0.0.0.0",
    ],
)
def test_private_and_link_local_answers_are_refused(monkeypatch, address):
    """Un DNS comprometido no debe poder empujarnos contra la red interna."""
    monkeypatch.setattr(
        weather_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: addrinfo(address),
    )

    with pytest.raises(WeatherUnavailableError):
        weather_client._resolve_public_address(ALLOWED_HOST, 443, 5.0)


def test_a_single_internal_answer_poisons_the_whole_set(monkeypatch):
    """Basta una dirección interna entre las respuestas para abortar."""
    monkeypatch.setattr(
        weather_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: addrinfo("93.184.216.34") + addrinfo("127.0.0.1"),
    )

    with pytest.raises(WeatherUnavailableError):
        weather_client._resolve_public_address(ALLOWED_HOST, 443, 5.0)


def test_a_public_answer_is_accepted(monkeypatch):
    monkeypatch.setattr(
        weather_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: addrinfo("93.184.216.34"),
    )

    assert (
        weather_client._resolve_public_address(ALLOWED_HOST, 443, 5.0)
        == "93.184.216.34"
    )


def test_dns_failure_is_reported_as_unavailable(monkeypatch):
    def explode(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(weather_client.socket, "getaddrinfo", explode)

    with pytest.raises(WeatherUnavailableError):
        weather_client._resolve_public_address(ALLOWED_HOST, 443, 5.0)


def test_current_reading_is_parsed():
    weather = weather_client._parse_current(
        {
            "current": {
                "time": "2026-08-04T18:00",
                "temperature_2m": 27.4,
                "apparent_temperature": 29.1,
                "relative_humidity_2m": 41,
                "wind_speed_10m": 12.5,
                "weather_code": 1,
                "is_day": 1,
            }
        },
        41.8,
        -3.5,
    )

    assert weather.temperature_c == 27.4
    assert weather.relative_humidity == 41
    assert weather.is_day is True
    assert weather.provider == "open-meteo"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"current": None},
        {"current": {"time": "2026-08-04T18:00"}},
        {"current": {"temperature_2m": 21.0}},
    ],
)
def test_incomplete_readings_are_refused(payload):
    """Sin temperatura o sin instante, mejor no dibujar el bloque."""
    with pytest.raises(WeatherUnavailableError):
        weather_client._parse_current(payload, 41.8, -3.5)


def test_optional_fields_may_be_missing():
    weather = weather_client._parse_current(
        {"current": {"time": "2026-08-04T18:00", "temperature_2m": 15}},
        41.8,
        -3.5,
    )

    assert weather.temperature_c == 15.0
    assert weather.apparent_temperature_c is None
    assert weather.wind_speed_kmh is None
    assert weather.is_day is None


def test_cache_key_groups_nearby_coordinates():
    assert weather_service.cache_key(41.80001, -3.50001) == weather_service.cache_key(
        41.80009, -3.50009
    )
    assert weather_service.cache_key(41.8, -3.5) != weather_service.cache_key(
        41.9, -3.5
    )


class FakeRedis:
    def __init__(self, initial=None):
        self.store = dict(initial or {})
        self.writes: list[tuple[str, int, str]] = []

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.writes.append((key, ttl, value))
        self.store[key] = value


def sample_weather() -> MunicipalWeather:
    return MunicipalWeather(
        temperature_c=20.0,
        apparent_temperature_c=None,
        relative_humidity=None,
        wind_speed_kmh=None,
        weather_code=None,
        is_day=None,
        observed_at="2026-08-04T18:00",
        latitude=41.8,
        longitude=-3.5,
    )


def test_a_cached_reading_avoids_calling_the_provider(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no debería llamarse al proveedor")

    monkeypatch.setattr(weather_service, "fetch_current_weather", explode)
    key = weather_service.cache_key(41.8, -3.5)
    redis = FakeRedis({key: json.dumps(vars(sample_weather()))})

    weather = weather_service.get_municipal_weather(41.8, -3.5, redis_client=redis)

    assert weather.temperature_c == 20.0


def test_a_fresh_reading_is_cached_with_the_declared_window(monkeypatch):
    monkeypatch.setattr(
        weather_service,
        "fetch_current_weather",
        lambda *args, **kwargs: sample_weather(),
    )
    redis = FakeRedis()

    weather_service.get_municipal_weather(41.8, -3.5, redis_client=redis)

    assert len(redis.writes) == 1
    _, ttl, _ = redis.writes[0]
    assert ttl == weather_service.CACHE_TTL_SECONDS


def test_an_unreadable_cache_entry_is_repopulated(monkeypatch):
    monkeypatch.setattr(
        weather_service,
        "fetch_current_weather",
        lambda *args, **kwargs: sample_weather(),
    )
    key = weather_service.cache_key(41.8, -3.5)
    redis = FakeRedis({key: "esto no es json"})

    weather = weather_service.get_municipal_weather(41.8, -3.5, redis_client=redis)

    assert weather.temperature_c == 20.0
    assert len(redis.writes) == 1


def test_a_broken_cache_does_not_break_the_reading(monkeypatch):
    """Redis caído degrada a consultar cada vez, no a fallar."""
    monkeypatch.setattr(
        weather_service,
        "fetch_current_weather",
        lambda *args, **kwargs: sample_weather(),
    )

    class BrokenRedis:
        def get(self, key):
            raise RuntimeError("redis caído")

        def setex(self, key, ttl, value):
            raise RuntimeError("redis caído")

    weather = weather_service.get_municipal_weather(
        41.8,
        -3.5,
        redis_client=BrokenRedis(),
    )

    assert weather.temperature_c == 20.0


def test_without_redis_the_reading_still_works(monkeypatch):
    monkeypatch.setattr(
        weather_service,
        "fetch_current_weather",
        lambda *args, **kwargs: sample_weather(),
    )

    weather = weather_service.get_municipal_weather(41.8, -3.5, redis_client=None)

    assert weather.temperature_c == 20.0
