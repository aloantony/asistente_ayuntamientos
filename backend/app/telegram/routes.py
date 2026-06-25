import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Annotated, Any
from urllib import error as urlerror
from urllib import request as urlrequest

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.assistant.gateway import gateway
from app.assistant.models import AssistantConversation
from app.assistant.service import run_agent_turn
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.rbac.permissions import has_permission
from app.telegram.models import TelegramLinkCode, TelegramUserLink
from app.telegram.schemas import (
    TelegramLinkCodeRead,
    TelegramLinkStatusRead,
    TelegramWebhookRead,
)
from app.users.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.get("/link", response_model=TelegramLinkStatusRead)
def get_telegram_link_status(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TelegramLinkStatusRead:
    link = get_active_link_for_user(db, current_user.id)
    if link is None:
        return TelegramLinkStatusRead(linked=False)
    return TelegramLinkStatusRead(
        linked=True,
        status=link.status,
        telegram_username=link.telegram_username,
        linked_at=link.linked_at,
        revoked_at=link.revoked_at,
    )


@router.post("/link-codes", response_model=TelegramLinkCodeRead)
def create_telegram_link_code(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TelegramLinkCodeRead:
    require_telegram_allowed(db, current_user)
    now = datetime.now(UTC)
    db.query(TelegramLinkCode).filter(
        TelegramLinkCode.user_id == current_user.id,
        TelegramLinkCode.status == "pending",
    ).update({"status": "revoked"})
    raw_code = secrets.token_hex(3).upper()
    expires_at = now + timedelta(seconds=settings.telegram_link_code_ttl_seconds)
    code = TelegramLinkCode(
        user_id=current_user.id,
        code_hash=hash_code(raw_code),
        status="pending",
        expires_at=expires_at,
    )
    db.add(code)
    db.commit()
    return TelegramLinkCodeRead(
        code=raw_code,
        expires_at=expires_at,
        ttl_seconds=settings.telegram_link_code_ttl_seconds,
    )


@router.delete("/link", response_model=TelegramLinkStatusRead)
def revoke_telegram_link(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TelegramLinkStatusRead:
    link = get_active_link_for_user(db, current_user.id)
    if link is not None:
        link.status = "revoked"
        link.revoked_at = datetime.now(UTC)
        db.commit()
    return TelegramLinkStatusRead(linked=False)


@router.post("/webhook", response_model=TelegramWebhookRead)
async def telegram_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    secret_token: Annotated[
        str | None,
        Header(alias="X-Telegram-Bot-Api-Secret-Token"),
    ] = None,
) -> TelegramWebhookRead:
    if not settings.telegram_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram is not enabled",
        )
    if settings.telegram_webhook_secret and not secrets.compare_digest(
        secret_token or "",
        settings.telegram_webhook_secret,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram webhook secret",
        )

    update = await request.json()
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return TelegramWebhookRead()
    chat = message.get("chat")
    sender = message.get("from") or {}
    text = str(message.get("text") or "").strip()
    if not isinstance(chat, dict) or not text:
        return TelegramWebhookRead()

    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return TelegramWebhookRead()

    if text.startswith("/link"):
        await handle_link_command(db, chat_id, sender, text)
        return TelegramWebhookRead()
    if text.startswith("/help") or text.startswith("/start"):
        send_telegram_message(
            chat_id,
            "Genera un código desde la sección Cuenta y envía /link CODIGO. "
            "Después podrás escribir al asistente desde este chat.",
        )
        return TelegramWebhookRead()

    link = db.scalar(
        select(TelegramUserLink)
        .options(selectinload(TelegramUserLink.user))
        .where(
            TelegramUserLink.telegram_chat_id == chat_id,
            TelegramUserLink.status == "active",
        )
    )
    if link is None or not link.user.is_active:
        send_telegram_message(
            chat_id,
            "Este chat no está vinculado. Genera un código desde la web y envía /link CODIGO.",
        )
        return TelegramWebhookRead()
    if not has_permission(link.user, "assistant.use", db):
        send_telegram_message(
            chat_id,
            "Tu usuario no tiene permiso para usar el asistente.",
        )
        return TelegramWebhookRead()

    conversation = get_or_create_telegram_conversation(db, link.user, chat_id)
    reply = run_agent_turn(db, link.user, conversation, text, gateway)
    send_telegram_message(chat_id, reply.content)
    return TelegramWebhookRead()


async def handle_link_command(
    db: Session,
    chat_id: str,
    sender: dict[str, Any],
    text: str,
) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        send_telegram_message(chat_id, "Usa /link CODIGO.")
        return
    code = db.scalar(
        select(TelegramLinkCode)
        .options(selectinload(TelegramLinkCode.user))
        .where(
            TelegramLinkCode.code_hash == hash_code(parts[1].strip()),
            TelegramLinkCode.status == "pending",
        )
    )
    now = datetime.now(UTC)
    if code is None:
        send_telegram_message(chat_id, "Código no válido o ya usado.")
        return
    if code.expires_at < now:
        code.status = "expired"
        db.commit()
        send_telegram_message(chat_id, "El código ha caducado. Genera otro desde la web.")
        return
    if not code.user.is_active or not has_permission(code.user, "assistant.use", db):
        send_telegram_message(chat_id, "El usuario no puede usar el asistente.")
        return

    existing_user_link = get_active_link_for_user(db, code.user_id)
    if existing_user_link is not None:
        existing_user_link.status = "revoked"
        existing_user_link.revoked_at = now
    existing_chat_link = db.scalar(
        select(TelegramUserLink).where(
            TelegramUserLink.telegram_chat_id == chat_id,
            TelegramUserLink.status == "active",
        )
    )
    if existing_chat_link is not None:
        existing_chat_link.status = "revoked"
        existing_chat_link.revoked_at = now

    link = TelegramUserLink(
        user_id=code.user_id,
        telegram_chat_id=chat_id,
        telegram_user_id=str(sender.get("id")) if sender.get("id") else None,
        telegram_username=sender.get("username"),
        status="active",
        linked_at=now,
    )
    code.status = "used"
    code.used_at = now
    db.add(link)
    db.commit()
    send_telegram_message(chat_id, "Telegram queda vinculado a tu usuario.")


def get_or_create_telegram_conversation(
    db: Session,
    user: User,
    chat_id: str,
) -> AssistantConversation:
    conversation = db.scalar(
        select(AssistantConversation)
        .where(
            AssistantConversation.created_by_id == user.id,
            AssistantConversation.channel == "telegram",
            AssistantConversation.external_thread_id == chat_id,
            AssistantConversation.status == "active",
        )
        .order_by(AssistantConversation.id.desc())
    )
    if conversation is not None:
        return conversation
    conversation = AssistantConversation(
        title="Telegram",
        created_by_id=user.id,
        channel="telegram",
        external_thread_id=chat_id,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def get_active_link_for_user(db: Session, user_id: int) -> TelegramUserLink | None:
    return db.scalar(
        select(TelegramUserLink).where(
            TelegramUserLink.user_id == user_id,
            TelegramUserLink.status == "active",
        )
    )


def require_telegram_allowed(db: Session, current_user: User) -> None:
    if not has_permission(current_user, "assistant.use", db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: assistant.use",
        )


def send_telegram_message(chat_id: str, text: str) -> None:
    if not settings.telegram_enabled or not settings.telegram_bot_token:
        logger.info("Telegram message skipped: telegram is not configured")
        return
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    request = urlrequest.Request(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=10):
            return
    except (urlerror.HTTPError, urlerror.URLError, TimeoutError):
        logger.warning("Telegram sendMessage failed", exc_info=True)


def hash_code(code: str) -> str:
    return sha256(code.strip().upper().encode("utf-8")).hexdigest()
