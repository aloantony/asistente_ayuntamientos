from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
        CheckConstraint(
            "curation_status in ('approved', 'pending_review', 'needs_changes', 'rejected')",
            name="ck_ordinances_curation_status",
        ),
        CheckConstraint(
            """
            legal_review_status in (
                'pending_review',
                'human_approved',
                'human_rejected'
            )
            """,
            name="ck_ordinances_legal_review_status",
        ),
        CheckConstraint(
            """
            (
                legal_review_status = 'pending_review'
                and legal_reviewed_by_id is null
                and legal_reviewed_at is null
            ) or (
                legal_review_status = 'human_approved'
                and curation_status = 'approved'
                and legal_reviewed_at is not null
            ) or (
                legal_review_status = 'human_rejected'
                and curation_status = 'rejected'
                and legal_reviewed_at is not null
            )
            """,
            name="ck_ordinances_legal_review_audit",
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
    curation_status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="pending_review",
        server_default="pending_review",
        nullable=False,
    )
    import_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("ordinance_import_jobs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_hash: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
        nullable=True,
    )
    extraction_status: Mapped[str] = mapped_column(
        String(30),
        default="manual",
        server_default="manual",
        nullable=False,
    )
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_review_status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="pending_review",
        server_default="pending_review",
        nullable=False,
    )
    legal_reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    legal_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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

    municipality: Mapped["Municipality"] = relationship("Municipality")
    document: Mapped["Document | None"] = relationship("Document")
    import_job: Mapped["OrdinanceImportJob | None"] = relationship(
        "OrdinanceImportJob",
        foreign_keys=[import_job_id],
    )
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
    legal_reviewed_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[legal_reviewed_by_id],
    )
    legal_chunks: Mapped[list["OrdinanceLegalChunk"]] = relationship(
        "OrdinanceLegalChunk",
        back_populates="ordinance",
    )


class OfficialLegalSource(TimestampMixin, Base):
    __tablename__ = "official_legal_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type in ('boe', 'bop', 'autonomic', 'municipal', 'other')",
            name="ck_official_legal_sources_source_type",
        ),
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_official_legal_sources_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrdinanceImportJob(TimestampMixin, Base):
    __tablename__ = "ordinance_import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft', 'queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_ordinance_import_jobs_status",
        ),
        CheckConstraint(
            "source_policy in ('official_only')",
            name="ck_ordinance_import_jobs_source_policy",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subtopic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    search_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    municipality_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    official_source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_urls_json: Mapped[str] = mapped_column(Text, nullable=False)
    review_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    source_policy: Mapped[str] = mapped_column(
        String(30),
        default="official_only",
        server_default="official_only",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="draft",
        server_default="draft",
        nullable=False,
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped["User | None"] = relationship("User")
    items: Mapped[list["OrdinanceImportItem"]] = relationship(
        "OrdinanceImportItem",
        back_populates="job",
        cascade="all, delete-orphan",
    )


class OrdinanceImportItem(TimestampMixin, Base):
    __tablename__ = "ordinance_import_items"
    __table_args__ = (
        CheckConstraint(
            """
            status in (
                'discovered',
                'fetching',
                'extracted',
                'pending_review',
                'approved',
                'rejected',
                'duplicate',
                'failed'
            )
            """,
            name="ck_ordinance_import_items_status",
        ),
        UniqueConstraint(
            "job_id",
            "source_url",
            "municipality_id",
            name="uq_ordinance_import_items_job_url_municipality",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("ordinance_import_jobs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    municipality_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipalities.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    official_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("official_legal_sources.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    ordinance_id: Mapped[int | None] = mapped_column(
        ForeignKey("ordinances.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="discovered",
        server_default="discovered",
        nullable=False,
    )
    source_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    job: Mapped["OrdinanceImportJob"] = relationship(
        "OrdinanceImportJob",
        back_populates="items",
    )
    municipality: Mapped["Municipality | None"] = relationship("Municipality")
    official_source: Mapped["OfficialLegalSource | None"] = relationship(
        "OfficialLegalSource"
    )
    ordinance: Mapped["Ordinance | None"] = relationship(
        "Ordinance",
        foreign_keys=[ordinance_id],
    )
    review_reports: Mapped[list["OrdinanceReviewReport"]] = relationship(
        "OrdinanceReviewReport",
        back_populates="import_item",
    )


class OrdinanceReviewReport(TimestampMixin, Base):
    __tablename__ = "ordinance_review_reports"
    __table_args__ = (
        CheckConstraint(
            "proposed_decision in ('approve', 'needs_changes', 'reject')",
            name="ck_ordinance_review_reports_proposed_decision",
        ),
        CheckConstraint(
            "status in ('agent_reviewed', 'human_approved', 'human_rejected', 'superseded')",
            name="ck_ordinance_review_reports_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ordinance_id: Mapped[int] = mapped_column(
        ForeignKey("ordinances.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    import_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("ordinance_import_items.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="agent_reviewed",
        server_default="agent_reviewed",
        nullable=False,
    )
    proposed_decision: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    checklist_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    doubts: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_agent: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ordinance: Mapped["Ordinance"] = relationship("Ordinance")
    import_item: Mapped["OrdinanceImportItem | None"] = relationship(
        "OrdinanceImportItem",
        back_populates="review_reports",
    )
    reviewed_by: Mapped["User | None"] = relationship("User")


class OrdinanceLegalChunk(TimestampMixin, Base):
    __tablename__ = "ordinance_legal_chunks"
    __table_args__ = (
        CheckConstraint(
            "review_status in ('pending_review', 'approved', 'rejected')",
            name="ck_ordinance_legal_chunks_review_status",
        ),
        CheckConstraint(
            "embedding_status in ('pending', 'ready', 'failed', 'disabled')",
            name="ck_ordinance_legal_chunks_embedding_status",
        ),
        UniqueConstraint(
            "ordinance_id",
            "chunk_index",
            name="uq_ordinance_legal_chunks_ordinance_chunk_index",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ordinance_id: Mapped[int] = mapped_column(
        ForeignKey("ordinances.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    import_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("ordinance_import_items.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    citation: Mapped[str | None] = mapped_column(String(500), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    source_locator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="pending_review",
        server_default="pending_review",
        nullable=False,
    )
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="pending",
        server_default="pending",
        nullable=False,
    )
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ordinance: Mapped["Ordinance"] = relationship(
        "Ordinance",
        back_populates="legal_chunks",
    )
    import_item: Mapped["OrdinanceImportItem | None"] = relationship(
        "OrdinanceImportItem"
    )
