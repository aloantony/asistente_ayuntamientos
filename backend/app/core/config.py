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
    municipal_shield_max_upload_bytes: int = 2 * 1024 * 1024
    municipal_weather_timeout_seconds: float = 5.0
    municipal_weather_cache_seconds: int = 1800
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
    openai_api_key: str | None = None
    assistant_realtime_enabled: bool = True
    assistant_realtime_model: str = "gpt-realtime-2.1"
    assistant_realtime_voice: str = "marin"
    assistant_realtime_url: str = "https://api.openai.com/v1/realtime/calls"
    assistant_realtime_client_secret_url: str = (
        "https://api.openai.com/v1/realtime/client_secrets"
    )
    assistant_realtime_timeout_seconds: float = 20.0
    assistant_realtime_language_code: str = "es"
    assistant_realtime_transcription_model: str = "gpt-realtime-whisper"
    assistant_realtime_transcription_delay: str = "low"
    assistant_realtime_vad_threshold: float = 0.5
    assistant_realtime_vad_prefix_padding_ms: int = 300
    assistant_realtime_vad_silence_duration_ms: int = 500
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
    speech_synthesis_voice: str = "es-ES-DarioNeural"
    speech_synthesis_language_code: str = "es-ES"
    # Azure output format. 48 kHz / 96 kbps sounds noticeably fuller than the
    # older 24 kHz / 48 kbps default; tune via SPEECH_SYNTHESIS_OUTPUT_FORMAT.
    speech_synthesis_output_format: str = "audio-48khz-96kbitrate-mono-mp3"
    # SSML prosody rate, e.g. "+0%" (normal), "+12%" (a bit faster). Empty
    # string disables the prosody wrapper. Tune via SPEECH_SYNTHESIS_RATE.
    speech_synthesis_rate: str = "+12%"
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
