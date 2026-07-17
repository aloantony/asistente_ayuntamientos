from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.geo.models import GeoLocation
    from app.organizations.models import Organization
    from app.users.models import User


TAXONOMY_CODE_PATTERN = r"^[a-z0-9]+([-_][a-z0-9]+)*$"


class MunicipalAssetCategory(TimestampMixin, Base):
    __tablename__ = "municipal_asset_categories"
    __table_args__ = (
        CheckConstraint(
            f"code ~ '{TAXONOMY_CODE_PATTERN}'",
            name="ck_municipal_asset_categories_code",
        ),
        CheckConstraint(
            "color is null or color ~ '^#[0-9A-Fa-f]{6}$'",
            name="ck_municipal_asset_categories_color",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_municipal_asset_categories_sort_order",
        ),
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipal_asset_categories_status",
        ),
        UniqueConstraint(
            "organization_id",
            "code",
            name="uq_municipal_asset_categories_org_code",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_asset_categories_id_org",
        ),
        Index(
            "ix_municipal_asset_categories_org_status_sort",
            "organization_id",
            "status",
            "sort_order",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
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

    organization: Mapped["Organization"] = relationship("Organization")
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
    types: Mapped[list["MunicipalAssetType"]] = relationship(
        "MunicipalAssetType",
        back_populates="category",
        primaryjoin="MunicipalAssetCategory.id == MunicipalAssetType.category_id",
        foreign_keys="MunicipalAssetType.category_id",
    )


class MunicipalAssetType(TimestampMixin, Base):
    __tablename__ = "municipal_asset_types"
    __table_args__ = (
        CheckConstraint(
            f"code ~ '{TAXONOMY_CODE_PATTERN}'",
            name="ck_municipal_asset_types_code",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_municipal_asset_types_sort_order",
        ),
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipal_asset_types_status",
        ),
        ForeignKeyConstraint(
            ["category_id", "organization_id"],
            [
                "municipal_asset_categories.id",
                "municipal_asset_categories.organization_id",
            ],
            name="fk_municipal_asset_types_category_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "category_id",
            "code",
            name="uq_municipal_asset_types_org_category_code",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_asset_types_id_org",
        ),
        Index(
            "ix_municipal_asset_types_org_status_sort",
            "organization_id",
            "status",
            "sort_order",
            "id",
        ),
        Index(
            "ix_municipal_asset_types_category_sort",
            "category_id",
            "sort_order",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
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

    organization: Mapped["Organization"] = relationship("Organization")
    category: Mapped["MunicipalAssetCategory"] = relationship(
        "MunicipalAssetCategory",
        back_populates="types",
        primaryjoin="MunicipalAssetType.category_id == MunicipalAssetCategory.id",
        foreign_keys=[category_id],
    )
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
    assets: Mapped[list["MunicipalAsset"]] = relationship(
        "MunicipalAsset",
        back_populates="asset_type",
        primaryjoin="MunicipalAssetType.id == MunicipalAsset.asset_type_id",
        foreign_keys="MunicipalAsset.asset_type_id",
    )


class MunicipalAsset(TimestampMixin, Base):
    __tablename__ = "municipal_assets"
    __table_args__ = (
        CheckConstraint(
            "code is null or btrim(code) <> ''",
            name="ck_municipal_assets_code",
        ),
        CheckConstraint(
            "status in ('active', 'inactive', 'retired', 'archived')",
            name="ck_municipal_assets_status",
        ),
        CheckConstraint(
            "condition_status in ('good', 'fair', 'poor', 'unknown')",
            name="ck_municipal_assets_condition_status",
        ),
        ForeignKeyConstraint(
            ["organization_id", "municipality_id"],
            ["organizations.id", "organizations.municipality_id"],
            name="fk_municipal_assets_organization_municipality",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["asset_type_id", "organization_id"],
            [
                "municipal_asset_types.id",
                "municipal_asset_types.organization_id",
            ],
            name="fk_municipal_assets_asset_type_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["location_id", "organization_id", "municipality_id"],
            [
                "geo_locations.id",
                "geo_locations.organization_id",
                "geo_locations.municipality_id",
            ],
            name="fk_municipal_assets_location_tenant",
            ondelete="NO ACTION",
            match="SIMPLE",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "organization_id",
            "code",
            name="uq_municipal_assets_org_code",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            "municipality_id",
            name="uq_municipal_assets_id_org_municipality",
        ),
        Index(
            "ix_municipal_assets_org_status_id",
            "organization_id",
            "status",
            "id",
        ),
        Index(
            "ix_municipal_assets_org_type_id",
            "organization_id",
            "asset_type_id",
            "id",
        ),
        Index(
            "ix_municipal_assets_municipality_id",
            "municipality_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    municipality_id: Mapped[int] = mapped_column(
        ForeignKey("municipalities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_type_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("geo_locations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        server_default="active",
        nullable=False,
    )
    condition_status: Mapped[str] = mapped_column(
        String(30),
        default="unknown",
        server_default="unknown",
        nullable=False,
    )
    material: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dimensions: Mapped[str | None] = mapped_column(String(500), nullable=True)
    installed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_inspected_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    asset_type: Mapped["MunicipalAssetType"] = relationship(
        "MunicipalAssetType",
        back_populates="assets",
        primaryjoin="MunicipalAsset.asset_type_id == MunicipalAssetType.id",
        foreign_keys=[asset_type_id],
    )
    location: Mapped["GeoLocation | None"] = relationship(
        "GeoLocation",
        primaryjoin="MunicipalAsset.location_id == GeoLocation.id",
        foreign_keys=[location_id],
    )
    created_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[created_by_id],
    )
    updated_by: Mapped["User | None"] = relationship(
        "User",
        foreign_keys=[updated_by_id],
    )
