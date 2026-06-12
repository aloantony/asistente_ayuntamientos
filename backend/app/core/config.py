from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Asistente Ayuntamientos"
    app_version: str = "0.1.0"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://app:app@postgres:5432/app"
    redis_url: str = "redis://redis:6379/0"
    secret_key: str = "change-me-in-development"
    access_token_expire_minutes: int = 60
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 60
    bootstrap_admin_token: str | None = None
    jwt_algorithm: str = "HS256"
    cors_allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    document_storage_root: str = "/var/lib/asistente_ayuntamientos/documents"
    document_max_upload_bytes: int = 25 * 1024 * 1024
    anthropic_api_key: str | None = None
    assistant_model: str = "claude-opus-4-8"
    assistant_max_tokens: int = 16000
    assistant_max_tool_iterations: int = 8

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("database_url")
    @classmethod
    def prefer_psycopg_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
