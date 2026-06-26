"""create ordinance imports and telegram links

Revision ID: 20260617_0012
Revises: 20260617_0011
Create Date: 2026-06-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260617_0012"
down_revision: Union[str, None] = "20260617_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assistant_conversations",
        sa.Column(
            "channel",
            sa.String(length=30),
            server_default="web",
            nullable=False,
        ),
    )
    op.add_column(
        "assistant_conversations",
        sa.Column("external_thread_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        op.f("ix_assistant_conversations_channel"),
        "assistant_conversations",
        ["channel"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assistant_conversations_external_thread_id"),
        "assistant_conversations",
        ["external_thread_id"],
        unique=False,
    )

    op.create_table(
        "official_legal_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=2000), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_type in ('boe', 'bop', 'autonomic', 'municipal', 'other')",
            name="ck_official_legal_sources_source_type",
        ),
        sa.CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_official_legal_sources_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_official_legal_sources_domain"),
        "official_legal_sources",
        ["domain"],
        unique=True,
    )
    op.bulk_insert(
        sa.table(
            "official_legal_sources",
            sa.column("name", sa.String),
            sa.column("base_url", sa.String),
            sa.column("domain", sa.String),
            sa.column("source_type", sa.String),
            sa.column("status", sa.String),
        ),
        [
            {
                "name": "BOE Datos Abiertos",
                "base_url": "https://www.boe.es/datosabiertos/api/api.php",
                "domain": "boe.es",
                "source_type": "boe",
                "status": "active",
            },
            {
                "name": "Boletín Oficial de la Provincia de Burgos",
                "base_url": "https://bopbur.diputaciondeburgos.es/",
                "domain": "bopbur.diputaciondeburgos.es",
                "source_type": "bop",
                "status": "active",
            }
        ],
    )

    op.create_table(
        "ordinance_import_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("topic", sa.String(length=255), nullable=True),
        sa.Column("subtopic", sa.String(length=255), nullable=True),
        sa.Column("search_query", sa.Text(), nullable=True),
        sa.Column("municipality_ids_json", sa.Text(), nullable=False),
        sa.Column("official_source_ids_json", sa.Text(), nullable=False),
        sa.Column("source_urls_json", sa.Text(), nullable=False),
        sa.Column("review_criteria", sa.Text(), nullable=False),
        sa.Column(
            "source_policy",
            sa.String(length=30),
            server_default="official_only",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), server_default="draft", nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('draft', 'queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_ordinance_import_jobs_status",
        ),
        sa.CheckConstraint(
            "source_policy in ('official_only')",
            name="ck_ordinance_import_jobs_source_policy",
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ordinance_import_jobs_created_by_id"),
        "ordinance_import_jobs",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ordinance_import_jobs_status"),
        "ordinance_import_jobs",
        ["status"],
        unique=False,
    )

    op.add_column(
        "ordinances",
        sa.Column(
            "curation_status",
            sa.String(length=30),
            server_default="approved",
            nullable=False,
        ),
    )
    op.add_column("ordinances", sa.Column("import_job_id", sa.Integer(), nullable=True))
    op.add_column("ordinances", sa.Column("source_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "ordinances",
        sa.Column(
            "extraction_status",
            sa.String(length=30),
            server_default="manual",
            nullable=False,
        ),
    )
    op.add_column("ordinances", sa.Column("confidence_score", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_ordinances_curation_status",
        "ordinances",
        "curation_status in ('approved', 'pending_review', 'needs_changes', 'rejected')",
    )
    op.create_index(op.f("ix_ordinances_curation_status"), "ordinances", ["curation_status"], unique=False)
    op.create_index(op.f("ix_ordinances_import_job_id"), "ordinances", ["import_job_id"], unique=False)
    op.create_index(op.f("ix_ordinances_source_hash"), "ordinances", ["source_hash"], unique=False)
    op.create_foreign_key(
        "fk_ordinances_import_job_id_ordinance_import_jobs",
        "ordinances",
        "ordinance_import_jobs",
        ["import_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "ordinance_import_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("municipality_id", sa.Integer(), nullable=True),
        sa.Column("official_source_id", sa.Integer(), nullable=True),
        sa.Column("ordinance_id", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.String(length=2000), nullable=False),
        sa.Column("source_title", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="discovered", nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("extracted_metadata_json", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["job_id"], ["ordinance_import_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["municipality_id"], ["municipalities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["official_source_id"], ["official_legal_sources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["ordinance_id"], ["ordinances.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "source_url",
            "municipality_id",
            name="uq_ordinance_import_items_job_url_municipality",
        ),
    )
    for column in ("job_id", "municipality_id", "official_source_id", "ordinance_id", "status", "source_hash"):
        op.create_index(
            op.f(f"ix_ordinance_import_items_{column}"),
            "ordinance_import_items",
            [column],
            unique=False,
        )

    op.create_table(
        "ordinance_review_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ordinance_id", sa.Integer(), nullable=False),
        sa.Column("import_item_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="agent_reviewed", nullable=False),
        sa.Column("proposed_decision", sa.String(length=30), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("checklist_json", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("doubts", sa.Text(), nullable=True),
        sa.Column("reviewed_by_agent", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "proposed_decision in ('approve', 'needs_changes', 'reject')",
            name="ck_ordinance_review_reports_proposed_decision",
        ),
        sa.CheckConstraint(
            "status in ('agent_reviewed', 'human_approved', 'human_rejected', 'superseded')",
            name="ck_ordinance_review_reports_status",
        ),
        sa.ForeignKeyConstraint(["ordinance_id"], ["ordinances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_item_id"], ["ordinance_import_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("ordinance_id", "import_item_id", "reviewed_by_id"):
        op.create_index(
            op.f(f"ix_ordinance_review_reports_{column}"),
            "ordinance_review_reports",
            [column],
            unique=False,
        )

    op.create_table(
        "ordinance_legal_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ordinance_id", sa.Integer(), nullable=False),
        sa.Column("import_item_id", sa.Integer(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("citation", sa.String(length=500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("source_locator", sa.String(length=255), nullable=True),
        sa.Column("review_status", sa.String(length=30), server_default="pending_review", nullable=False),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("embedding_status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status in ('pending_review', 'approved', 'rejected')",
            name="ck_ordinance_legal_chunks_review_status",
        ),
        sa.CheckConstraint(
            "embedding_status in ('pending', 'ready', 'failed', 'disabled')",
            name="ck_ordinance_legal_chunks_embedding_status",
        ),
        sa.ForeignKeyConstraint(["ordinance_id"], ["ordinances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_item_id"], ["ordinance_import_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ordinance_id",
            "chunk_index",
            name="uq_ordinance_legal_chunks_ordinance_chunk_index",
        ),
    )
    for column in ("ordinance_id", "import_item_id", "review_status", "embedding_status"):
        op.create_index(
            op.f(f"ix_ordinance_legal_chunks_{column}"),
            "ordinance_legal_chunks",
            [column],
            unique=False,
        )
    # Embeddings are stored as JSON text for now because the application model
    # and local deterministic similarity code read/write string vectors. A
    # future pgvector migration should introduce a matching SQLAlchemy type and
    # database-side similarity before changing this column type.

    op.create_table(
        "telegram_user_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=100), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=100), nullable=True),
        sa.Column("telegram_username", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'revoked')",
            name="ck_telegram_user_links_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_telegram_user_links_user_id"),
        sa.UniqueConstraint("telegram_chat_id", name="uq_telegram_user_links_chat_id"),
    )
    op.create_index(op.f("ix_telegram_user_links_user_id"), "telegram_user_links", ["user_id"], unique=False)

    op.create_table(
        "telegram_link_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('pending', 'used', 'expired', 'revoked')",
            name="ck_telegram_link_codes_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash", name="uq_telegram_link_codes_code_hash"),
    )
    op.create_index(op.f("ix_telegram_link_codes_user_id"), "telegram_link_codes", ["user_id"], unique=False)
    op.create_index(op.f("ix_telegram_link_codes_expires_at"), "telegram_link_codes", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_link_codes_expires_at"), table_name="telegram_link_codes")
    op.drop_index(op.f("ix_telegram_link_codes_user_id"), table_name="telegram_link_codes")
    op.drop_table("telegram_link_codes")
    op.drop_index(op.f("ix_telegram_user_links_user_id"), table_name="telegram_user_links")
    op.drop_table("telegram_user_links")
    for column in ("embedding_status", "review_status", "import_item_id", "ordinance_id"):
        op.drop_index(op.f(f"ix_ordinance_legal_chunks_{column}"), table_name="ordinance_legal_chunks")
    op.drop_table("ordinance_legal_chunks")
    for column in ("reviewed_by_id", "import_item_id", "ordinance_id"):
        op.drop_index(op.f(f"ix_ordinance_review_reports_{column}"), table_name="ordinance_review_reports")
    op.drop_table("ordinance_review_reports")
    for column in ("source_hash", "status", "ordinance_id", "official_source_id", "municipality_id", "job_id"):
        op.drop_index(op.f(f"ix_ordinance_import_items_{column}"), table_name="ordinance_import_items")
    op.drop_table("ordinance_import_items")
    op.drop_constraint("fk_ordinances_import_job_id_ordinance_import_jobs", "ordinances", type_="foreignkey")
    op.drop_index(op.f("ix_ordinances_source_hash"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_import_job_id"), table_name="ordinances")
    op.drop_index(op.f("ix_ordinances_curation_status"), table_name="ordinances")
    op.drop_constraint("ck_ordinances_curation_status", "ordinances", type_="check")
    op.drop_column("ordinances", "confidence_score")
    op.drop_column("ordinances", "extraction_status")
    op.drop_column("ordinances", "source_hash")
    op.drop_column("ordinances", "import_job_id")
    op.drop_column("ordinances", "curation_status")
    op.drop_index(op.f("ix_ordinance_import_jobs_status"), table_name="ordinance_import_jobs")
    op.drop_index(op.f("ix_ordinance_import_jobs_created_by_id"), table_name="ordinance_import_jobs")
    op.drop_table("ordinance_import_jobs")
    op.drop_index(op.f("ix_official_legal_sources_domain"), table_name="official_legal_sources")
    op.drop_table("official_legal_sources")
    op.drop_index(op.f("ix_assistant_conversations_external_thread_id"), table_name="assistant_conversations")
    op.drop_index(op.f("ix_assistant_conversations_channel"), table_name="assistant_conversations")
    op.drop_column("assistant_conversations", "external_thread_id")
    op.drop_column("assistant_conversations", "channel")
