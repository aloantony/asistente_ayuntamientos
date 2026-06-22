from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.users.models import User


class TelegramUserLink(TimestampMixin, Base):
    __tablename__ = "telegram_user_links"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'revoked')",
            name="ck_telegram_user_links_status",
        ),
        UniqueConstraint("user_id", name="uq_telegram_user_links_user_id"),
        UniqueConstraint("telegram_chat_id", name="uq_telegram_user_links_chat_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    telegram_chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    telegram_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped["User"] = relationship("User")


class TelegramLinkCode(TimestampMixin, Base):
    __tablename__ = "telegram_link_codes"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'used', 'expired', 'revoked')",
            name="ck_telegram_link_codes_status",
        ),
        UniqueConstraint("code_hash", name="uq_telegram_link_codes_code_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped["User"] = relationship("User")
