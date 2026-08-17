from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
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


def _install_immutable_evidence_trigger(
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
            CREATE OR REPLACE FUNCTION {function_name}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'reference delivery evidence is immutable'
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


_install_immutable_evidence_trigger(
    ReferenceWMSCapabilitiesSnapshot.__table__,
    function_name="prevent_reference_wms_capabilities_mutation",
    trigger_name="trg_reference_wms_capabilities_immutable",
)
_install_immutable_evidence_trigger(
    ReferenceLicenseReview.__table__,
    function_name="prevent_reference_license_review_mutation",
    trigger_name="trg_reference_license_reviews_immutable",
)
_install_immutable_evidence_trigger(
    ReferenceDeliveryAttestation.__table__,
    function_name="prevent_reference_delivery_attestation_mutation",
    trigger_name="trg_reference_delivery_attestations_immutable",
)
