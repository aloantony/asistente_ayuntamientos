from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_secure_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


class Settings(BaseSettings):
    app_name: str = "Asistente Ayuntamientos"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://app:app@postgres:5432/app"
    redis_url: str = "redis://redis:6379/0"
    secret_key: str = "change-me-in-development"
    access_token_expire_minutes: int = 60
    organization_invitation_expire_hours: int = Field(default=72, ge=1, le=720)
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: int = 60
    bootstrap_admin_token: str | None = None
    jwt_algorithm: str = "HS256"
    cors_allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    document_storage_root: str = "/var/lib/asistente_ayuntamientos/documents"
    document_max_upload_bytes: int = 25 * 1024 * 1024
    assistant_runtime: str = "anthropic"
    anthropic_api_key: str | None = None
    assistant_model: str = "claude-opus-4-8"
    assistant_max_tokens: int = 16000
    assistant_max_tool_iterations: int = 8
    assistant_history_max_messages: int = 40
    hermes_agent_base_url: str = "http://127.0.0.1:8642/v1"
    hermes_agent_api_key: str | None = None
    hermes_agent_model: str = "hermes-agent"
    hermes_agent_real_data_allowed: bool = False
    hermes_agent_timeout_seconds: float = 120.0
    hermes_agent_health_timeout_seconds: float = 3.0
    hermes_web_base_url: str = "http://127.0.0.1:8643/v1"
    hermes_web_api_key: str | None = None
    hermes_web_model: str = "hermes-agent"
    hermes_web_timeout_seconds: float = 60.0
    ordinance_import_max_fetch_bytes: int = 15 * 1024 * 1024
    ordinance_import_search_limit: int = 5
    ordinance_import_max_chunks: int = 200
    ordinance_chunk_chars: int = 1400
    embeddings_runtime: str = "local_hash"
    embeddings_base_url: str | None = None
    embeddings_api_key: str | None = None
    embeddings_model: str = "local-hash-384"
    embeddings_dimensions: int = 384
    embeddings_timeout_seconds: float = 60.0
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_link_code_ttl_seconds: int = 600
    speech_transcription_runtime: str = "disabled"
    speech_transcription_language_code: str = "multi"
    speech_transcription_max_bytes: int = 20 * 1024 * 1024
    nvidia_api_key: str | None = None
    nvidia_riva_server: str = "grpc.nvcf.nvidia.com:443"
    nvidia_whisper_function_id: str | None = None
    speech_synthesis_runtime: str = "disabled"
    speech_synthesis_voice: str = "es-ES-ElviraNeural"
    speech_synthesis_language_code: str = "es-ES"
    speech_synthesis_max_chars: int = 3000
    speech_synthesis_timeout_seconds: float = 30.0
    azure_speech_key: str | None = None
    azure_speech_region: str = "westeurope"

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

    @field_validator("assistant_runtime")
    @classmethod
    def validate_assistant_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"anthropic", "hermes_agent"}:
            raise ValueError("assistant_runtime must be 'anthropic' or 'hermes_agent'")
        return normalized

    @field_validator("embeddings_runtime")
    @classmethod
    def validate_embeddings_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"local_hash", "openai_compatible", "disabled"}:
            raise ValueError(
                "embeddings_runtime must be 'local_hash', 'openai_compatible' or 'disabled'"
            )
        return normalized

    @field_validator("speech_transcription_runtime")
    @classmethod
    def validate_speech_transcription_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "nvidia_nim"}:
            raise ValueError(
                "speech_transcription_runtime must be 'disabled' or 'nvidia_nim'"
            )
        return normalized

    @field_validator("speech_synthesis_runtime")
    @classmethod
    def validate_speech_synthesis_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "azure"}:
            raise ValueError("speech_synthesis_runtime must be 'disabled' or 'azure'")
        return normalized

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.environment != "production":
            return self

        if (
            self.secret_key
            in {
                "change-me-in-development",
                "change-this-secret-key-in-real-environments",
            }
            or len(self.secret_key) < 32
        ):
            raise ValueError(
                "production requires a non-default SECRET_KEY of at least 32 characters"
            )
        if self.bootstrap_admin_token and len(self.bootstrap_admin_token) < 32:
            raise ValueError(
                "production BOOTSTRAP_ADMIN_TOKEN must contain at least 32 characters"
            )

        origins = self.cors_origins
        if not origins or any(not _is_secure_origin(origin) for origin in origins):
            raise ValueError(
                "production CORS_ALLOWED_ORIGINS must contain only explicit HTTPS origins"
            )
        return self

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
