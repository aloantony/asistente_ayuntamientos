"""Voice turn orchestration for Anacleto."""

from collections.abc import Generator

from sqlalchemy.orm import Session

from app.assistant.gateway import AIGateway
from app.assistant.models import AssistantConversation
from app.assistant.speech import SpeechTranscriptionError, transcribe_audio_bytes
from app.assistant.turn import TurnEvent, run_agent_turn_events
from app.core.config import settings
from app.users.models import User


def run_voice_turn_events(
    db: Session,
    current_user: User,
    conversation: AssistantConversation,
    audio: bytes,
    gateway: AIGateway,
) -> Generator[TurnEvent, None, None]:
    """Transcribe user audio, run one assistant turn and emit voice-aware events."""
    yield TurnEvent("voice_state", {"state": "transcribing"})

    try:
        transcript = transcribe_audio_bytes(
            audio,
            language_code=settings.speech_transcription_language_code,
        ).strip()
    except SpeechTranscriptionError:
        yield TurnEvent("error", {"detail": "Audio transcription is not available"})
        return

    if not transcript:
        yield TurnEvent("error", {"detail": "Audio transcription returned no text"})
        return

    yield TurnEvent("transcript_final", {"text": transcript})
    yield TurnEvent("voice_state", {"state": "thinking"})

    responding = False
    terminal_event: TurnEvent | None = None
    for event in run_agent_turn_events(
        db,
        current_user,
        conversation,
        transcript,
        gateway,
        input_mode="voice",
    ):
        if event.type in {"done", "error"}:
            terminal_event = event
            continue
        if event.type == "tool_activity":
            if event.data.get("status") == "started":
                yield TurnEvent("voice_state", {"state": "tool_running"})
            elif event.data.get("status") == "finished":
                yield TurnEvent("voice_state", {"state": "thinking"})
        elif event.type == "text_delta" and not responding:
            responding = True
            yield TurnEvent("voice_state", {"state": "responding"})
        yield event

    yield TurnEvent("voice_state", {"state": "done"})
    yield terminal_event or TurnEvent("error", {"detail": "Assistant request failed"})
