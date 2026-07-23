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
            "'style', 'metadata', 'tile_archive')",
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
            "role in ('observation', 'input', 'style', 'metadata')",
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
            "role in ('input', 'style', 'metadata')",
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
            "'tile_prefix', 'style_sld', 'legend', 'metadata')",
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

for _table, _function_name, _trigger_name in (
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
