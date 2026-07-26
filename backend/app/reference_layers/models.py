from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User


class ReferenceCatalogSnapshot(TimestampMixin, Base):
    __tablename__ = "reference_catalog_snapshots"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_catalog_snapshots_provider_nonempty",
        ),
        CheckConstraint(
            "source_url like 'https://%'",
            name="ck_reference_catalog_snapshots_source_https",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_catalog_snapshots_sha256",
        ),
        CheckConstraint(
            "definition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_catalog_snapshots_definition_sha256",
        ),
        CheckConstraint(
            "service_count >= 0 and group_count >= 0 and layer_count >= 0 "
            "and unresolved_count >= 0",
            name="ck_reference_catalog_snapshots_counts",
        ),
        CheckConstraint(
            "status in ('validated', 'applied', 'rejected')",
            name="ck_reference_catalog_snapshots_status",
        ),
        CheckConstraint(
            "not is_current or status = 'applied'",
            name="ck_reference_catalog_snapshots_current_applied",
        ),
        UniqueConstraint(
            "provider_key",
            "content_sha256",
            "definition_sha256",
            name="uq_reference_catalog_snapshots_provider_hashes",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_catalog_snapshots_provider_id",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            "definition_sha256",
            name=(
                "uq_reference_catalog_snapshots_provider_id_definition"
            ),
        ),
        Index(
            "uq_reference_catalog_snapshots_current_provider",
            "provider_key",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_catalog_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    normalized_definition_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    service_count: Mapped[int] = mapped_column(Integer, nullable=False)
    group_count: Mapped[int] = mapped_column(Integer, nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    services: Mapped[list["ReferenceService"]] = relationship(
        "ReferenceService",
        back_populates="last_seen_snapshot",
        foreign_keys="ReferenceService.last_seen_snapshot_id",
        primaryjoin=(
            "ReferenceCatalogSnapshot.id == "
            "ReferenceService.last_seen_snapshot_id"
        ),
    )
    layers: Mapped[list["ReferenceLayer"]] = relationship(
        "ReferenceLayer",
        back_populates="last_seen_snapshot",
        foreign_keys="ReferenceLayer.last_seen_snapshot_id",
        primaryjoin=(
            "ReferenceCatalogSnapshot.id == "
            "ReferenceLayer.last_seen_snapshot_id"
        ),
    )
    styles: Mapped[list["ReferenceLayerStyle"]] = relationship(
        "ReferenceLayerStyle",
        back_populates="last_seen_snapshot",
        foreign_keys="ReferenceLayerStyle.last_seen_snapshot_id",
        primaryjoin=(
            "ReferenceCatalogSnapshot.id == "
            "ReferenceLayerStyle.last_seen_snapshot_id"
        ),
    )


class ReferenceCatalogObservedVersion(Base):
    """Deduplicated, immutable SIUR catalog bytes awaiting human review."""

    __tablename__ = "reference_catalog_observed_versions"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_catalog_observed_versions_provider_nonempty",
        ),
        CheckConstraint(
            "source_url like 'https://%' and final_url like 'https://%'",
            name="ck_reference_catalog_observed_versions_urls_https",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$' and "
            "raw_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_catalog_observed_versions_hashes",
        ),
        CheckConstraint(
            "size_bytes > 0 and size_bytes <= 2097152",
            name="ck_reference_catalog_observed_versions_size",
        ),
        CheckConstraint(
            "length(source_url) <= 8192 and length(final_url) <= 8192 and "
            "octet_length(raw_catalog_json::text) <= 16777216 and "
            "octet_length(analysis_json::text) <= 1048576",
            name="ck_reference_catalog_observed_versions_bounds",
        ),
        UniqueConstraint(
            "provider_key",
            "content_sha256",
            name="uq_reference_catalog_observed_versions_content",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_catalog_observed_versions_provider_id",
        ),
        Index(
            "ix_reference_catalog_observed_versions_retrieved",
            "provider_key",
            "retrieved_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_catalog_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    analysis_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    checks: Mapped[list["ReferenceCatalogUpdateCheck"]] = relationship(
        "ReferenceCatalogUpdateCheck",
        back_populates="observed_version",
    )


class ReferenceCatalogUpdateCheck(Base):
    """Immutable evidence for one completed conditional catalog check."""

    __tablename__ = "reference_catalog_update_checks"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(idempotency_key) <> ''",
            name="ck_reference_catalog_update_checks_identity_nonempty",
        ),
        CheckConstraint(
            "source_url like 'https://%' and "
            "(response_final_url is null or response_final_url like 'https://%')",
            name="ck_reference_catalog_update_checks_urls_https",
        ),
        CheckConstraint(
            "trigger_kind in ('scheduled', 'manual')",
            name="ck_reference_catalog_update_checks_trigger_kind",
        ),
        CheckConstraint(
            "status in ('unchanged', 'update_available', 'error')",
            name="ck_reference_catalog_update_checks_status",
        ),
        CheckConstraint(
            "(response_raw_sha256 is null or "
            "response_raw_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_catalog_update_checks_hash",
        ),
        CheckConstraint(
            "response_size_bytes >= 0 and response_size_bytes <= 2097152 "
            "and duration_ms >= 0 and next_check_at > checked_at",
            name="ck_reference_catalog_update_checks_measurements",
        ),
        CheckConstraint(
            "(status in ('unchanged', 'update_available') and "
            "observed_version_id is not null and "
            "http_status is not null and http_status in (200, 304) and "
            "error_code is null and "
            "error_message is null and error_retryable is null) or "
            "(status = 'error' and observed_version_id is null and "
            "error_code is not null and error_message is not null and "
            "btrim(error_code) <> '' and btrim(error_message) <> '' and "
            "error_retryable is not null)",
            name="ck_reference_catalog_update_checks_result_shape",
        ),
        CheckConstraint(
            "(not_modified and http_status = 304 and "
            "response_size_bytes = 0 and response_raw_sha256 is null) or "
            "(not not_modified and (http_status is null or http_status <> 304))",
            name="ck_reference_catalog_update_checks_not_modified",
        ),
        CheckConstraint(
            "length(idempotency_key) <= 128 and length(source_url) <= 8192 and "
            "(response_final_url is null or "
            "length(response_final_url) <= 8192) and "
            "(request_etag is null or length(request_etag) <= 4096) and "
            "(request_last_modified is null or "
            "length(request_last_modified) <= 4096) and "
            "(response_etag is null or length(response_etag) <= 4096) and "
            "(response_last_modified is null or "
            "length(response_last_modified) <= 4096) and "
            "(error_message is null or length(error_message) <= 4096) and "
            "octet_length(response_redirect_chain_json::text) <= 65536",
            name="ck_reference_catalog_update_checks_bounds",
        ),
        UniqueConstraint(
            "provider_key",
            "idempotency_key",
            name="uq_reference_catalog_update_checks_idempotency",
        ),
        ForeignKeyConstraint(
            ["provider_key", "baseline_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_catalog_update_checks_baseline",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "observed_version_id"],
            [
                "reference_catalog_observed_versions.provider_key",
                "reference_catalog_observed_versions.id",
            ],
            name="fk_reference_catalog_update_checks_observed",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_catalog_update_checks_latest",
            "provider_key",
            "checked_at",
            "id",
        ),
        Index(
            "ix_reference_catalog_update_checks_status",
            "provider_key",
            "status",
            "checked_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_snapshot_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    observed_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    request_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_last_modified: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    http_status: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    not_modified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    response_final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_last_modified: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    response_size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    response_raw_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    response_redirect_chain_json: Mapped[list[str]] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'::json"),
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_retryable: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    baseline_snapshot: Mapped[ReferenceCatalogSnapshot | None] = relationship(
        "ReferenceCatalogSnapshot",
        foreign_keys=[baseline_snapshot_id],
    )
    observed_version: Mapped[
        ReferenceCatalogObservedVersion | None
    ] = relationship(
        "ReferenceCatalogObservedVersion",
        back_populates="checks",
        foreign_keys=[observed_version_id],
    )


