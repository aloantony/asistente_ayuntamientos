from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
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
    from app.organizations.models import Organization
    from app.requirements.models import Requirement
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
    channel: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="web",
        server_default="web",
        nullable=False,
    )
    external_thread_id: Mapped[str | None] = mapped_column(
        String(255),
        index=True,
        nullable=True,
    )
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_conversation_folders.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # JSON-encoded private assistant state for deterministic follow-ups such
    # as selected organization and pending confirmations.
    state: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped["User"] = relationship("User")
    folder: Mapped["AssistantConversationFolder | None"] = relationship(
        "AssistantConversationFolder",
        back_populates="conversations",
    )
    messages: Mapped[list["AssistantMessage"]] = relationship(
        "AssistantMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.id",
    )


class AssistantConversationFolder(TimestampMixin, Base):
    __tablename__ = "assistant_conversation_folders"
    __table_args__ = (
        UniqueConstraint(
            "created_by_id",
            "name",
            name="uq_assistant_conversation_folders_user_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )

    created_by: Mapped["User"] = relationship("User")
    conversations: Mapped[list[AssistantConversation]] = relationship(
        "AssistantConversation",
        back_populates="folder",
        passive_deletes=True,
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
    agent_key: Mapped[str | None] = mapped_column(
        String(100),
        index=True,
        nullable=True,
    )
    # JSON-encoded routing metadata kept for historical messages. New
    # Anacleto v2 messages use routing=None.
    routing: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class AssistantAdminFeedback(TimestampMixin, Base):
    __tablename__ = "assistant_admin_feedback"
    __table_args__ = (
        CheckConstraint(
            "status in ('submitted', 'reviewed', 'dismissed', 'archived')",
            name="ck_assistant_admin_feedback_status",
        ),
        CheckConstraint(
            "category in ('bug', 'improvement', 'missing_capability', 'data_issue', 'ux', 'other')",
            name="ck_assistant_admin_feedback_category",
        ),
        CheckConstraint(
            "priority in ('low', 'medium', 'high', 'urgent')",
            name="ck_assistant_admin_feedback_priority",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(20),
        default="medium",
        server_default="medium",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="submitted",
        server_default="submitted",
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
    submitted_by_id: Mapped[int | None] = mapped_column(
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

    organization: Mapped["Organization | None"] = relationship("Organization")
    source_conversation: Mapped["AssistantConversation | None"] = relationship(
        "AssistantConversation",
        foreign_keys=[source_conversation_id],
    )
    source_message: Mapped["AssistantMessage | None"] = relationship(
        "AssistantMessage",
        foreign_keys=[source_message_id],
    )
    submitted_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[submitted_by_id],
    )
    reviewed_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[reviewed_by_id],
    )


class AssistantTransversalFeature(TimestampMixin, Base):
    __tablename__ = "assistant_transversal_features"
    __table_args__ = (
        CheckConstraint(
            """
            status in (
                'proposed',
                'approved',
                'developed',
                'available',
                'rejected',
                'archived',
                'blocked'
            )
            """,
            name="ck_assistant_transversal_features_status",
        ),
        CheckConstraint(
            """
            category in (
                'process',
                'compliance',
                'automation',
                'documents',
                'citizen_service',
                'other'
            )
            """,
            name="ck_assistant_transversal_features_category",
        ),
        CheckConstraint(
            "sensitivity in ('normal', 'personal', 'sensitive', 'legal')",
            name="ck_assistant_transversal_features_sensitivity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_requirement_id: Mapped[int | None] = mapped_column(
        ForeignKey("requirements.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
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
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
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
    auto_activatable: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
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

    source_requirement: Mapped["Requirement | None"] = relationship("Requirement")
    source_organization: Mapped["Organization"] = relationship("Organization")
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
    adoptions: Mapped[list["AssistantTransversalFeatureAdoption"]] = relationship(
        "AssistantTransversalFeatureAdoption",
        back_populates="feature",
    )


class AssistantTransversalFeatureAdoption(TimestampMixin, Base):
    __tablename__ = "assistant_transversal_feature_adoptions"
    __table_args__ = (
        CheckConstraint(
            "status in ('suggested', 'accepted', 'activation_pending', 'active', 'rejected', 'paused')",
            name="ck_assistant_transversal_feature_adoptions_status",
        ),
        UniqueConstraint(
            "feature_id",
            "organization_id",
            name="uq_assistant_transversal_feature_adoptions_feature_org",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_transversal_features.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="suggested",
        server_default="suggested",
        nullable=False,
    )
    requested_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
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
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    feature: Mapped["AssistantTransversalFeature"] = relationship(
        "AssistantTransversalFeature",
        back_populates="adoptions",
    )
    organization: Mapped["Organization"] = relationship("Organization")
    requested_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[requested_by_id],
    )
    approved_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[approved_by_id],
    )
    source_conversation: Mapped["AssistantConversation | None"] = relationship(
        "AssistantConversation",
        foreign_keys=[source_conversation_id],
    )
    source_message: Mapped["AssistantMessage | None"] = relationship(
        "AssistantMessage",
        foreign_keys=[source_message_id],
    )
