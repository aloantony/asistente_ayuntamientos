import re
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


def _is_secure_service_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
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
    login_rate_limit_attempts: int = Field(default=10, gt=0)
    login_rate_limit_window_seconds: int = Field(default=60, gt=0)
    rate_limit_backend: Literal["redis", "memory"] = "redis"
    rate_limit_redis_timeout_seconds: float = Field(default=0.5, gt=0)
    rate_limit_key_prefix: str = "asistente:rate-limit"
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
    self_hosted_ai_base_url: str | None = None
    self_hosted_ai_api_key: str | None = None
    self_hosted_ai_model: str = "municipal-assistant"
    self_hosted_ai_timeout_seconds: float = 120.0
    self_hosted_ai_health_timeout_seconds: float = 3.0
    hermes_web_base_url: str = "http://127.0.0.1:8643/v1"
    hermes_web_api_key: str | None = None
    hermes_web_model: str = "hermes-agent"
    hermes_web_timeout_seconds: float = 60.0
    ordinance_import_max_fetch_bytes: int = 15 * 1024 * 1024
    ordinance_import_search_limit: int = 5
    ordinance_import_max_chunks: int = 200
    ordinance_chunk_chars: int = 1400
    bop_archive_proxy_base_url: str | None = None
    bop_archive_proxy_api_key: str | None = None
    bop_archive_proxy_signing_key: str | None = None
    bop_archive_proxy_timeout_seconds: float = 45.0
    bop_archive_proxy_storage_root: str = (
        "/var/lib/asistente_ayuntamientos/bop_archive"
    )
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

    @field_validator("rate_limit_key_prefix")
    @classmethod
    def validate_rate_limit_key_prefix(cls, value: str) -> str:
        normalized = value.strip().strip(":")
        if not re.fullmatch(r"[A-Za-z0-9:_-]+", normalized):
            raise ValueError(
                "rate_limit_key_prefix may only contain letters, numbers, "
                "':', '_' and '-'"
            )
        return normalized

    @field_validator("assistant_runtime")
    @classmethod
    def validate_assistant_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"anthropic", "hermes_agent", "self_hosted"}:
            raise ValueError(
                "assistant_runtime must be 'anthropic', 'hermes_agent' or "
                "'self_hosted'"
            )
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
        if self.rate_limit_backend != "redis":
            raise ValueError("production requires RATE_LIMIT_BACKEND=redis")

        if self.assistant_runtime == "self_hosted":
            if not self.self_hosted_ai_base_url or not _is_secure_service_url(
                self.self_hosted_ai_base_url
            ):
                raise ValueError(
                    "production self-hosted AI requires an explicit HTTPS base URL"
                )
            if not self.self_hosted_ai_api_key or len(self.self_hosted_ai_api_key) < 32:
                raise ValueError(
                    "production SELF_HOSTED_AI_API_KEY must contain at least "
                    "32 characters"
                )

        bop_proxy_values = (
            self.bop_archive_proxy_base_url,
            self.bop_archive_proxy_api_key,
            self.bop_archive_proxy_signing_key,
        )
        if any(bop_proxy_values):
            if self.bop_archive_proxy_base_url and not _is_secure_service_url(
                self.bop_archive_proxy_base_url
            ):
                raise ValueError(
                    "production BOP archive proxy requires an explicit HTTPS "
                    "base URL"
                )
            if not self.bop_archive_proxy_api_key or not (
                self.bop_archive_proxy_signing_key
            ):
                raise ValueError(
                    "production BOP archive proxy requires both API and signing keys"
                )
            if len(self.bop_archive_proxy_api_key) < 32:
                raise ValueError(
                    "production BOP_ARCHIVE_PROXY_API_KEY must contain at least "
                    "32 characters"
                )
            if len(self.bop_archive_proxy_signing_key) < 32:
                raise ValueError(
                    "production BOP_ARCHIVE_PROXY_SIGNING_KEY must contain at "
                    "least 32 characters"
                )
            if self.bop_archive_proxy_api_key == self.bop_archive_proxy_signing_key:
                raise ValueError(
                    "production BOP archive proxy API and signing keys must differ"
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