class ReferenceService(TimestampMixin, Base):
    __tablename__ = "reference_services"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> '' "
            "and btrim(title) <> ''",
            name="ck_reference_services_identity_nonempty",
        ),
        CheckConstraint(
            "upstream_protocol in "
            "('wms', 'wfs', 'wmts', 'xyz', 'arcgis_rest', 'local')",
            name="ck_reference_services_protocol",
        ),
        CheckConstraint(
            "base_url ~ '^https?://' and "
            "(capabilities_url is null or capabilities_url ~ '^https?://') "
            "and (license_url is null or license_url ~ '^https?://')",
            name="ck_reference_services_urls",
        ),
        CheckConstraint(
            "license_status in ('pending', 'approved', 'restricted')",
            name="ck_reference_services_license_status",
        ),
        CheckConstraint(
            "cache_policy in ('none', 'on_demand', 'mirror')",
            name="ck_reference_services_cache_policy",
        ),
        CheckConstraint(
            "status in ('active', 'degraded', 'missing', 'disabled')",
            name="ck_reference_services_status",
        ),
        CheckConstraint(
            "capabilities_sha256 is null or "
            "capabilities_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_services_capabilities_sha256",
        ),
        UniqueConstraint(
            "provider_key",
            "source_key",
            name="uq_reference_services_provider_source",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_services_provider_id",
        ),
        ForeignKeyConstraint(
            ["provider_key", "last_seen_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_services_provider_snapshot",
            ondelete="RESTRICT",
        ),
        Index("ix_reference_services_snapshot", "last_seen_snapshot_id"),
        Index("ix_reference_services_status", "provider_key", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seen_snapshot_id: Mapped[int] = mapped_column(
        nullable=False,
    )
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    upstream_protocol: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    default_crs: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_format: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    cache_policy: Mapped[str] = mapped_column(
        String(20),
        default="none",
        server_default="none",
        nullable=False,
    )
    capabilities_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_seen_snapshot: Mapped[ReferenceCatalogSnapshot] = relationship(
        "ReferenceCatalogSnapshot",
        back_populates="services",
        foreign_keys=[last_seen_snapshot_id],
        primaryjoin=(
            "ReferenceService.last_seen_snapshot_id == "
            "ReferenceCatalogSnapshot.id"
        ),
    )
    layers: Mapped[list["ReferenceLayer"]] = relationship(
        "ReferenceLayer",
        back_populates="service",
        foreign_keys="ReferenceLayer.service_id",
        primaryjoin="ReferenceService.id == ReferenceLayer.service_id",
    )


class ReferenceLayer(TimestampMixin, Base):
    __tablename__ = "reference_layers"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> '' "
            "and btrim(title) <> ''",
            name="ck_reference_layers_identity_nonempty",
        ),
        CheckConstraint(
            "node_type in ('group', 'layer')",
            name="ck_reference_layers_node_type",
        ),
        CheckConstraint(
            "role is null or role in ('base', 'overlay')",
            name="ck_reference_layers_role",
        ),
        CheckConstraint(
            "renderer is null or renderer in ('raster_tile', 'vector_tile')",
            name="ck_reference_layers_renderer",
        ),
        CheckConstraint(
            "delivery_mode is null or delivery_mode in ('proxy', 'mirror')",
            name="ck_reference_layers_delivery_mode",
        ),
        CheckConstraint(
            "status in ('active', 'degraded', 'missing', 'disabled')",
            name="ck_reference_layers_status",
        ),
        CheckConstraint(
            "default_opacity >= 0 and default_opacity <= 1",
            name="ck_reference_layers_default_opacity",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_reference_layers_sort_order",
        ),
        CheckConstraint(
            "(min_zoom is null or min_zoom between 0 and 24) and "
            "(max_zoom is null or max_zoom between 0 and 24) and "
            "(min_zoom is null or max_zoom is null or min_zoom <= max_zoom)",
            name="ck_reference_layers_zoom_range",
        ),
        CheckConstraint(
            "(min_scale_denominator is null or min_scale_denominator > 0) and "
            "(max_scale_denominator is null or max_scale_denominator > 0)",
            name="ck_reference_layers_scale_positive",
        ),
        CheckConstraint(
            "(legend_url is null or legend_url ~ '^https?://') and "
            "(metadata_url is null or metadata_url ~ '^https?://')",
            name="ck_reference_layers_urls",
        ),
        CheckConstraint(
            "(node_type = 'group' and service_id is null and role is null "
            "and renderer is null and delivery_mode is null) or "
            "(node_type = 'layer' and service_id is not null "
            "and role is not null and renderer is not null "
            "and delivery_mode is not null)",
            name="ck_reference_layers_node_shape",
        ),
        UniqueConstraint(
            "provider_key",
            "source_key",
            name="uq_reference_layers_provider_source",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_layers_provider_id",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            "service_id",
            name="uq_reference_layers_provider_id_service",
        ),
        ForeignKeyConstraint(
            ["provider_key", "last_seen_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_layers_provider_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_layers_provider_service",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "parent_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layers_provider_parent",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_layers_parent_order",
            "parent_id",
            "sort_order",
            "id",
        ),
        Index("ix_reference_layers_service_status", "service_id", "status"),
        Index("ix_reference_layers_snapshot", "last_seen_snapshot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seen_snapshot_id: Mapped[int] = mapped_column(
        nullable=False,
    )
    service_id: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    parent_id: Mapped[int | None] = mapped_column(
        nullable=True,
    )
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    node_type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    renderer: Mapped[str | None] = mapped_column(String(30), nullable=True)
    delivery_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    style_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_format: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supported_crs_json: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    bounds_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    options_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    default_visible: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    default_opacity: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),
        default=Decimal("1"),
        server_default="1",
        nullable=False,
    )
    min_zoom: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    max_zoom: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    min_scale_denominator: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 3),
        nullable=True,
    )
    max_scale_denominator: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 3),
        nullable=True,
    )
    queryable: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    downloadable: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    legend_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
    )

    last_seen_snapshot: Mapped[ReferenceCatalogSnapshot] = relationship(
        "ReferenceCatalogSnapshot",
        back_populates="layers",
        foreign_keys=[last_seen_snapshot_id],
        primaryjoin=(
            "ReferenceLayer.last_seen_snapshot_id == "
            "ReferenceCatalogSnapshot.id"
        ),
    )
    service: Mapped[ReferenceService | None] = relationship(
        "ReferenceService",
        back_populates="layers",
        foreign_keys=[service_id],
        primaryjoin="ReferenceLayer.service_id == ReferenceService.id",
    )
    parent: Mapped["ReferenceLayer | None"] = relationship(
        "ReferenceLayer",
        remote_side="ReferenceLayer.id",
        back_populates="children",
        foreign_keys=[parent_id],
        primaryjoin="ReferenceLayer.parent_id == ReferenceLayer.id",
    )
    children: Mapped[list["ReferenceLayer"]] = relationship(
        "ReferenceLayer",
        back_populates="parent",
        foreign_keys=[parent_id],
        primaryjoin="ReferenceLayer.id == ReferenceLayer.parent_id",
    )
    organization_settings: Mapped[list["OrganizationReferenceLayerSetting"]] = (
        relationship(
            "OrganizationReferenceLayerSetting",
            back_populates="layer",
        )
    )
    styles: Mapped[list["ReferenceLayerStyle"]] = relationship(
        "ReferenceLayerStyle",
        back_populates="layer",
        foreign_keys="ReferenceLayerStyle.layer_id",
        primaryjoin="ReferenceLayer.id == ReferenceLayerStyle.layer_id",
        order_by=(
            "ReferenceLayerStyle.sort_order, ReferenceLayerStyle.id"
        ),
    )


