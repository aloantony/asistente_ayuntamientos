from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.assistant.models import AssistantConversation, AssistantMessage
    from app.organizations.models import Organization
    from app.users.models import User


CANVAS_DOCUMENT_TYPES = (
    "municipal_ordinance",
    "regulation",
    "report",
    "letter",
    "minutes",
    "other",
)
CANVAS_DOCUMENT_STATUSES = ("draft", "archived")
CANVAS_REVISION_SOURCES = ("user", "assistant", "restore")


class AssistantCanvasDocument(TimestampMixin, Base):
    __tablename__ = "assistant_canvas_documents"
    __table_args__ = (
        CheckConstraint(
            "document_type in "
            "('municipal_ordinance', 'regulation', 'report', 'letter', "
            "'minutes', 'other')",
            name="ck_assistant_canvas_documents_type",
        ),
        CheckConstraint(
            "status in ('draft', 'archived')",
            name="ck_assistant_canvas_documents_status",
        ),
        CheckConstraint(
            "current_revision >= 1",
            name="ck_assistant_canvas_documents_current_revision",
        ),
        CheckConstraint(
            "(creation_id is null and creation_payload_sha256 is null) or "
            "(creation_id is not null and "
            "creation_payload_sha256 is not null and "
            "creation_payload_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_assistant_canvas_documents_creation_payload",
        ),
        UniqueConstraint(
            "conversation_id",
            "creation_id",
            name="uq_assistant_canvas_documents_conversation_creation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    document_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="draft",
        server_default="draft",
        nullable=False,
    )
    current_revision: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    creation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    creation_payload_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    conversation: Mapped["AssistantConversation"] = relationship(
        "AssistantConversation",
        back_populates="canvas_documents",
    )
    organization: Mapped["Organization | None"] = relationship("Organization")
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
    source_message: Mapped["AssistantMessage | None"] = relationship(
        "AssistantMessage",
        foreign_keys=[source_message_id],
    )
    revisions: Mapped[list["AssistantCanvasRevision"]] = relationship(
        "AssistantCanvasRevision",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="AssistantCanvasRevision.revision_number",
    )


class AssistantCanvasRevision(Base):
    __tablename__ = "assistant_canvas_revisions"
    __table_args__ = (
        CheckConstraint(
            "revision_number >= 1",
            name="ck_assistant_canvas_revisions_number",
        ),
        CheckConstraint(
            "edit_source in ('user', 'assistant', 'restore')",
            name="ck_assistant_canvas_revisions_source",
        ),
        CheckConstraint(
            "(mutation_id is null and mutation_payload_sha256 is null) or "
            "(mutation_id is not null and "
            "mutation_payload_sha256 is not null and "
            "mutation_payload_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_assistant_canvas_revisions_mutation_payload",
        ),
        UniqueConstraint(
            "document_id",
            "revision_number",
            name="uq_assistant_canvas_revisions_document_number",
        ),
        UniqueConstraint(
            "document_id",
            "mutation_id",
            name="uq_assistant_canvas_revisions_document_mutation",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_canvas_documents.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_source: Mapped[str] = mapped_column(String(20), nullable=False)
    mutation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mutation_payload_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    source_tool_call_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    document: Mapped["AssistantCanvasDocument"] = relationship(
        "AssistantCanvasDocument",
        back_populates="revisions",
    )
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    source_message: Mapped["AssistantMessage | None"] = relationship(
        "AssistantMessage",
        foreign_keys=[source_message_id],
    )
