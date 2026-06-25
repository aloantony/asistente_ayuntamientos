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