class ReferenceLayerStyle(TimestampMixin, Base):
    __tablename__ = "reference_layer_styles"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> '' "
            "and btrim(remote_name) <> '' and btrim(title) <> ''",
            name="ck_reference_layer_styles_identity_nonempty",
        ),
        CheckConstraint(
            "legend_url is null or legend_url ~ '^https?://'",
            name="ck_reference_layer_styles_legend_url",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_reference_layer_styles_sort_order",
        ),
        CheckConstraint(
            "status in ('active', 'degraded', 'missing', 'disabled')",
            name="ck_reference_layer_styles_status",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_key",
            name="uq_reference_layer_styles_provider_layer_source",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_layer_styles_provider_id",
        ),
        ForeignKeyConstraint(
            ["provider_key", "last_seen_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_layer_styles_provider_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_styles_provider_layer",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_reference_layer_styles_default",
            "provider_key",
            "layer_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index(
            "ix_reference_layer_styles_layer_order",
            "layer_id",
            "sort_order",
            "id",
        ),
        Index(
            "ix_reference_layer_styles_snapshot",
            "last_seen_snapshot_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seen_snapshot_id: Mapped[int] = mapped_column(nullable=False)
    layer_id: Mapped[int] = mapped_column(nullable=False)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    legend_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
    )

    last_seen_snapshot: Mapped[ReferenceCatalogSnapshot] = relationship(
        "ReferenceCatalogSnapshot",
        back_populates="styles",
        foreign_keys=[last_seen_snapshot_id],
        primaryjoin=(
            "ReferenceLayerStyle.last_seen_snapshot_id == "
            "ReferenceCatalogSnapshot.id"
        ),
    )
    layer: Mapped[ReferenceLayer] = relationship(
        "ReferenceLayer",
        back_populates="styles",
        foreign_keys=[layer_id],
        primaryjoin="ReferenceLayerStyle.layer_id == ReferenceLayer.id",
    )


class ReferenceWMSCapabilitiesSnapshot(Base):
    __tablename__ = "reference_wms_capabilities_snapshots"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_wms_capabilities_provider_nonempty",
        ),
        CheckConstraint(
            "raw_size_bytes between 1 and 4194304 "
            "and raw_size_bytes = octet_length(raw_xml)",
            name="ck_reference_wms_capabilities_raw_size",
        ),
        CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$' "
            "and normalized_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_wms_capabilities_hashes",
        ),
        CheckConstraint(
            "normalization_version = 'siur-wms-capabilities-v1'",
            name="ck_reference_wms_capabilities_normalization",
        ),
        CheckConstraint(
            "wms_version in ('1.1.1', '1.3.0')",
            name="ck_reference_wms_capabilities_version",
        ),
        CheckConstraint(
            "get_map_endpoint like 'https://%' and "
            "(get_legend_endpoint is null or "
            "get_legend_endpoint like 'https://%') and "
            "(get_feature_info_endpoint is null or "
            "get_feature_info_endpoint like 'https://%')",
            name="ck_reference_wms_capabilities_endpoints",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "raw_sha256",
            "normalized_sha256",
            name="uq_reference_wms_capabilities_content",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "id",
            name="uq_reference_wms_capabilities_provider_service_id",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_wms_capabilities_provider_service",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_wms_capabilities_service_created",
            "provider_key",
            "service_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[int] = mapped_column(nullable=False)
    raw_xml: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalization_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    wms_version: Mapped[str] = mapped_column(String(16), nullable=False)
    get_map_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    get_legend_endpoint: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    get_feature_info_endpoint: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    get_map_formats_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
    )
    get_legend_formats_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
    )
    get_feature_info_formats_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
    )
    layer_manifest_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceLicenseReview(Base):
    __tablename__ = "reference_license_reviews"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(reviewer) <> '' "
            "and btrim(license_name) <> '' and btrim(license_terms) <> ''",
            name="ck_reference_license_reviews_required_text",
        ),
        CheckConstraint(
            "document_size_bytes between 1 and 262144 "
            "and document_size_bytes = octet_length(reviewed_document)",
            name="ck_reference_license_reviews_document_size",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$' "
            "and review_sha256 ~ '^[0-9a-f]{64}$' and "
            "(supersedes_review_sha256 is null or "
            "supersedes_review_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_license_reviews_hashes",
        ),
        CheckConstraint(
            "decision in ('approved', 'restricted', 'rejected')",
            name="ck_reference_license_reviews_decision",
        ),
        CheckConstraint(
            "license_url is null or license_url like 'https://%'",
            name="ck_reference_license_reviews_license_url",
        ),
        CheckConstraint(
            "not allow_cache or allow_proxy",
            name="ck_reference_license_reviews_cache_requires_proxy",
        ),
        CheckConstraint(
            "(not allow_proxy and not allow_cache) or decision = 'approved'",
            name="ck_reference_license_reviews_permissions_approved",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "evidence_sha256",
            "review_sha256",
            name="uq_reference_license_reviews_content",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "id",
            name="uq_reference_license_reviews_provider_service_id",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "review_sha256",
            name="uq_reference_license_reviews_review_hash",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_license_reviews_provider_service",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id", "supersedes_review_sha256"],
            [
                "reference_license_reviews.provider_key",
                "reference_license_reviews.service_id",
                "reference_license_reviews.review_sha256",
            ],
            name="fk_reference_license_reviews_supersedes",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_license_reviews_service_reviewed",
            "provider_key",
            "service_id",
            "id",
        ),
        Index(
            "uq_reference_license_reviews_genesis",
            "provider_key",
            "service_id",
            unique=True,
            postgresql_where=text("supersedes_review_sha256 is null"),
        ),
        Index(
            "uq_reference_license_reviews_successor",
            "provider_key",
            "service_id",
            "supersedes_review_sha256",
            unique=True,
            postgresql_where=text("supersedes_review_sha256 is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[int] = mapped_column(nullable=False)
    reviewed_document: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    document_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_review_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    license_name: Mapped[str] = mapped_column(String(500), nullable=False)
    license_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_terms: Mapped[str] = mapped_column(Text, nullable=False)
    allow_proxy: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    allow_cache: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryAttestation(Base):
    __tablename__ = "reference_delivery_attestations"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_reference_delivery_attestations_provider_nonempty",
        ),
        CheckConstraint(
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$' "
            "and attestation_sha256 ~ '^[0-9a-f]{64}$' and "
            "(previous_attestation_sha256 is null or "
            "previous_attestation_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_delivery_attestations_hashes",
        ),
        CheckConstraint(
            "attestation_kind in ('delivery', 'revocation')",
            name="ck_reference_delivery_attestations_kind",
        ),
        CheckConstraint(
            "(sequence_number = 1 and previous_attestation_id is null and "
            "previous_attestation_sha256 is null) or "
            "(sequence_number > 1 and previous_attestation_id is not null "
            "and previous_attestation_sha256 is not null)",
            name="ck_reference_delivery_attestations_chain",
        ),
        UniqueConstraint(
            "attestation_sha256",
            name="uq_reference_delivery_attestations_hash",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "sequence_number",
            name="uq_reference_delivery_attestations_sequence",
        ),
        UniqueConstraint(
            "provider_key",
            "service_id",
            "id",
            "attestation_sha256",
            name="uq_reference_delivery_attestations_chain_target",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id"],
            ["reference_services.provider_key", "reference_services.id"],
            name="fk_reference_delivery_attestations_provider_service",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "catalog_snapshot_id",
                "catalog_definition_sha256",
            ],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
                "reference_catalog_snapshots.definition_sha256",
            ],
            name="fk_reference_delivery_attestations_catalog",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id", "capabilities_snapshot_id"],
            [
                "reference_wms_capabilities_snapshots.provider_key",
                "reference_wms_capabilities_snapshots.service_id",
                "reference_wms_capabilities_snapshots.id",
            ],
            name="fk_reference_delivery_attestations_capabilities",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "service_id", "license_review_id"],
            [
                "reference_license_reviews.provider_key",
                "reference_license_reviews.service_id",
                "reference_license_reviews.id",
            ],
            name="fk_reference_delivery_attestations_license",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "service_id",
                "previous_attestation_id",
                "previous_attestation_sha256",
            ],
            [
                "reference_delivery_attestations.provider_key",
                "reference_delivery_attestations.service_id",
                "reference_delivery_attestations.id",
                "reference_delivery_attestations.attestation_sha256",
            ],
            name="fk_reference_delivery_attestations_previous",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_delivery_attestations_current_lookup",
            "provider_key",
            "service_id",
            "sequence_number",
        ),
        Index(
            "uq_reference_delivery_attestations_genesis",
            "provider_key",
            "service_id",
            unique=True,
            postgresql_where=text("previous_attestation_id is null"),
        ),
        Index(
            "uq_reference_delivery_attestations_successor",
            "provider_key",
            "service_id",
            "previous_attestation_id",
            unique=True,
            postgresql_where=text("previous_attestation_id is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[int] = mapped_column(nullable=False)
    catalog_snapshot_id: Mapped[int] = mapped_column(nullable=False)
    catalog_definition_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    capabilities_snapshot_id: Mapped[int] = mapped_column(nullable=False)
    license_review_id: Mapped[int] = mapped_column(nullable=False)
    attestation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_attestation_id: Mapped[int | None] = mapped_column(nullable=True)
    previous_attestation_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    attestation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceLayerSource(TimestampMixin, Base):
    __tablename__ = "reference_layer_sources"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(source_key) <> ''",
            name="ck_reference_layer_sources_identity_nonempty",
        ),
        CheckConstraint(
            "protocol in ('wfs', 'ogc_api_features', 'wcs', "
            "'arcgis_rest', 'atom', 'download', 'wmts', 'xyz', "
            "'wms_tiles', 'local')",
            name="ck_reference_layer_sources_protocol",
        ),
        CheckConstraint(
            "target_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_layer_sources_target_kind",
        ),
        CheckConstraint(
            "sync_strategy in ('conditional_get', 'full_snapshot', "
            "'paged_snapshot', 'tile_seed', 'manual')",
            name="ck_reference_layer_sources_sync_strategy",
        ),
        CheckConstraint(
            "(protocol = 'local' and endpoint_url is null) or "
            "(protocol <> 'local' and endpoint_url like 'https://%')",
            name="ck_reference_layer_sources_endpoint",
        ),
        CheckConstraint(
            "definition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_layer_sources_definition_sha256",
        ),
        CheckConstraint(
            "priority >= 0 and check_interval_seconds >= 300 and "
            "full_refresh_interval_seconds >= check_interval_seconds",
            name="ck_reference_layer_sources_schedule",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_key",
            name="uq_reference_layer_sources_layer_source",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_layer_sources_provider_layer_id",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_sources_provider_layer",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_reference_layer_sources_primary",
            "provider_key",
            "layer_id",
            unique=True,
            postgresql_where=text("enabled and is_primary"),
        ),
        Index(
            "ix_reference_layer_sources_due",
            "next_check_at",
            "id",
            postgresql_where=text("enabled"),
        ),
        Index(
            "ix_reference_layer_sources_layer_priority",
            "layer_id",
            "enabled",
            "priority",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'::json"),
        nullable=False,
    )
    definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(
        SmallInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    check_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=86400,
        server_default="86400",
        nullable=False,
    )
    full_refresh_interval_seconds: Mapped[int] = mapped_column(
        Integer,
        default=2592000,
        server_default="2592000",
        nullable=False,
    )
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceMirrorAuthorizationReview(Base):
    """Append-only human authorization for one exact local-mirror source."""

    __tablename__ = "reference_mirror_authorization_reviews"
    __table_args__ = (
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(reviewer) <> '' and "
            "btrim(license_name) <> '' and btrim(license_terms) <> ''",
            name="ck_reference_mirror_authorizations_required_text",
        ),
        CheckConstraint(
            "document_size_bytes between 1 and 262144 and "
            "document_size_bytes = octet_length(reviewed_document)",
            name="ck_reference_mirror_authorizations_document_size",
        ),
        CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$' and "
            "review_sha256 ~ '^[0-9a-f]{64}$' and "
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "(supersedes_review_sha256 is null or "
            "supersedes_review_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_mirror_authorizations_hashes",
        ),
        CheckConstraint(
            "decision in ('approved', 'restricted', 'rejected')",
            name="ck_reference_mirror_authorizations_decision",
        ),
        CheckConstraint(
            "protocol in ('wfs', 'ogc_api_features', 'wcs', "
            "'arcgis_rest', 'atom', 'download', 'wmts', 'xyz', "
            "'wms_tiles', 'local') and "
            "target_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_mirror_authorizations_source_kind",
        ),
        CheckConstraint(
            "canonical_origin like 'https://%' and "
            "license_url like 'https://%' and "
            "json_typeof(allowed_origins_json) = 'array' and "
            "json_array_length(allowed_origins_json) between 1 and 32",
            name="ck_reference_mirror_authorizations_urls",
        ),
        CheckConstraint(
            "(supersedes_review_id is null and "
            "supersedes_review_sha256 is null) or "
            "(supersedes_review_id is not null and "
            "supersedes_review_sha256 is not null)",
            name="ck_reference_mirror_authorizations_chain_shape",
        ),
        CheckConstraint(
            "(not allow_metadata_probe and not allow_dataset_download and "
            "not allow_local_storage and not allow_local_service and "
            "not allow_bulk_tile_seed) or decision = 'approved'",
            name="ck_reference_mirror_authorizations_approved_permissions",
        ),
        CheckConstraint(
            "not allow_local_storage or allow_dataset_download",
            name="ck_reference_mirror_authorizations_storage_download",
        ),
        CheckConstraint(
            "not allow_local_service or allow_local_storage",
            name="ck_reference_mirror_authorizations_service_storage",
        ),
        CheckConstraint(
            "not allow_local_service or "
            "(attribution is not null and btrim(attribution) <> '')",
            name="ck_reference_mirror_authorizations_service_attribution",
        ),
        CheckConstraint(
            "target_kind <> 'tiles' or not allow_local_service or "
            "allow_bulk_tile_seed",
            name="ck_reference_mirror_authorizations_tiles_seed",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "document_sha256",
            "review_sha256",
            name="uq_reference_mirror_authorizations_content",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "review_sha256",
            name="uq_reference_mirror_authorizations_review_hash",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "id",
            "review_sha256",
            name="uq_reference_mirror_authorizations_chain_target",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "source_id",
            "source_definition_sha256",
            "id",
            "review_sha256",
            name="uq_reference_mirror_authorizations_run_target",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "service_id"],
            [
                "reference_layers.provider_key",
                "reference_layers.id",
                "reference_layers.service_id",
            ],
            name="fk_reference_mirror_authorizations_layer_service",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
            ],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_mirror_authorizations_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "supersedes_review_id",
                "supersedes_review_sha256",
            ],
            [
                "reference_mirror_authorization_reviews.provider_key",
                "reference_mirror_authorization_reviews.layer_id",
                "reference_mirror_authorization_reviews.source_id",
                "reference_mirror_authorization_reviews.id",
                "reference_mirror_authorization_reviews.review_sha256",
            ],
            name="fk_reference_mirror_authorizations_supersedes",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_mirror_authorizations_source_reviewed",
            "provider_key",
            "layer_id",
            "source_id",
            "reviewed_at",
            "id",
        ),
        Index(
            "uq_reference_mirror_authorizations_genesis",
            "provider_key",
            "layer_id",
            "source_id",
            unique=True,
            postgresql_where=text("supersedes_review_id is null"),
        ),
        Index(
            "uq_reference_mirror_authorizations_successor",
            "provider_key",
            "layer_id",
            "source_id",
            "supersedes_review_id",
            unique=True,
            postgresql_where=text("supersedes_review_id is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[int] = mapped_column(Integer, nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_definition_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_origin: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_origins_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
    )
    reviewed_document: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
    )
    document_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_review_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    supersedes_review_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    license_name: Mapped[str] = mapped_column(String(500), nullable=False)
    license_url: Mapped[str] = mapped_column(Text, nullable=False)
    license_terms: Mapped[str] = mapped_column(Text, nullable=False)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_metadata_probe: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    allow_dataset_download: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    allow_local_storage: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    allow_local_service: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    allow_bulk_tile_seed: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceSyncRun(TimestampMixin, Base):
    __tablename__ = "reference_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "source_definition_sha256 ~ '^[0-9a-f]{64}$' and attempt_no > 0 "
            "and expected_active_generation >= 0 and fallback_depth >= 0",
            name="ck_reference_sync_runs_identity",
        ),
        CheckConstraint(
            "(parent_run_id is null and fallback_depth = 0) or "
            "(parent_run_id is not null and fallback_depth > 0 and "
            "trigger_kind = 'retry')",
            name="ck_reference_sync_runs_fallback_chain",
        ),
        CheckConstraint(
            "parent_run_id is null or parent_run_id <> id",
            name="ck_reference_sync_runs_parent_not_self",
        ),
        CheckConstraint(
            "trigger_kind in ('scheduled', 'manual', 'retry', 'backfill')",
            name="ck_reference_sync_runs_trigger_kind",
        ),
        CheckConstraint(
            "check_mode in ('conditional', 'full')",
            name="ck_reference_sync_runs_check_mode",
        ),
        CheckConstraint(
            "status in ('queued', 'running', 'unchanged', 'succeeded', "
            "'rejected', 'failed', 'cancelled')",
            name="ck_reference_sync_runs_status",
        ),
        CheckConstraint(
            "observed_manifest_sha256 is null or "
            "observed_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_sync_runs_manifest_sha256",
        ),
        CheckConstraint(
            "(status = 'queued' and started_at is null and finished_at is null "
            "and lease_token is null and lease_expires_at is null and "
            "heartbeat_at is null) or "
            "(status = 'running' and started_at is not null and "
            "finished_at is null and lease_token is not null and "
            "lease_expires_at is not null and heartbeat_at is not null) or "
            "(status in ('unchanged', 'succeeded', 'rejected', 'failed', "
            "'cancelled') and finished_at is not null and lease_token is null "
            "and lease_expires_at is null)",
            name="ck_reference_sync_runs_lifecycle",
        ),
        CheckConstraint(
            "status not in ('rejected', 'failed') or "
            "(error_code is not null and btrim(error_code) <> '')",
            name="ck_reference_sync_runs_failure_error",
        ),
        CheckConstraint(
            "(observed_etag is null or length(observed_etag) <= 4096) and "
            "(observed_version is null or length(observed_version) <= 2048) "
            "and (error_summary is null or length(error_summary) <= 4096) "
            "and octet_length(stats_json::text) <= 1048576",
            name="ck_reference_sync_runs_observed_bounds",
        ),
        UniqueConstraint(
            "source_id",
            "id",
            name="uq_reference_sync_runs_source_id",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_sync_runs_layer_id",
        ),
        UniqueConstraint(
            "source_id",
            "id",
            "mirror_authorization_review_id",
            "mirror_authorization_review_sha256",
            name="uq_reference_sync_runs_authorization",
        ),
        CheckConstraint(
            "(mirror_authorization_review_id is null and "
            "mirror_authorization_review_sha256 is null) or "
            "(mirror_authorization_review_id is not null and "
            "mirror_authorization_review_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_sync_runs_authorization",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_sync_runs_layer_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "parent_run_id"],
            [
                "reference_sync_runs.provider_key",
                "reference_sync_runs.layer_id",
                "reference_sync_runs.id",
            ],
            name="fk_reference_sync_runs_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "source_definition_sha256",
                "mirror_authorization_review_id",
                "mirror_authorization_review_sha256",
            ],
            [
                "reference_mirror_authorization_reviews.provider_key",
                "reference_mirror_authorization_reviews.layer_id",
                "reference_mirror_authorization_reviews.source_id",
                "reference_mirror_authorization_reviews.source_definition_sha256",
                "reference_mirror_authorization_reviews.id",
                "reference_mirror_authorization_reviews.review_sha256",
            ],
            name="fk_reference_sync_runs_mirror_authorization",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_reference_sync_runs_open_source",
            "source_id",
            unique=True,
            postgresql_where=text("status in ('queued', 'running')"),
        ),
        Index(
            "uq_reference_sync_runs_open_layer",
            "provider_key",
            "layer_id",
            unique=True,
            postgresql_where=text("status in ('queued', 'running')"),
        ),
        Index(
            "uq_reference_sync_runs_fallback_child",
            "parent_run_id",
            unique=True,
            postgresql_where=text("parent_run_id is not null"),
        ),
        Index(
            "ix_reference_sync_runs_queued",
            "queued_at",
            "id",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_reference_sync_runs_running_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_reference_sync_runs_source_history", "source_id", "id"),
        Index(
            "ix_reference_sync_runs_layer_history",
            "provider_key",
            "layer_id",
            "id",
        ),
        Index("ix_reference_sync_runs_requested_by", "requested_by_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reference_layer_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    fallback_depth: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    requested_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_definition_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    source_definition_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    mirror_authorization_review_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    mirror_authorization_review_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    check_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_no: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    expected_active_generation: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    observed_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_last_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    observed_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'::json"),
        nullable=False,
    )


