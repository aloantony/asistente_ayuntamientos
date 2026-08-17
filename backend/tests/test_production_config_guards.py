"""Arranque en producción con valores de desarrollo.

El fallo que estas pruebas evitan no es teórico: `SECRET_KEY` firma los JWT, y
su valor por defecto está publicado en este repositorio. Un despliegue que
arrancase con él permitiría a cualquiera fabricarse un token de administrador.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import (
    MINIMUM_SECRET_KEY_LENGTH,
    Settings,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]

VALID_SECRET = "x" * MINIMUM_SECRET_KEY_LENGTH
PRODUCTION_BASE = {
    "environment": "production",
    "secret_key": VALID_SECRET,
    "database_url": "postgresql+psycopg://app:s3cret@db:5432/app",
    "cors_allowed_origins": "https://ayuntamiento.example",
    "bootstrap_admin_token": None,
}


def build(**overrides) -> Settings:
    return Settings(**{**PRODUCTION_BASE, **overrides})


def test_a_correctly_configured_production_starts() -> None:
    settings = build()

    assert settings.environment == "production"
    assert settings.is_development_like is False


@pytest.mark.parametrize("environment", ["development", "test"])
def test_placeholders_are_allowed_where_they_are_the_point(environment: str) -> None:
    # Un portátil y la suite de CI deben seguir arrancando sin ceremonia.
    settings = Settings(
        environment=environment,
        secret_key="change-me-in-development",
        database_url="postgresql+psycopg://app:app@postgres:5432/app",
        cors_allowed_origins="http://localhost:3000",
        bootstrap_admin_token="dev-bootstrap-token",
    )

    assert settings.is_development_like is True


@pytest.mark.parametrize(
    "placeholder",
    ["change-me-in-development", "change-this-secret-key-in-real-environments"],
)
def test_documented_placeholder_secret_keys_are_rejected(placeholder: str) -> None:
    with pytest.raises(ValueError) as error:
        build(secret_key=placeholder)

    assert "SECRET_KEY" in str(error.value)


def test_a_short_secret_key_is_rejected_even_if_it_is_not_a_placeholder() -> None:
    # Inventarse una clave corta es tan malo como no cambiarla.
    with pytest.raises(ValueError) as error:
        build(secret_key="x" * (MINIMUM_SECRET_KEY_LENGTH - 1))

    assert str(MINIMUM_SECRET_KEY_LENGTH) in str(error.value)


def test_the_bootstrap_token_placeholder_is_rejected() -> None:
    with pytest.raises(ValueError) as error:
        build(bootstrap_admin_token="dev-bootstrap-token")

    assert "BOOTSTRAP_ADMIN_TOKEN" in str(error.value)


def test_an_unset_bootstrap_token_is_the_safe_case() -> None:
    # No definirlo es más seguro que definirlo: el endpoint queda cerrado.
    assert build(bootstrap_admin_token=None).bootstrap_admin_token is None


def test_the_development_database_password_is_rejected() -> None:
    with pytest.raises(ValueError) as error:
        build(database_url="postgresql+psycopg://app:app@postgres:5432/app")

    assert "DATABASE_URL" in str(error.value)


def test_loopback_cors_origins_are_rejected() -> None:
    with pytest.raises(ValueError) as error:
        build(cors_allowed_origins="http://localhost:3000")

    assert "loopback" in str(error.value)


def test_plain_http_origins_are_rejected_because_the_cookie_is_secure() -> None:
    with pytest.raises(ValueError) as error:
        build(cors_allowed_origins="http://ayuntamiento.example")

    assert "https" in str(error.value)


def test_no_cors_origins_is_legitimate() -> None:
    # ADR-010 pone frontend y backend en el mismo host: ahí no hay petición
    # cross-origin que permitir, y exigir una entrada sería pedir ruido.
    assert build(cors_allowed_origins="").cors_origins == []


def test_every_problem_is_reported_at_once() -> None:
    # Arreglar un despliegue a base de reinicios, descubriendo un fallo por
    # vuelta, es la forma más rápida de que alguien se rinda a medias.
    with pytest.raises(ValueError) as error:
        build(
            secret_key="change-me-in-development",
            database_url="postgresql+psycopg://app:app@postgres:5432/app",
            cors_allowed_origins="http://localhost:3000",
            bootstrap_admin_token="dev-bootstrap-token",
        )

    message = str(error.value)
    for expected in (
        "SECRET_KEY",
        "BOOTSTRAP_ADMIN_TOKEN",
        "DATABASE_URL",
        "loopback",
    ):
        assert expected in message


def routes_for_environment(environment: str) -> set[str]:
    """Importa la app en un proceso limpio y devuelve sus rutas.

    Hace falta un subproceso porque `settings` es un singleton de módulo: una
    vez importada la app, cambiar el entorno en este proceso no la reconstruye.
    """
    env = {
        **os.environ,
        "ENVIRONMENT": environment,
        "SECRET_KEY": "x" * MINIMUM_SECRET_KEY_LENGTH,
        "DATABASE_URL": "postgresql+psycopg://app:s3cret@127.0.0.1:5432/app",
        "CORS_ALLOWED_ORIGINS": "https://ayuntamiento.example",
        "BOOTSTRAP_ADMIN_TOKEN": "",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app;"
            "print(*[getattr(r, 'path', '') for r in app.routes])",
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return set(completed.stdout.split())


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_api_docs_are_closed_in_production(path: str) -> None:
    # Los endpoints exigen autenticación, pero el esquema describe cada ruta,
    # cada modelo y cada permiso: no hay motivo para regalar el mapa.
    assert path not in routes_for_environment("production")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_api_docs_stay_open_in_development(path: str) -> None:
    assert path in routes_for_environment("development")
