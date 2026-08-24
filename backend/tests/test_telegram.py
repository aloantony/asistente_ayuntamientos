from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import settings
from app.telegram.models import TelegramLinkCode, TelegramUserLink
from app.telegram import routes as telegram_routes
from conftest import headers_for


def test_telegram_link_code_requires_assistant_permission(
    client, make_user
):
    user = make_user()

    response = client.post(
        "/telegram/link-codes",
        headers=headers_for(user),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Permission required: assistant.use"


def test_telegram_webhook_links_existing_user_with_one_time_code(
    client,
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(telegram_routes, "send_telegram_message", lambda *_: None)
    user = make_user()
    grant_permissions(user, make_organization(), ["assistant.use"])

    code_response = client.post(
        "/telegram/link-codes",
        headers=headers_for(user),
    )
    assert code_response.status_code == 200
    code = code_response.json()["code"]

    linked = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={
            "message": {
                "chat": {"id": 12345},
                "from": {"id": 67890, "username": "alcaldia"},
                "text": f"/link {code}",
            }
        },
    )

    assert linked.status_code == 200
    link = db.scalar(select(TelegramUserLink))
    assert link is not None
    assert link.user_id == user.id
    assert link.telegram_chat_id == "12345"
    assert link.telegram_username == "alcaldia"
    stored_code = db.scalar(select(TelegramLinkCode))
    assert stored_code.status == "used"

    reused = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={
            "message": {
                "chat": {"id": 999},
                "from": {"id": 999},
                "text": f"/link {code}",
            }
        },
    )

    assert reused.status_code == 200
    assert len(list(db.scalars(select(TelegramUserLink)))) == 1


def test_telegram_webhook_rejects_invalid_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={"message": {"chat": {"id": 1}, "text": "/start"}},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Telegram webhook secret"


def test_telegram_webhook_fails_closed_without_a_secret(client, monkeypatch):
    """Sin secreto la verificación se saltaba entera y quedaba abierta (ADR-036)."""
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_webhook_secret", None)

    response = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 1}, "text": "/start"}},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Telegram webhook secret is not configured"


def test_telegram_webhook_transcribes_voice_message_for_linked_user(
    client,
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "telegram-token")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "speech_transcription_runtime", "nvidia_nim")

    sent_messages = []
    monkeypatch.setattr(
        telegram_routes,
        "send_telegram_message",
        lambda chat_id, text: sent_messages.append((chat_id, text)),
    )
    monkeypatch.setattr(
        telegram_routes,
        "download_telegram_file",
        lambda file_id: b"telegram-audio-bytes",
    )
    monkeypatch.setattr(
        telegram_routes,
        "transcribe_audio_bytes",
        lambda audio, language_code=None: "Necesito revisar la ordenanza de terrazas",
    )

    user = make_user()
    grant_permissions(user, make_organization(), ["assistant.use"])
    db.add(
        TelegramUserLink(
            user_id=user.id,
            telegram_chat_id="12345",
            telegram_user_id="67890",
            telegram_username="alcaldia",
            status="active",
            linked_at=datetime.now(UTC),
        )
    )
    db.commit()

    agent_inputs = []
    input_modes = []

    def fake_run_agent_turn(
        db_session,
        user_arg,
        conversation,
        content,
        agent_gateway,
        *,
        input_mode="text",
    ):
        agent_inputs.append(content)
        input_modes.append(input_mode)
        return SimpleNamespace(content="Respuesta desde Anacleto")

    monkeypatch.setattr(telegram_routes, "run_agent_turn", fake_run_agent_turn)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={
            "message": {
                "chat": {"id": 12345},
                "from": {"id": 67890, "username": "alcaldia"},
                "voice": {"file_id": "voice-file-id", "duration": 4},
            }
        },
    )

    assert response.status_code == 200
    assert agent_inputs == ["Necesito revisar la ordenanza de terrazas"]
    assert input_modes == ["voice"]
    assert sent_messages == [("12345", "Respuesta desde Anacleto")]


def test_telegram_webhook_reports_voice_transcription_unavailable(
    client,
    db,
    monkeypatch,
    make_user,
    make_organization,
    grant_permissions,
):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "telegram-token")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "secret")
    monkeypatch.setattr(settings, "speech_transcription_runtime", "disabled")

    sent_messages = []
    monkeypatch.setattr(
        telegram_routes,
        "send_telegram_message",
        lambda chat_id, text: sent_messages.append((chat_id, text)),
    )
    monkeypatch.setattr(
        telegram_routes,
        "run_agent_turn",
        lambda *_, **__: (_ for _ in ()).throw(
            AssertionError("agent should not run")
        ),
    )

    user = make_user()
    grant_permissions(user, make_organization(), ["assistant.use"])
    db.add(
        TelegramUserLink(
            user_id=user.id,
            telegram_chat_id="12345",
            status="active",
            linked_at=datetime.now(UTC),
        )
    )
    db.commit()

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={
            "message": {
                "chat": {"id": 12345},
                "from": {"id": 67890},
                "voice": {"file_id": "voice-file-id", "duration": 4},
            }
        },
    )

    assert response.status_code == 200
    assert sent_messages == [
        (
            "12345",
            "Ahora mismo no puedo transcribir audios. Escribe el mensaje en texto y lo reviso.",
        )
    ]