class ReferenceSourceArtifact(Base):
    __tablename__ = "reference_source_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_kind in ('capabilities', 'manifest', 'dataset', "
            "'style', 'style_package', 'style_resource', 'metadata', "
            "'tile_archive')",
            name="ck_reference_source_artifacts_kind",
        ),
        CheckConstraint(
            "storage_backend in ('filesystem', 's3')",
            name="ck_reference_source_artifacts_storage_backend",
        ),
        CheckConstraint(
            "size_bytes > 0 and sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_source_artifacts_content",
        ),
        CheckConstraint(
            "(source_url is null or source_url like 'https://%') and "
            "(final_url is null or final_url like 'https://%')",
            name="ck_reference_source_artifacts_urls",
        ),
        CheckConstraint(
            "btrim(media_type) <> '' and btrim(storage_key) <> ''",
            name="ck_reference_source_artifacts_required_text",
        ),
        CheckConstraint(
            "(source_version is null or length(source_version) <= 2048) and "
            "(upstream_etag is null or length(upstream_etag) <= 4096) and "
            "octet_length(metadata_json::text) <= 4194304",
            name="ck_reference_source_artifacts_metadata_bounds",
        ),
        UniqueConstraint(
            "source_id",
            "artifact_kind",
            "sha256",
            name="uq_reference_source_artifacts_content",
        ),
        UniqueConstraint(
            "source_id",
            "id",
            name="uq_reference_source_artifacts_source_id",
        ),
        Index(
            "ix_reference_source_artifacts_storage",
            "storage_backend",
            "storage_key",
        ),
        Index(
            "ix_reference_source_artifacts_source_history",
            "source_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reference_layer_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_last_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'::json"),
        nullable=False,
    )
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceSyncRunArtifact(Base):
    __tablename__ = "reference_sync_run_artifacts"
    __table_args__ = (
        CheckConstraint(
            "role in ('observation', 'input', 'style', 'style_package', "
            "'style_resource', 'metadata')",
            name="ck_reference_sync_run_artifacts_role",
        ),
        ForeignKeyConstraint(
            ["source_id", "run_id"],
            ["reference_sync_runs.source_id", "reference_sync_runs.id"],
            name="fk_reference_sync_run_artifacts_source_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_sync_run_artifacts_source_artifact",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_sync_run_artifacts_artifact",
            "artifact_id",
            "run_id",
        ),
    )

    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceStyleParityPlan(Base):
    """Immutable style-reproduction decision for one catalog-bound sync run."""

    __tablename__ = "reference_style_parity_plans"
    __table_args__ = (
        CheckConstraint(
            "delivery_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_style_parity_plans_kind",
        ),
        CheckConstraint(
            "required_style_count > 0 and missing_style_count >= 0 and "
            "missing_style_count <= required_style_count and "
            "complete = (missing_style_count = 0)",
            name="ck_reference_style_parity_plans_counts",
        ),
        CheckConstraint(
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_style_parity_plans_hashes",
        ),
        CheckConstraint(
            "btrim(provider_key) <> '' and "
            "octet_length(evidence_json::text) <= 4194304",
            name="ck_reference_style_parity_plans_evidence",
        ),
        UniqueConstraint(
            "source_id",
            "sync_run_id",
            name="uq_reference_style_parity_plans_run",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_style_parity_plans_layer_id",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_style_parity_plans_layer_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "sync_run_id"],
            ["reference_sync_runs.source_id", "reference_sync_runs.id"],
            name="fk_reference_style_parity_plans_source_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "catalog_snapshot_id",
                "catalog_definition_sha256",
            ],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
                "reference_catalog_snapshots.definition_sha256",
            ],
            name="fk_reference_style_parity_plans_catalog",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_style_parity_plans_snapshot",
            "provider_key",
            "catalog_snapshot_id",
            "layer_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_definition_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sync_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    required_style_count: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_style_count: Mapped[int] = mapped_column(Integer, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceStyleParityPlanItem(Base):
    """One required catalog (or implicit default) style in a parity plan."""

    __tablename__ = "reference_style_parity_plan_items"
    __table_args__ = (
        CheckConstraint(
            "parity_kind in ('exact', 'adapted', 'baked', 'missing')",
            name="ck_reference_style_parity_plan_items_kind",
        ),
        CheckConstraint(
            "btrim(style_source_key) <> '' and btrim(remote_name) <> '' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$' and resource_count >= 0 and "
            "octet_length(evidence_json::text) <= 4194304",
            name="ck_reference_style_parity_plan_items_evidence",
        ),
        CheckConstraint(
            "(parity_kind = 'missing' and not verified and "
            "reason_code is not null and btrim(reason_code) <> '') or "
            "(parity_kind <> 'missing' and verified and reason_code is null)",
            name="ck_reference_style_parity_plan_items_verification",
        ),
        CheckConstraint(
            "(parity_kind = 'exact' and "
            "source_style_artifact_id is not null and "
            "source_package_artifact_id is null and resource_count = 0) or "
            "(parity_kind = 'adapted' and "
            "source_style_artifact_id is not null and "
            "source_package_artifact_id is not null and resource_count >= 0) "
            "or (parity_kind in ('baked', 'missing') and "
            "source_style_artifact_id is null and "
            "source_package_artifact_id is null and resource_count = 0)",
            name="ck_reference_style_parity_plan_items_artifacts",
        ),
        UniqueConstraint(
            "plan_id",
            "style_source_key",
            name="uq_reference_style_parity_plan_items_identity",
        ),
        UniqueConstraint(
            "plan_id",
            "id",
            name="uq_reference_style_parity_plan_items_plan_id",
        ),
        ForeignKeyConstraint(
            ["plan_id"],
            ["reference_style_parity_plans.id"],
            name="fk_reference_style_parity_plan_items_plan",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["style_id"],
            ["reference_layer_styles.id"],
            name="fk_reference_style_parity_plan_items_style",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "source_style_artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_style_parity_plan_items_style_artifact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "source_package_artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_style_parity_plan_items_package_artifact",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_style_parity_plan_items_status",
            "plan_id",
            "parity_kind",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    style_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    style_source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_style_artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    source_package_artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    resource_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceStyleParityPlanResource(Base):
    """A locally copied auxiliary resource required by one adapted SLD."""

    __tablename__ = "reference_style_parity_plan_resources"
    __table_args__ = (
        CheckConstraint(
            "resolved_url like 'https://%' and "
            "btrim(original_href) <> '' and "
            "local_path ~ '^resources/[0-9a-f]{64}\\.[a-z0-9]{1,8}$' and "
            "sha256 ~ '^[0-9a-f]{64}$' and btrim(media_type) <> '' and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_style_parity_plan_resources_evidence",
        ),
        UniqueConstraint(
            "plan_item_id",
            "original_href",
            name="uq_reference_style_parity_plan_resources_href",
        ),
        UniqueConstraint(
            "plan_item_id",
            "id",
            name="uq_reference_style_parity_plan_resources_item_id",
        ),
        ForeignKeyConstraint(
            ["plan_item_id"],
            ["reference_style_parity_plan_items.id"],
            name="fk_reference_style_parity_plan_resources_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_style_parity_plan_resources_artifact",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_style_parity_plan_resources_artifact",
            "artifact_id",
            "plan_item_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plan_item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_href: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_url: Mapped[str] = mapped_column(Text, nullable=False)
    local_path: Mapped[str] = mapped_column(String(96), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryVersion(Base):
    __tablename__ = "reference_delivery_versions"
    __table_args__ = (
        CheckConstraint(
            "delivery_kind in ('vector', 'raster', 'tiles')",
            name="ck_reference_delivery_versions_kind",
        ),
        CheckConstraint(
            "sequence_number > 0 and "
            "(feature_count is null or feature_count >= 0)",
            name="ck_reference_delivery_versions_counts",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$' and "
            "manifest_sha256 ~ '^[0-9a-f]{64}$' and "
            "validation_sha256 ~ '^[0-9a-f]{64}$' and "
            "catalog_definition_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reference_delivery_versions_hashes",
        ),
        CheckConstraint(
            "btrim(provider_key) <> '' and btrim(crs) <> ''",
            name="ck_reference_delivery_versions_required_text",
        ),
        CheckConstraint(
            "source_version is null or length(source_version) <= 2048",
            name="ck_reference_delivery_versions_source_version_bounds",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_delivery_versions_provider_layer_id",
        ),
        UniqueConstraint(
            "source_id",
            "id",
            name="uq_reference_delivery_versions_source_id",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "sequence_number",
            name="uq_reference_delivery_versions_sequence",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "manifest_sha256",
            name="uq_reference_delivery_versions_manifest",
        ),
        UniqueConstraint(
            "sync_run_id",
            name="uq_reference_delivery_versions_sync_run",
        ),
        CheckConstraint(
            "(mirror_authorization_review_id is null and "
            "mirror_authorization_review_sha256 is null) or "
            "(mirror_authorization_review_id is not null and "
            "mirror_authorization_review_sha256 ~ '^[0-9a-f]{64}$')",
            name="ck_reference_delivery_versions_authorization",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "source_id"],
            [
                "reference_layer_sources.provider_key",
                "reference_layer_sources.layer_id",
                "reference_layer_sources.id",
            ],
            name="fk_reference_delivery_versions_layer_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "sync_run_id"],
            ["reference_sync_runs.source_id", "reference_sync_runs.id"],
            name="fk_reference_delivery_versions_source_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "source_id",
                "sync_run_id",
                "mirror_authorization_review_id",
                "mirror_authorization_review_sha256",
            ],
            [
                "reference_sync_runs.source_id",
                "reference_sync_runs.id",
                "reference_sync_runs.mirror_authorization_review_id",
                "reference_sync_runs.mirror_authorization_review_sha256",
            ],
            name="fk_reference_delivery_versions_run_authorization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "source_id",
                "mirror_authorization_review_id",
                "mirror_authorization_review_sha256",
            ],
            [
                "reference_mirror_authorization_reviews.provider_key",
                "reference_mirror_authorization_reviews.layer_id",
                "reference_mirror_authorization_reviews.source_id",
                "reference_mirror_authorization_reviews.id",
                "reference_mirror_authorization_reviews.review_sha256",
            ],
            name="fk_reference_delivery_versions_mirror_authorization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "catalog_snapshot_id",
                "catalog_definition_sha256",
            ],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
                "reference_catalog_snapshots.definition_sha256",
            ],
            name="fk_reference_delivery_versions_catalog",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_delivery_versions_source_history",
            "source_id",
            "id",
        ),
        Index(
            "ix_reference_delivery_versions_catalog_snapshot",
            "catalog_snapshot_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sync_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    catalog_snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_definition_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    mirror_authorization_review_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    mirror_authorization_review_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    crs: Mapped[str] = mapped_column(Text, nullable=False)
    bounds_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    feature_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    validation_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryVersionArtifact(Base):
    __tablename__ = "reference_delivery_version_artifacts"
    __table_args__ = (
        CheckConstraint(
            "role in ('input', 'style', 'style_package', 'style_resource', "
            "'metadata')",
            name="ck_reference_delivery_version_artifacts_role",
        ),
        ForeignKeyConstraint(
            ["source_id", "version_id"],
            [
                "reference_delivery_versions.source_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_delivery_version_artifacts_source_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id", "artifact_id"],
            [
                "reference_source_artifacts.source_id",
                "reference_source_artifacts.id",
            ],
            name="fk_reference_delivery_version_artifacts_source_artifact",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_delivery_version_artifacts_artifact",
            "artifact_id",
            "version_id",
        ),
    )

    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryAsset(Base):
    __tablename__ = "reference_delivery_assets"
    __table_args__ = (
        CheckConstraint(
            "asset_kind in ('vector_table', 'raster_cog', 'tile_archive', "
            "'tile_prefix', 'style_sld', 'style_package', 'legend', "
            "'metadata')",
            name="ck_reference_delivery_assets_kind",
        ),
        CheckConstraint(
            "storage_backend in ('postgres', 'filesystem', 's3')",
            name="ck_reference_delivery_assets_storage_backend",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$' and "
            "(size_bytes is null or size_bytes >= 0) and "
            "(storage_backend = 'postgres' or size_bytes is not null)",
            name="ck_reference_delivery_assets_content",
        ),
        CheckConstraint(
            "btrim(asset_key) <> '' and btrim(storage_key) <> '' and "
            "btrim(media_type) <> ''",
            name="ck_reference_delivery_assets_required_text",
        ),
        CheckConstraint(
            "octet_length(metadata_json::text) <= 4194304",
            name="ck_reference_delivery_assets_metadata_bounds",
        ),
        UniqueConstraint(
            "version_id",
            "asset_key",
            name="uq_reference_delivery_assets_version_key",
        ),
        Index(
            "uq_reference_delivery_assets_primary",
            "version_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index(
            "ix_reference_delivery_assets_storage",
            "storage_backend",
            "storage_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reference_delivery_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_key: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'::json"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryStyleParity(Base):
    """Final style parity bound to one immutable local delivery version."""

    __tablename__ = "reference_delivery_style_parities"
    __table_args__ = (
        CheckConstraint(
            "parity_kind in ('exact', 'adapted', 'baked') and verified",
            name="ck_reference_delivery_style_parities_verified",
        ),
        CheckConstraint(
            "resource_count >= 0 and "
            "((parity_kind = 'adapted' and resource_count >= 0) or "
            "(parity_kind in ('exact', 'baked') and resource_count = 0)) and "
            "evidence_sha256 ~ '^[0-9a-f]{64}$' and "
            "octet_length(evidence_json::text) <= 4194304",
            name="ck_reference_delivery_style_parities_evidence",
        ),
        UniqueConstraint(
            "version_id",
            "plan_item_id",
            name="uq_reference_delivery_style_parities_item",
        ),
        UniqueConstraint(
            "version_id",
            "id",
            name="uq_reference_delivery_style_parities_version_id",
        ),
        ForeignKeyConstraint(
            ["version_id"],
            ["reference_delivery_versions.id"],
            name="fk_reference_delivery_style_parities_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_item_id"],
            ["reference_style_parity_plan_items.id"],
            name="fk_reference_delivery_style_parities_plan_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["delivery_asset_id"],
            ["reference_delivery_assets.id"],
            name="fk_reference_delivery_style_parities_asset",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_delivery_style_parities_version",
            "version_id",
            "parity_kind",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plan_item_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    delivery_asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resource_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryStyleResource(Base):
    """Exact auxiliary-resource provenance for a delivered adapted style."""

    __tablename__ = "reference_delivery_style_resources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delivery_parity_id"],
            ["reference_delivery_style_parities.id"],
            name="fk_reference_delivery_style_resources_parity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_resource_id"],
            ["reference_style_parity_plan_resources.id"],
            name="fk_reference_delivery_style_resources_plan_resource",
            ondelete="RESTRICT",
        ),
    )

    delivery_parity_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    plan_resource_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceDeliveryPromotion(Base):
    __tablename__ = "reference_delivery_promotions"
    __table_args__ = (
        CheckConstraint(
            "action in ('promote', 'rollback', 'deactivate', 'reactivate')",
            name="ck_reference_delivery_promotions_action",
        ),
        CheckConstraint(
            "(action = 'promote' and to_version_id is not null) or "
            "(action = 'rollback' and from_version_id is not null and "
            "to_version_id is not null) or "
            "(action = 'deactivate' and from_version_id is not null and "
            "to_version_id is null) or "
            "(action = 'reactivate' and from_version_id is null and "
            "to_version_id is not null)",
            name="ck_reference_delivery_promotions_shape",
        ),
        CheckConstraint(
            "from_version_id is null or to_version_id is null or "
            "from_version_id <> to_version_id",
            name="ck_reference_delivery_promotions_changes_version",
        ),
        CheckConstraint(
            "(sequence_number = 1 and previous_event_id is null and "
            "previous_event_sha256 is null) or "
            "(sequence_number > 1 and previous_event_id is not null and "
            "previous_event_sha256 is not null)",
            name="ck_reference_delivery_promotions_chain",
        ),
        CheckConstraint(
            "event_sha256 ~ '^[0-9a-f]{64}$' and "
            "(previous_event_sha256 is null or "
            "previous_event_sha256 ~ '^[0-9a-f]{64}$') and "
            "btrim(reason) <> ''",
            name="ck_reference_delivery_promotions_evidence",
        ),
        UniqueConstraint(
            "event_sha256",
            name="uq_reference_delivery_promotions_hash",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "sequence_number",
            name="uq_reference_delivery_promotions_sequence",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            name="uq_reference_delivery_promotions_provider_layer_id",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "id",
            "event_sha256",
            name="uq_reference_delivery_promotions_chain_target",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_delivery_promotions_provider_layer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "from_version_id"],
            [
                "reference_delivery_versions.provider_key",
                "reference_delivery_versions.layer_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_delivery_promotions_from_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "to_version_id"],
            [
                "reference_delivery_versions.provider_key",
                "reference_delivery_versions.layer_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_delivery_promotions_to_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "provider_key",
                "layer_id",
                "previous_event_id",
                "previous_event_sha256",
            ],
            [
                "reference_delivery_promotions.provider_key",
                "reference_delivery_promotions.layer_id",
                "reference_delivery_promotions.id",
                "reference_delivery_promotions.event_sha256",
            ],
            name="fk_reference_delivery_promotions_previous",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_delivery_promotions_current_lookup",
            "provider_key",
            "layer_id",
            "sequence_number",
        ),
        Index(
            "ix_reference_delivery_promotions_from_version",
            "provider_key",
            "layer_id",
            "from_version_id",
        ),
        Index(
            "ix_reference_delivery_promotions_to_version",
            "provider_key",
            "layer_id",
            "to_version_id",
        ),
        Index("ix_reference_delivery_promotions_run", "run_id"),
        Index("ix_reference_delivery_promotions_actor", "actor_id"),
        Index(
            "uq_reference_delivery_promotions_genesis",
            "provider_key",
            "layer_id",
            unique=True,
            postgresql_where=text("previous_event_id is null"),
        ),
        Index(
            "uq_reference_delivery_promotions_successor",
            "provider_key",
            "layer_id",
            "previous_event_id",
            unique=True,
            postgresql_where=text("previous_event_id is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    from_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    to_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("reference_sync_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    previous_event_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    event_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceLayerDeliveryState(Base):
    __tablename__ = "reference_layer_delivery_state"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'disabled')",
            name="ck_reference_layer_delivery_state_status",
        ),
        CheckConstraint(
            "(status = 'active' and active_version_id is not null) or "
            "(status = 'disabled' and active_version_id is null)",
            name="ck_reference_layer_delivery_state_version",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_reference_layer_delivery_state_generation",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_delivery_state_provider_layer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "active_version_id"],
            [
                "reference_delivery_versions.provider_key",
                "reference_delivery_versions.layer_id",
                "reference_delivery_versions.id",
            ],
            name="fk_reference_layer_delivery_state_active_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id", "last_promotion_id"],
            [
                "reference_delivery_promotions.provider_key",
                "reference_delivery_promotions.layer_id",
                "reference_delivery_promotions.id",
            ],
            name="fk_reference_layer_delivery_state_last_promotion",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_reference_layer_delivery_state_active_version",
            "active_version_id",
        ),
        Index(
            "ix_reference_layer_delivery_state_last_promotion",
            "last_promotion_id",
        ),
    )

    provider_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    layer_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    active_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_promotion_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ReferenceLayerMirrorStrategy(Base):
    __tablename__ = "reference_layer_mirror_strategies"
    __table_args__ = (
        CheckConstraint(
            "strategy in ('vector', 'raster', 'tiles', 'composition', 'blocked')",
            name="ck_reference_layer_mirror_strategies_strategy",
        ),
        CheckConstraint(
            "btrim(provider_key) <> '' and strategy_reason_code is not null "
            "and btrim(strategy_reason_code) <> ''",
            name="ck_reference_layer_mirror_strategies_reason_code",
        ),
        CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$' and generation > 0",
            name="ck_reference_layer_mirror_strategies_evidence_generation",
        ),
        CheckConstraint(
            "(strategy in ('vector', 'raster', 'tiles') and source_id is not null) "
            "or (strategy in ('composition', 'blocked') and source_id is null)",
            name="ck_reference_layer_mirror_strategies_source_shape",
        ),
        ForeignKeyConstraint(
            ["provider_key", "layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_mirror_strategies_provider_layer",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "catalog_snapshot_id"],
            [
                "reference_catalog_snapshots.provider_key",
                "reference_catalog_snapshots.id",
            ],
            name="fk_reference_layer_mirror_strategies_provider_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_id"],
            ["reference_layer_sources.id"],
            name="fk_reference_layer_mirror_strategies_provider_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "provider_key",
            "layer_id",
            "catalog_snapshot_id",
            name="uq_reference_layer_mirror_strategies_snapshot_layer",
        ),
        UniqueConstraint(
            "provider_key",
            "id",
            name="uq_reference_layer_mirror_strategies_provider_id",
        ),
        Index(
            "ix_reference_layer_mirror_strategies_current",
            "provider_key",
            "catalog_snapshot_id",
            "layer_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_snapshot_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    catalog_definition_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    strategy_reason_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    strategy_reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ReferenceLayerMirrorStrategyDependency(Base):
    __tablename__ = "reference_layer_mirror_strategy_dependencies"
    __table_args__ = (
        CheckConstraint(
            "dependency_order >= 0 and dependency_layer_id <> strategy_layer_id",
            name="ck_reference_layer_mirror_strategy_dependencies_shape",
        ),
        ForeignKeyConstraint(
            ["provider_key", "strategy_id"],
            [
                "reference_layer_mirror_strategies.provider_key",
                "reference_layer_mirror_strategies.id",
            ],
            name="fk_reference_layer_mirror_strategy_dependencies_strategy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["provider_key", "dependency_layer_id"],
            ["reference_layers.provider_key", "reference_layers.id"],
            name="fk_reference_layer_mirror_strategy_dependencies_layer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "provider_key",
            "strategy_id",
            "dependency_layer_id",
            name="uq_reference_layer_mirror_strategy_dependencies_item",
        ),
        Index(
            "ix_reference_layer_mirror_strategy_dependencies_order",
            "strategy_id",
            "dependency_order",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    strategy_layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dependency_layer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dependency_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class OrganizationReferenceLayerSetting(TimestampMixin, Base):
    __tablename__ = "organization_reference_layer_settings"
    __table_args__ = (
        CheckConstraint(
            "visible is not null or opacity is not null",
            name="ck_org_reference_layer_settings_has_override",
        ),
        CheckConstraint(
            "opacity is null or (opacity >= 0 and opacity <= 1)",
            name="ck_org_reference_layer_settings_opacity",
        ),
        UniqueConstraint(
            "organization_id",
            "layer_id",
            name="uq_org_reference_layer_settings_org_layer",
        ),
        Index("ix_org_reference_layer_settings_layer", "layer_id"),
        Index("ix_org_reference_layer_settings_updated_by", "updated_by_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    layer_id: Mapped[int] = mapped_column(
        ForeignKey("reference_layers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    visible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    opacity: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    layer: Mapped[ReferenceLayer] = relationship(
        "ReferenceLayer",
        back_populates="organization_settings",
    )
    updated_by: Mapped["User | None"] = relationship("User")


def _install_immutable_reference_trigger(
    table: Any,
    *,
    function_name: str,
    trigger_name: str,
    error_message: str,
) -> None:
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE OR REPLACE FUNCTION {function_name}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '{error_message}'
                    USING ERRCODE = '55000';
            END;
            $$ LANGUAGE plpgsql;
            """
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE OR DELETE ON {table.name}
            FOR EACH ROW EXECUTE FUNCTION {function_name}();
            """
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        table,
        "before_drop",
        DDL(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table.name}"
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        table,
        "after_drop",
        DDL(
            f"DROP FUNCTION IF EXISTS {function_name}()"
        ).execute_if(dialect="postgresql"),
    )


def _install_immutable_reference_truncate_trigger(
    table: Any,
    *,
    function_name: str,
    trigger_name: str,
) -> None:
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE TRUNCATE ON {table.name}
            FOR EACH STATEMENT EXECUTE FUNCTION {function_name}();
            """
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        table,
        "before_drop",
        DDL(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table.name}"
        ).execute_if(dialect="postgresql"),
    )


_install_immutable_reference_trigger(
    ReferenceWMSCapabilitiesSnapshot.__table__,
    function_name="prevent_reference_wms_capabilities_mutation",
    trigger_name="trg_reference_wms_capabilities_immutable",
    error_message="reference delivery evidence is immutable",
)
_install_immutable_reference_trigger(
    ReferenceLicenseReview.__table__,
    function_name="prevent_reference_license_review_mutation",
    trigger_name="trg_reference_license_reviews_immutable",
    error_message="reference delivery evidence is immutable",
)
_install_immutable_reference_trigger(
    ReferenceDeliveryAttestation.__table__,
    function_name="prevent_reference_delivery_attestation_mutation",
    trigger_name="trg_reference_delivery_attestations_immutable",
    error_message="reference delivery evidence is immutable",
)
_install_immutable_reference_trigger(
    ReferenceMirrorAuthorizationReview.__table__,
    function_name="prevent_reference_mirror_authorization_mutation",
    trigger_name="trg_reference_mirror_authorizations_immutable",
    error_message="reference mirror authorization is immutable",
)
_install_immutable_reference_truncate_trigger(
    ReferenceMirrorAuthorizationReview.__table__,
    function_name="prevent_reference_mirror_authorization_mutation",
    trigger_name="trg_reference_mirror_authorizations_truncate_immutable",
)
_install_immutable_reference_trigger(
    ReferenceCatalogObservedVersion.__table__,
    function_name="prevent_reference_catalog_observed_version_mutation",
    trigger_name="trg_reference_catalog_observed_versions_immutable",
    error_message="reference catalog observation evidence is immutable",
)
_install_immutable_reference_trigger(
    ReferenceCatalogUpdateCheck.__table__,
    function_name="prevent_reference_catalog_update_check_mutation",
    trigger_name="trg_reference_catalog_update_checks_immutable",
    error_message="reference catalog check evidence is immutable",
)
_install_immutable_reference_truncate_trigger(
    ReferenceCatalogObservedVersion.__table__,
    function_name="prevent_reference_catalog_observed_version_mutation",
    trigger_name="trg_reference_catalog_observed_versions_truncate_immutable",
)
_install_immutable_reference_truncate_trigger(
    ReferenceCatalogUpdateCheck.__table__,
    function_name="prevent_reference_catalog_update_check_mutation",
    trigger_name="trg_reference_catalog_update_checks_truncate_immutable",
)

for _table, _function_name, _trigger_name in (
    (
        ReferenceStyleParityPlan.__table__,
        "prevent_reference_style_parity_plan_mutation",
        "trg_reference_style_parity_plans_immutable",
    ),
    (
        ReferenceStyleParityPlanItem.__table__,
        "prevent_reference_style_parity_plan_item_mutation",
        "trg_reference_style_parity_plan_items_immutable",
    ),
    (
        ReferenceStyleParityPlanResource.__table__,
        "prevent_reference_style_parity_plan_resource_mutation",
        "trg_reference_style_parity_plan_resources_immutable",
    ),
    (
        ReferenceSourceArtifact.__table__,
        "prevent_reference_source_artifact_mutation",
        "trg_reference_source_artifacts_immutable",
    ),
    (
        ReferenceSyncRunArtifact.__table__,
        "prevent_reference_sync_run_artifact_mutation",
        "trg_reference_sync_run_artifacts_immutable",
    ),
    (
        ReferenceDeliveryVersion.__table__,
        "prevent_reference_delivery_version_mutation",
        "trg_reference_delivery_versions_immutable",
    ),
    (
        ReferenceDeliveryVersionArtifact.__table__,
        "prevent_reference_delivery_version_artifact_mutation",
        "trg_reference_delivery_version_artifacts_immutable",
    ),
    (
        ReferenceDeliveryAsset.__table__,
        "prevent_reference_delivery_asset_mutation",
        "trg_reference_delivery_assets_immutable",
    ),
    (
        ReferenceDeliveryStyleParity.__table__,
        "prevent_reference_delivery_style_parity_mutation",
        "trg_reference_delivery_style_parities_immutable",
    ),
    (
        ReferenceDeliveryStyleResource.__table__,
        "prevent_reference_delivery_style_resource_mutation",
        "trg_reference_delivery_style_resources_immutable",
    ),
    (
        ReferenceDeliveryPromotion.__table__,
        "prevent_reference_delivery_promotion_mutation",
        "trg_reference_delivery_promotions_immutable",
    ),
):
    _install_immutable_reference_trigger(
        _table,
        function_name=_function_name,
        trigger_name=_trigger_name,
        error_message="reference mirror records are immutable",
    )

for _table, _function_name, _trigger_name in (
    (
        ReferenceStyleParityPlan.__table__,
        "prevent_reference_style_parity_plan_mutation",
        "trg_reference_style_parity_plans_truncate_immutable",
    ),
    (
        ReferenceStyleParityPlanItem.__table__,
        "prevent_reference_style_parity_plan_item_mutation",
        "trg_reference_style_parity_plan_items_truncate_immutable",
    ),
    (
        ReferenceStyleParityPlanResource.__table__,
        "prevent_reference_style_parity_plan_resource_mutation",
        "trg_reference_style_parity_plan_resources_truncate_immutable",
    ),
    (
        ReferenceDeliveryStyleParity.__table__,
        "prevent_reference_delivery_style_parity_mutation",
        "trg_reference_delivery_style_parities_truncate_immutable",
    ),
    (
        ReferenceDeliveryStyleResource.__table__,
        "prevent_reference_delivery_style_resource_mutation",
        "trg_reference_delivery_style_resources_truncate_immutable",
    ),
):
    _install_immutable_reference_truncate_trigger(
        _table,
        function_name=_function_name,
        trigger_name=_trigger_name,
    )
