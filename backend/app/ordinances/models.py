from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.documents.models import Document
    from app.municipalities.models import Municipality
    from app.users.models import User


class Ordinance(TimestampMixin, Base):
    __tablename__ = "ordinances"
    __table_args__ = (
        CheckConstraint(
            """
            ordinance_type in (
                'ordinance',
                'regulation',
                'bylaw',
                'tax_ordinance',
                'urban_planning',
                'other'
            )
            """,
            name="ck_ordinances_ordinance_type",
        ),
        CheckConstraint(
            """
            status in (
                'active',
                'repealed',
                'partially_repealed',
                'superseded',
                'unknown',
                'archived'
            )
            """,
            name="ck_ordinances_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    municipality_id: Mapped[int] = mapped_column(
        ForeignKey("municipalities.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    topic: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    subtopic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ordinance_type: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    official_bulletin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bulletin_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approval_date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    publication_date: Mapped[date | None] = mapped_column(
        Date,
        index=True,
        nullable=True,
    )
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="unknown",
        server_default="unknown",
        nullable=False,
    )
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    municipality: Mapped["Municipality"] = relationship("Municipality")
    document: Mapped["Document | None"] = relationship("Document")
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
