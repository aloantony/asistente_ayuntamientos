"""Cobertura del endurecimiento para el despliegue público (ADR-031, ADR-032)."""

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.core.config import Settings, settings
from app.db.session import get_db
from app.main import app

# `_env_file=None` no aísla de os.environ, y el contenedor exporta todo el .env:
# cada campo relevante se fija explícitamente para que el test no dependa de él.
PRODUCTION_BASE = {
    "_env_file": None,
    "environment": "production",
    "secret_key": "x" * 48,
    "cors_allowed_origins": "https://anacleto.example",
    "allowed_hosts": "anacleto.example",
    "assistant_runtime": "anthropic",
}


def production_settings(**overrides) -> Settings:
    return Settings(**{**PRODUCTION_BASE, **overrides})


def test_production_accepts_a_hardened_configuration():
    configured = production_settings()

    assert configured.is_production is True
    assert configured.docs_enabled is False
    assert configured.cors_origins == ["https://anacleto.example"]
    assert configured.allowed_hosts_list == ["anacleto.example"]


@pytest.mark.parametrize(
    "secret_key",
    [
        "change-me-in-development",
        "change-this-secret-key-in-real-environments",
        "too-short",
    ],
)
def test_production_refuses_a_weak_secret_key(secret_key):
    with pytest.raises(ValidationError):
        production_settings(secret_key=secret_key)


@pytest.mark.parametrize(
    "origins",
    ["*", "http://anacleto.example", "https://anacleto.example,http://insecure"],
)
def test_production_refuses_insecure_cors_origins(origins):
    with pytest.raises(ValidationError):
        production_settings(cors_allowed_origins=origins)


def test_production_refuses_a_wildcard_host():
    with pytest.raises(ValidationError):
        production_settings(allowed_hosts="*")


def test_development_keeps_the_relaxed_defaults():
    """Las guardas no deben estorbar en desarrollo."""
    configured = Settings(
        _env_file=None,
        environment="development",
        secret_key="change-me-in-development",
        cors_allowed_origins="http://localhost:3000",
        allowed_hosts="*",
    )

    assert configured.docs_enabled is True
    assert configured.is_production is False


@pytest.mark.parametrize("value", ["Production", " production ", "prod", ""])
def test_environment_requires_a_canonical_value(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment=value)


def test_api_root_path_is_normalized():
    assert Settings(_env_file=None, api_root_path="/api/").api_root_path == "/api"
    assert Settings(_env_file=None, api_root_path="").api_root_path == ""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_root_path="api")


def test_responses_carry_the_security_headers(client):
    response = client.get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_cross_site_writes_are_rejected(client):
    """Segunda capa CSRF sobre la cookie SameSite=Lax (ADR-032)."""
    response = client.post(
        "/auth/login",
        json={"email": "someone@example.com", "password": "irrelevant"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site request rejected"


def test_same_site_writes_are_allowed(client):
    """Con un origen propio la escritura pasa y la valida la aplicación."""
    origin = settings.cors_origins[0]

    response = client.post(
        "/auth/login",
        json={"email": "someone@example.com", "password": "irrelevant"},
        headers={"Origin": origin},
    )

    assert response.status_code == 401


def test_writes_without_an_origin_are_allowed(client):
    """Los clientes de API y el webhook de Telegram no envían Origin."""
    response = client.post(
        "/auth/login",
        json={"email": "someone@example.com", "password": "irrelevant"},
    )

    assert response.status_code == 401


def test_reads_are_never_blocked_by_the_csrf_guard(client):
    response = client.get("/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200


def test_cross_site_hint_without_an_origin_is_rejected(client):
    response = client.post(
        "/auth/login",
        json={"email": "someone@example.com", "password": "irrelevant"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_login_ceiling_stops_spraying_across_distinct_accounts(client):
    """La clave (IP, cuenta) daba cupo nuevo a cada cuenta probada (ADR-032)."""
    limit = settings.login_ip_rate_limit_attempts
    statuses = []

    # Una cuenta distinta por intento: el limitador por cuenta nunca se agota.
    for attempt in range(limit + 1):
        response = client.post(
            "/auth/login",
            json={
                "email": f"victim{attempt}@example.com",
                "password": "wrong-password",
            },
        )
        statuses.append(response.status_code)

    assert statuses[0] == 401
    assert statuses[-1] == 429
    assert statuses.count(429) == 1


def test_bootstrap_admin_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_token", "a-real-token")
    limit = settings.bootstrap_admin_rate_limit_attempts

    statuses = [
        client.post(
            "/auth/bootstrap-admin",
            json={
                "email": "admin@example.com",
                "password": "a-strong-password",
                "full_name": "Admin",
            },
            headers={"X-Bootstrap-Admin-Token": "guess"},
        ).status_code
        for _ in range(limit + 1)
    ]

    assert statuses[0] == 401
    assert statuses[-1] == 429


def test_readiness_reports_the_dependencies(client):
    response = client.get("/ready")

    assert response.status_code in (200, 503)
    assert "database" in response.json()


def test_readiness_fails_when_the_database_is_down(client):
    class BrokenSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        response = client.get("/ready")
    finally:
        # El override del harness se restaura al salir del fixture `client`.
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
