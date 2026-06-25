from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TelegramLinkStatusRead(BaseModel):
    linked: bool
    status: str | None = None
    telegram_username: str | None = None
    linked_at: datetime | None = None
    revoked_at: datetime | None = None


class TelegramLinkCodeRead(BaseModel):
    code: str
    expires_at: datetime
    ttl_seconds: int


class TelegramWebhookRead(BaseModel):
    ok: bool = True

    model_config = ConfigDict(from_attributes=True)
