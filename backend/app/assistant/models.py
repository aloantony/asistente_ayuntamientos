from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User


class AssistantConversation(TimestampMixin, Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_assistant_conversations_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="active",
        server_default="active",
        nullable=False,
    )
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    created_by: Mapped["User"] = relationship("User")
    messages: Mapped[list["AssistantMessage"]] = relationship(
        "AssistantMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.id",
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint(
            "role in ('user', 'assistant')",
            name="ck_assistant_messages_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON-encoded audit trail of the tool calls the agent executed while
    # producing this message: [{tool, input, result_summary, ok}, ...]
    actions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    conversation: Mapped["AssistantConversation"] = relationship(
        "AssistantConversation",
        back_populates="messages",
    )


class AssistantMemoryEntry(TimestampMixin, Base):
    __tablename__ = "assistant_memory_entries"
    __table_args__ = (
        CheckConstraint(
            "status in ('proposed', 'approved', 'rejected', 'archived', 'blocked')",
            name="ck_assistant_memory_entries_status",
        ),
        CheckConstraint(
            "category in ('protocol', 'preference', 'context', 'decision', 'open_question')",
            name="ck_assistant_memory_entries_category",
        ),
        CheckConstraint(
            "sensitivity in ('normal', 'personal', 'sensitive', 'legal')",
            name="ck_assistant_memory_entries_sensitivity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="proposed",
        server_default="proposed",
        nullable=False,
    )
    sensitivity: Mapped[str] = mapped_column(
        String(30),
        default="normal",
        server_default="normal",
        nullable=False,
    )
    source_conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    proposed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    source_conversation: Mapped["AssistantConversation | None"] = relationship(
        "AssistantConversation",
        foreign_keys=[source_conversation_id],
    )
    source_message: Mapped["AssistantMessage | None"] = relationship(
        "AssistantMessage",
        foreign_keys=[source_message_id],
    )
    proposed_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[proposed_by_id],
    )
    reviewed_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[reviewed_by_id],
    )
