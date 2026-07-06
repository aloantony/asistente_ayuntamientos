import logging
from typing import Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)


class SpeechTranscriptionError(RuntimeError):
    """Raised when audio cannot be transcribed for the user-facing turn."""


class SpeechTranscriber(Protocol):
    def transcribe(self, audio: bytes, *, language_code: str | None = None) -> str:
        """Return a text transcript for the supplied audio bytes."""


class DisabledSpeechTranscriber:
    def transcribe(self, audio: bytes, *, language_code: str | None = None) -> str:
        raise SpeechTranscriptionError("Speech transcription is disabled")


class NvidiaNimSpeechTranscriber:
    def transcribe(self, audio: bytes, *, language_code: str | None = None) -> str:
        if not settings.nvidia_api_key:
            raise SpeechTranscriptionError("NVIDIA API key is not configured")
        if not settings.nvidia_whisper_function_id:
            raise SpeechTranscriptionError("NVIDIA Whisper function id is not configured")
        if len(audio) > settings.speech_transcription_max_bytes:
            raise SpeechTranscriptionError("Audio file is too large")

        try:
            import riva.client
        except ImportError as exc:
            raise SpeechTranscriptionError(
                "nvidia-riva-client is not installed"
            ) from exc

        metadata = [
            ("function-id", settings.nvidia_whisper_function_id),
            ("authorization", f"Bearer {settings.nvidia_api_key}"),
        ]
        auth = riva.client.Auth(
            use_ssl=True,
            uri=settings.nvidia_riva_server,
            metadata_args=metadata,
        )
        config = riva.client.RecognitionConfig(
            language_code=language_code or settings.speech_transcription_language_code,
            enable_automatic_punctuation=True,
            max_alternatives=1,
        )

        try:
            response = riva.client.ASRService(auth).offline_recognize(audio, config)
        except Exception as exc:  # pragma: no cover - network/client specific
            logger.warning("NVIDIA speech transcription failed", exc_info=True)
            raise SpeechTranscriptionError("Speech transcription failed") from exc

        transcript = transcript_from_riva_response(response)
        if not transcript:
            raise SpeechTranscriptionError("Speech transcription returned no text")
        return transcript


def get_speech_transcriber() -> SpeechTranscriber:
    if settings.speech_transcription_runtime == "nvidia_nim":
        return NvidiaNimSpeechTranscriber()
    return DisabledSpeechTranscriber()


def transcribe_audio_bytes(audio: bytes, *, language_code: str | None = None) -> str:
    return get_speech_transcriber().transcribe(audio, language_code=language_code)


def transcript_from_riva_response(response) -> str:
    chunks: list[str] = []
    for result in getattr(response, "results", []) or []:
        alternatives = getattr(result, "alternatives", []) or []
        if not alternatives:
            continue
        transcript = str(getattr(alternatives[0], "transcript", "")).strip()
        if transcript:
            chunks.append(transcript)
    return " ".join(chunks).strip()
