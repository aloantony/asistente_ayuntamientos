from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.projects.models import Project
    from app.users.models import User


class Requirement(TimestampMixin, Base):
    __tablename__ = "requirements"
    __table_args__ = (
        CheckConstraint(
            "priority in ('low', 'medium', 'high', 'urgent')",
            name="ck_requirements_priority",
        ),
        CheckConstraint(
            """
            status in (
                'draft',
                'submitted',
                'in_review',
                'needs_clarification',
                'accepted',
                'rejected',
                'converted',
                'archived'
            )
            """,
            name="ck_requirements_status",
        ),
        CheckConstraint(
            "source_type in ('manual', 'conversation', 'phone_call', 'meeting', 'other')",
            name="ck_requirements_source_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    problem: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_process: Mapped[str | None] = mapped_column(Text, nullable=True)
    desired_process: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_users: Mapped[str | None] = mapped_column(Text, nullable=True)
    involved_documents: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_sensitivity_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(20),
        default="medium",
        server_default="medium",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="draft",
        server_default="draft",
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        String(30),
        default="manual",
        server_default="manual",
        nullable=False,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    project: Mapped["Project | None"] = relationship("Project")
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    reviewed_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[reviewed_by_id],
    )
    messages: Mapped[list["RequirementMessage"]] = relationship(
        "RequirementMessage",
        back_populates="requirement",
        cascade="all, delete-orphan",
    )


class RequirementMessage(Base):
    __tablename__ = "requirement_messages"
    __table_args__ = (
        CheckConstraint(
            "message_type in ('note', 'question', 'answer', 'clarification', 'decision')",
            name="ck_requirement_messages_message_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    requirement_id: Mapped[int] = mapped_column(
        ForeignKey("requirements.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(
        String(30),
        default="note",
        server_default="note",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    requirement: Mapped["Requirement"] = relationship(
        "Requirement",
        back_populates="messages",
    )
    author: Mapped["User | None"] = relationship("User")
