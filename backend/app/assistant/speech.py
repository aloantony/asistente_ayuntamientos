import logging
import subprocess
from typing import Protocol
from urllib import error as urlerror
from urllib import request as urlrequest
from xml.sax.saxutils import escape

from app.core.config import settings

logger = logging.getLogger(__name__)


class SpeechTranscriptionError(RuntimeError):
    """Raised when audio cannot be transcribed for the user-facing turn."""


class SpeechSynthesisError(RuntimeError):
    """Raised when text cannot be synthesized for the user-facing turn."""


class SpeechTranscriber(Protocol):
    def transcribe(self, audio: bytes, *, language_code: str | None = None) -> str:
        """Return a text transcript for the supplied audio bytes."""


class SpeechSynthesizer(Protocol):
    def synthesize(self, text: str) -> bytes:
        """Return audio bytes for the supplied text."""


class DisabledSpeechTranscriber:
    def transcribe(self, audio: bytes, *, language_code: str | None = None) -> str:
        raise SpeechTranscriptionError("Speech transcription is disabled")


class DisabledSpeechSynthesizer:
    def synthesize(self, text: str) -> bytes:
        raise SpeechSynthesisError("Speech synthesis is disabled")


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
        audio, encoding, sample_rate = prepare_audio_for_riva(audio)
        config = riva.client.RecognitionConfig(
            encoding=encoding,
            sample_rate_hertz=sample_rate,
            audio_channel_count=1,
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


class AzureSpeechSynthesizer:
    def synthesize(self, text: str) -> bytes:
        if not settings.azure_speech_key:
            raise SpeechSynthesisError("Azure Speech key is not configured")

        endpoint = (
            f"https://{settings.azure_speech_region}."
            "tts.speech.microsoft.com/cognitiveservices/v1"
        )
        ssml = build_azure_ssml(
            text,
            settings.speech_synthesis_voice,
            settings.speech_synthesis_language_code,
            settings.speech_synthesis_rate,
        )
        request = urlrequest.Request(
            endpoint,
            data=ssml.encode("utf-8"),
            headers={
                "Ocp-Apim-Subscription-Key": settings.azure_speech_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": settings.speech_synthesis_output_format,
                "User-Agent": "asistente-ayuntamientos",
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(
                request,
                timeout=settings.speech_synthesis_timeout_seconds,
            ) as response:
                audio = response.read()
        except (urlerror.HTTPError, urlerror.URLError, TimeoutError) as exc:
            logger.warning("Azure speech synthesis failed", exc_info=True)
            raise SpeechSynthesisError("Speech synthesis failed") from exc

        if not audio:
            try:
                raise ValueError("Azure speech synthesis returned no audio")
            except ValueError as exc:
                logger.warning("Azure speech synthesis failed", exc_info=True)
                raise SpeechSynthesisError("Speech synthesis failed") from exc
        return audio


def build_azure_ssml(text: str, voice: str, language: str, rate: str = "") -> str:
    escaped_text = escape(text)
    inner = escaped_text
    if rate:
        escaped_rate = escape(rate, {"'": "&apos;", '"': "&quot;"})
        inner = f"<prosody rate='{escaped_rate}'>{escaped_text}</prosody>"
    return (
        f"<speak version='1.0' xml:lang='{language}'>"
        f"<voice name='{voice}'>{inner}</voice>"
        "</speak>"
    )


def get_speech_transcriber() -> SpeechTranscriber:
    if settings.speech_transcription_runtime == "nvidia_nim":
        return NvidiaNimSpeechTranscriber()
    return DisabledSpeechTranscriber()


def get_speech_synthesizer() -> SpeechSynthesizer:
    if settings.speech_synthesis_runtime == "azure":
        return AzureSpeechSynthesizer()
    return DisabledSpeechSynthesizer()


def transcribe_audio_bytes(audio: bytes, *, language_code: str | None = None) -> str:
    return get_speech_transcriber().transcribe(audio, language_code=language_code)


def synthesize_speech_bytes(text: str) -> bytes:
    return get_speech_synthesizer().synthesize(text)


def prepare_audio_for_riva(audio: bytes) -> tuple[bytes, int, int]:
    if is_wav(audio):
        return audio, riva_audio_encoding("LINEAR_PCM"), 16000
    if is_flac(audio):
        return audio, riva_audio_encoding("FLAC"), 16000
    if is_ogg(audio):
        return audio, riva_audio_encoding("OGGOPUS"), 16000
    return transcode_to_wav_pcm(audio), riva_audio_encoding("LINEAR_PCM"), 16000


def transcode_to_wav_pcm(audio: bytes) -> bytes:
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                "pipe:1",
            ],
            input=audio,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise SpeechTranscriptionError("Audio format is not supported") from exc
    return completed.stdout


def riva_audio_encoding(name: str) -> int:
    try:
        import riva.client
    except ImportError as exc:
        raise SpeechTranscriptionError("nvidia-riva-client is not installed") from exc
    return riva.client.AudioEncoding.Value(name)


def is_wav(audio: bytes) -> bool:
    return audio.startswith(b"RIFF") and audio[8:12] == b"WAVE"


def is_flac(audio: bytes) -> bool:
    return audio.startswith(b"fLaC")


def is_ogg(audio: bytes) -> bool:
    return audio.startswith(b"OggS")


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
