import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolate_security_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "ENVIRONMENT",
        "SECRET_KEY",
        "BOOTSTRAP_ADMIN_TOKEN",
        "CORS_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_settings_reject_unknown_environment() -> None:
    with pytest.raises(ValidationError, match="environment"):
        Settings(_env_file=None, environment="prod")


@pytest.mark.parametrize(
    "secret_key",
    [
        "change-me-in-development",
        "change-this-secret-key-in-real-environments",
        "too-short",
    ],
)
def test_production_rejects_unsafe_secret_key(secret_key: str) -> None:
    with pytest.raises(ValidationError, match="production requires"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key=secret_key,
            cors_allowed_origins="https://municipal.example",
        )


@pytest.mark.parametrize(
    "cors_allowed_origins",
    [
        "*",
        "http://municipal.example",
        "https://municipal.example/path",
    ],
)
def test_production_rejects_unsafe_cors_origins(
    cors_allowed_origins: str,
) -> None:
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key="a-production-secret-with-at-least-32-characters",
            cors_allowed_origins=cors_allowed_origins,
        )


def test_production_rejects_short_bootstrap_token() -> None:
    with pytest.raises(ValidationError, match="BOOTSTRAP_ADMIN_TOKEN"):
        Settings(
            _env_file=None,
            environment="production",
            secret_key="a-production-secret-with-at-least-32-characters",
            bootstrap_admin_token="dev-bootstrap-token",
            cors_allowed_origins="https://municipal.example",
        )


def test_production_accepts_strong_secret_key() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        secret_key="a-production-secret-with-at-least-32-characters",
        bootstrap_admin_token="a-bootstrap-token-with-at-least-32-characters",
        cors_allowed_origins="https://municipal.example",
    )

    assert settings.environment == "production"


def test_production_settings_initialize_during_module_import() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "ENVIRONMENT": "production",
            "SECRET_KEY": "a-production-secret-with-at-least-32-characters",
            "BOOTSTRAP_ADMIN_TOKEN": (
                "a-bootstrap-token-with-at-least-32-characters"
            ),
            "CORS_ALLOWED_ORIGINS": "https://municipal.example",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.core.config import settings; "
                "assert settings.environment == 'production'"
            ),
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
