from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
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
    )
    layers: Mapped[list["ReferenceLayer"]] = relationship(
        "ReferenceLayer",
        back_populates="last_seen_snapshot",
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
        Index("ix_reference_services_snapshot", "last_seen_snapshot_id"),
        Index("ix_reference_services_status", "provider_key", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seen_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("reference_catalog_snapshots.id", ondelete="RESTRICT"),
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
    )
    layers: Mapped[list["ReferenceLayer"]] = relationship(
        "ReferenceLayer",
        back_populates="service",
        foreign_keys="ReferenceLayer.service_id",
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
        ForeignKey("reference_catalog_snapshots.id", ondelete="RESTRICT"),
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
    )
    service: Mapped[ReferenceService | None] = relationship(
        "ReferenceService",
        back_populates="layers",
        foreign_keys=[service_id],
    )
    parent: Mapped["ReferenceLayer | None"] = relationship(
        "ReferenceLayer",
        remote_side="ReferenceLayer.id",
        back_populates="children",
        foreign_keys=[parent_id],
    )
    children: Mapped[list["ReferenceLayer"]] = relationship(
        "ReferenceLayer",
        back_populates="parent",
        foreign_keys=[parent_id],
    )
    organization_settings: Mapped[list["OrganizationReferenceLayerSetting"]] = (
        relationship(
            "OrganizationReferenceLayerSetting",
            back_populates="layer",
        )
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
