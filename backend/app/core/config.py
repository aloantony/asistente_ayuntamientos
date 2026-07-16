from functools import lru_cache
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
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
    assistant_runtime: str = "anthropic"
    anthropic_api_key: str | None = None
    assistant_model: str = "claude-opus-4-8"
    assistant_max_tokens: int = 16000
    assistant_max_tool_iterations: int = 8
    assistant_max_tool_calls: int = 8
    assistant_turn_timeout_seconds: float = 120.0
    assistant_gateway_timeout_seconds: float = 30.0
    assistant_history_max_messages: int = 40
    assistant_max_attachments_per_message: int = 5
    assistant_attachment_max_context_chars: int = 6000
    assistant_attachment_total_context_chars: int = 12000
    assistant_attachment_max_extract_bytes: int = 5 * 1024 * 1024
    assistant_attachment_text_max_concurrency: int = 4
    hermes_agent_base_url: str = "http://127.0.0.1:8642/v1"
    hermes_agent_api_key: str | None = None
    hermes_agent_model: str = "hermes-agent"
    hermes_agent_real_data_allowed: bool = False
    hermes_agent_native_tools_disabled_confirmed: bool = False
    hermes_agent_timeout_seconds: float = 120.0
    hermes_agent_health_timeout_seconds: float = 3.0
    hermes_web_base_url: str = "http://127.0.0.1:8643/v1"
    hermes_web_api_key: str | None = None
    hermes_web_model: str = "hermes-agent"
    hermes_web_timeout_seconds: float = 60.0
    web_search_provider: str = "hermes"
    brave_search_api_key: str | None = None
    brave_search_timeout_seconds: float = 15.0
    brave_search_country: str = "ES"
    brave_search_language: str = "es"
    brave_search_ui_language: str = "es-ES"
    brave_search_storage_rights_confirmed: bool = False
    assistant_web_reader_enabled: bool = False
    web_page_timeout_seconds: float = 10.0
    web_page_dns_timeout_seconds: float = 3.0
    web_page_max_concurrent_readers: int = 4
    web_page_max_response_bytes: int = 2 * 1024 * 1024
    web_page_max_redirects: int = 3
    web_page_max_text_chars: int = 12000
    openai_api_key: str | None = None
    openai_responses_base_url: str = "https://api.openai.com/v1"
    openai_responses_model: str = "gpt-5.6"
    openai_responses_reasoning_effort: str = "medium"
    openai_responses_max_output_tokens: int = 25000
    # Local development bridge backed by an interactive ChatGPT/Codex login.
    # It is deliberately isolated from the developer's normal ~/.codex home.
    codex_subscription_enabled: bool = False
    codex_subscription_real_data_allowed: bool = False
    codex_subscription_command: str = "codex"
    codex_subscription_home: str = "~/.codex-asistente-ayuntamientos"
    codex_subscription_model: str = ""
    codex_subscription_reasoning_effort: str = "medium"
    codex_subscription_session_ttl_seconds: float = 180.0
    codex_subscription_max_sessions: int = 4
    codex_subscription_health_timeout_seconds: float = 3.0
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
        if normalized not in {
            "anthropic",
            "hermes_agent",
            "openai_responses",
            "codex_subscription",
        }:
            raise ValueError(
                "assistant_runtime must be 'anthropic', 'hermes_agent' or "
                "'openai_responses' or 'codex_subscription'"
            )
        return normalized

    @field_validator("codex_subscription_command")
    @classmethod
    def validate_codex_subscription_command(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("codex_subscription_command must not be empty")
        return normalized

    @field_validator("codex_subscription_home")
    @classmethod
    def validate_codex_subscription_home(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("codex_subscription_home must not be empty")
        expanded = Path(normalized).expanduser().resolve(strict=False)
        personal_home = Path("~/.codex").expanduser().resolve(strict=False)
        if expanded == personal_home:
            raise ValueError(
                "codex_subscription_home must be dedicated and cannot be ~/.codex"
            )
        return str(expanded)

    @field_validator("codex_subscription_model")
    @classmethod
    def validate_codex_subscription_model(cls, value: str) -> str:
        normalized = value.strip()
        if "\x00" in normalized or len(normalized) > 128:
            raise ValueError("codex_subscription_model is invalid")
        return normalized

    @field_validator("codex_subscription_reasoning_effort")
    @classmethod
    def validate_codex_subscription_reasoning_effort(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(
                "codex_subscription_reasoning_effort must be one of: none, "
                "minimal, low, medium, high, xhigh"
            )
        return normalized

    @field_validator(
        "codex_subscription_session_ttl_seconds",
        "codex_subscription_health_timeout_seconds",
    )
    @classmethod
    def validate_codex_subscription_timeouts(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError(
                "codex_subscription timeouts must be finite and greater than zero"
            )
        return value

    @field_validator("codex_subscription_max_sessions")
    @classmethod
    def validate_codex_subscription_max_sessions(cls, value: int) -> int:
        if not 1 <= value <= 32:
            raise ValueError(
                "codex_subscription_max_sessions must be between 1 and 32"
            )
        return value

    @model_validator(mode="after")
    def reject_codex_subscription_outside_development(self):
        if self.assistant_runtime != "codex_subscription":
            return self
        if self.environment != "development":
            raise ValueError(
                "codex_subscription is a local development runtime and is "
                "forbidden outside environment=development"
            )
        if not self.codex_subscription_enabled:
            raise ValueError(
                "codex_subscription requires the explicit local-development "
                "opt-in CODEX_SUBSCRIPTION_ENABLED=true"
            )
        if not self.codex_subscription_real_data_allowed:
            raise ValueError(
                "codex_subscription requires explicit approval before application "
                "data is sent to the shared ChatGPT account"
            )
        return self

    @field_validator("openai_responses_base_url")
    @classmethod
    def validate_openai_responses_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        allowed_hosts = {
            "api.openai.com",
            "ae.api.openai.com",
            "au.api.openai.com",
            "ca.api.openai.com",
            "eu.api.openai.com",
            "gb.api.openai.com",
            "in.api.openai.com",
            "jp.api.openai.com",
            "kr.api.openai.com",
            "sg.api.openai.com",
            "us.api.openai.com",
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed_hosts
            or parsed.netloc != parsed.hostname
            or parsed.path not in {"", "/v1"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "openai_responses_base_url must be an official HTTPS OpenAI "
                "API base URL ending in /v1"
            )
        return f"https://{parsed.hostname}/v1"

    @field_validator("openai_responses_model")
    @classmethod
    def validate_openai_responses_model(cls, value: str) -> str:
        normalized = value.strip()
        if not (
            normalized == "gpt-5.6" or normalized.startswith("gpt-5.6-")
        ):
            raise ValueError(
                "openai_responses_model must use the supported GPT-5.6 family"
            )
        return normalized

    @field_validator("openai_responses_reasoning_effort")
    @classmethod
    def validate_openai_responses_reasoning_effort(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(
                "openai_responses_reasoning_effort must be one of: none, low, "
                "medium, high, xhigh, max"
            )
        return normalized

    @field_validator("openai_responses_max_output_tokens")
    @classmethod
    def validate_openai_responses_max_output_tokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "openai_responses_max_output_tokens must be greater than zero"
            )
        return value

    @field_validator("speech_synthesis_max_chars")
    @classmethod
    def validate_speech_synthesis_max_chars(cls, value: int) -> int:
        if not 1 <= value <= 20000:
            raise ValueError(
                "speech_synthesis_max_chars must be between 1 and 20000"
            )
        return value

    @field_validator("web_search_provider")
    @classmethod
    def validate_web_search_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"brave", "hermes", "disabled"}:
            raise ValueError(
                "web_search_provider must be 'brave', 'hermes' or 'disabled'"
            )
        return normalized

    @field_validator(
        "assistant_turn_timeout_seconds",
        "assistant_gateway_timeout_seconds",
    )
    @classmethod
    def validate_assistant_timeouts(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError("assistant timeouts must be finite and greater than zero")
        return value

    @field_validator("assistant_max_attachments_per_message")
    @classmethod
    def validate_assistant_attachment_count(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError(
                "assistant_max_attachments_per_message must be between 1 and 10"
            )
        return value

    @field_validator(
        "assistant_attachment_max_context_chars",
        "assistant_attachment_total_context_chars",
    )
    @classmethod
    def validate_assistant_attachment_context_chars(cls, value: int) -> int:
        if not 1 <= value <= 100_000:
            raise ValueError(
                "assistant attachment context limits must be between 1 and 100000"
            )
        return value

    @field_validator("assistant_attachment_max_extract_bytes")
    @classmethod
    def validate_assistant_attachment_extract_bytes(cls, value: int) -> int:
        if not 1 <= value <= 25 * 1024 * 1024:
            raise ValueError(
                "assistant_attachment_max_extract_bytes must be between 1 and 25 MiB"
            )
        return value

    @field_validator("assistant_attachment_text_max_concurrency")
    @classmethod
    def validate_attachment_text_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise ValueError(
                "assistant_attachment_text_max_concurrency must be between 1 and 64"
            )
        return value

    @field_validator(
        "brave_search_timeout_seconds",
        "web_page_timeout_seconds",
        "web_page_dns_timeout_seconds",
    )
    @classmethod
    def validate_web_timeouts(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError(
                "web timeouts must be finite and greater than zero"
            )
        return value

    @field_validator("web_page_max_response_bytes")
    @classmethod
    def validate_web_page_max_response_bytes(cls, value: int) -> int:
        if not 1024 <= value <= 10 * 1024 * 1024:
            raise ValueError(
                "web_page_max_response_bytes must be between 1024 and 10485760"
            )
        return value

    @field_validator("web_page_max_concurrent_readers")
    @classmethod
    def validate_web_page_max_concurrent_readers(cls, value: int) -> int:
        if not 1 <= value <= 32:
            raise ValueError(
                "web_page_max_concurrent_readers must be between 1 and 32"
            )
        return value

    @field_validator("web_page_max_redirects")
    @classmethod
    def validate_web_page_max_redirects(cls, value: int) -> int:
        if not 0 <= value <= 5:
            raise ValueError("web_page_max_redirects must be between 0 and 5")
        return value

    @field_validator("web_page_max_text_chars")
    @classmethod
    def validate_web_page_max_text_chars(cls, value: int) -> int:
        if not 1000 <= value <= 50000:
            raise ValueError(
                "web_page_max_text_chars must be between 1000 and 50000"
            )
        return value

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
