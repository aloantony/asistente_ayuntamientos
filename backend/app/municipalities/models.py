from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
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


class Municipality(TimestampMixin, Base):
    __tablename__ = "municipalities"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipalities_status",
        ),
        CheckConstraint(
            "municipality_type in ('municipality', 'minor_local_entity', 'district', 'other')",
            name="ck_municipalities_municipality_type",
        ),
        CheckConstraint(
            "rural_urban_profile in ('rural', 'semi_rural', 'urban', 'mixed', 'unknown')",
            name="ck_municipalities_rural_urban_profile",
        ),
        CheckConstraint(
            "population is null or population >= 0",
            name="ck_municipalities_population_non_negative",
        ),
        CheckConstraint(
            "population_reference_year is null "
            "or population_reference_year between 1900 and 9999",
            name="ck_municipalities_population_reference_year",
        ),
        CheckConstraint(
            "num_nonnulls(population_reference_year, population_source_url, "
            "population_source_sha256) in (0, 3)",
            name="ck_municipalities_population_provenance_complete",
        ),
        CheckConstraint(
            "population_reference_year is null or population is not null",
            name="ck_municipalities_population_provenance_has_population",
        ),
        CheckConstraint(
            "population_source_url is null "
            "or population_source_url like 'https://%'",
            name="ck_municipalities_population_source_url",
        ),
        CheckConstraint(
            "population_source_sha256 is null "
            "or population_source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_municipalities_population_source_sha256",
        ),
        CheckConstraint(
            "num_nonnulls(ine_check_digit, directory_reference_date, "
            "directory_source_url, directory_source_sha256) in (0, 4)",
            name="ck_municipalities_directory_provenance_complete",
        ),
        CheckConstraint(
            "ine_check_digit is null or ine_check_digit ~ '^[0-9]$'",
            name="ck_municipalities_ine_check_digit",
        ),
        CheckConstraint(
            "directory_reference_date is null or ine_code ~ '^[0-9]{5}$'",
            name="ck_municipalities_directory_has_ine_code",
        ),
        CheckConstraint(
            "directory_source_url is null "
            "or directory_source_url like 'https://%'",
            name="ck_municipalities_directory_source_url",
        ),
        CheckConstraint(
            "directory_source_sha256 is null "
            "or directory_source_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_municipalities_directory_source_sha256",
        ),
        CheckConstraint(
            "surface_km2 is null or surface_km2 >= 0",
            name="ck_municipalities_surface_km2_non_negative",
        ),
        CheckConstraint(
            "density is null or density >= 0",
            name="ck_municipalities_density_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    province: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    autonomous_community: Mapped[str] = mapped_column(
        String(255),
        index=True,
        nullable=False,
    )
    country: Mapped[str] = mapped_column(
        String(100),
        default="España",
        server_default="España",
        nullable=False,
    )
    ine_code: Mapped[str | None] = mapped_column(
        String(20),
        unique=True,
        index=True,
        nullable=True,
    )
    ine_check_digit: Mapped[str | None] = mapped_column(String(1), nullable=True)
    directory_reference_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    directory_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    directory_source_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    population_reference_year: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
    )
    population_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    population_source_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    surface_km2: Mapped[float | None] = mapped_column(Float, nullable=True)
    density: Mapped[float | None] = mapped_column(Float, nullable=True)
    postal_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    municipality_type: Mapped[str] = mapped_column(
        String(50),
        default="municipality",
        server_default="municipality",
        nullable=False,
    )
    rural_urban_profile: Mapped[str] = mapped_column(
        String(50),
        default="unknown",
        server_default="unknown",
        nullable=False,
    )
    economic_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    tourism_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    geographic_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    administrative_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        default="active",
        server_default="active",
        nullable=False,
    )

    organizations: Mapped[list["Organization"]] = relationship(
        "Organization",
        back_populates="municipality",
    )
    geography_snapshots: Mapped[list["MunicipalityGeographySnapshot"]] = (
        relationship(
            "MunicipalityGeographySnapshot",
            back_populates="municipality",
            cascade="all, delete-orphan",
            overlaps="official_geography",
        )
    )
    official_geography: Mapped["MunicipalityGeographySnapshot | None"] = (
        relationship(
            "MunicipalityGeographySnapshot",
            primaryjoin=(
                "and_(Municipality.id == "
                "MunicipalityGeographySnapshot.municipality_id, "
                "MunicipalityGeographySnapshot.is_current.is_(True))"
            ),
            uselist=False,
            viewonly=True,
            lazy="selectin",
            overlaps="geography_snapshots,municipality",
        )
    )


class ReferenceDatasetVersion(TimestampMixin, Base):
    __tablename__ = "reference_dataset_versions"
    __table_args__ = (
        CheckConstraint(
            "btrim(dataset_key) <> ''",
            name="ck_ref_datasets_key_nonempty",
        ),
        CheckConstraint(
            "btrim(title) <> '' and btrim(version_label) <> ''",
            name="ck_ref_datasets_labels_nonempty",
        ),
        CheckConstraint(
            "catalog_url like 'https://%' and download_url like 'https://%'",
            name="ck_ref_datasets_source_urls",
        ),
        CheckConstraint(
            "license_url like 'https://%'",
            name="ck_ref_datasets_license_url",
        ),
        CheckConstraint(
            "archive_sha256 ~ '^[0-9a-f]{64}$' "
            "and content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_ref_datasets_sha256",
        ),
        CheckConstraint(
            "national_row_count > 0 and target_row_count > 0 "
            "and target_row_count <= national_row_count",
            name="ck_ref_datasets_row_counts",
        ),
        UniqueConstraint(
            "dataset_key",
            "content_sha256",
            name="uq_ref_dataset_key_content_sha",
        ),
        Index(
            "ix_ref_datasets_key_reference_date",
            "dataset_key",
            "reference_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    version_label: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_date: Mapped[date] = mapped_column(Date, nullable=False)
    catalog_url: Mapped[str] = mapped_column(Text, nullable=False)
    download_url: Mapped[str] = mapped_column(Text, nullable=False)
    member_name: Mapped[str] = mapped_column(String(255), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    license_name: Mapped[str] = mapped_column(String(100), nullable=False)
    license_url: Mapped[str] = mapped_column(Text, nullable=False)
    attribution: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    national_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_row_count: Mapped[int] = mapped_column(Integer, nullable=False)

    geography_snapshots: Mapped[list["MunicipalityGeographySnapshot"]] = (
        relationship(
            "MunicipalityGeographySnapshot",
            back_populates="dataset_version",
        )
    )


class MunicipalityGeographySnapshot(TimestampMixin, Base):
    __tablename__ = "municipality_geography_snapshots"
    __table_args__ = (
        CheckConstraint(
            "source_municipality_code ~ '^[0-9]{5}000000$'",
            name="ck_muni_geo_source_code",
        ),
        CheckConstraint(
            "source_province_code ~ '^[0-9]{2}$' "
            "and source_province_code = left(source_municipality_code, 2)",
            name="ck_muni_geo_province_code",
        ),
        CheckConstraint(
            "relationship_id > 0 and geographic_code ~ '^[0-9]{5}$'",
            name="ck_muni_geo_identifiers",
        ),
        CheckConstraint(
            "source_population >= 0 and capital_population >= 0 "
            "and capital_population <= source_population",
            name="ck_muni_geo_populations",
        ),
        CheckConstraint(
            "surface_km2 > 0 and perimeter_m > 0",
            name="ck_muni_geo_measurements",
        ),
        CheckConstraint(
            "capital_ine_code ~ '^[0-9]{11}$' "
            "and left(capital_ine_code, 5) = left(source_municipality_code, 5)",
            name="ck_muni_geo_capital_code",
        ),
        CheckConstraint(
            "longitude between -180 and 180 and latitude between -90 and 90",
            name="ck_muni_geo_coordinates",
        ),
        CheckConstraint(
            "btrim(source_province_name) <> '' "
            "and btrim(source_municipality_name) <> '' "
            "and btrim(capital_name) <> '' and btrim(crs) <> ''",
            name="ck_muni_geo_labels_nonempty",
        ),
        UniqueConstraint(
            "municipality_id",
            "dataset_version_id",
            name="uq_muni_geo_municipality_dataset",
        ),
        UniqueConstraint(
            "dataset_version_id",
            "source_municipality_code",
            name="uq_muni_geo_dataset_source_code",
        ),
        UniqueConstraint(
            "dataset_version_id",
            "relationship_id",
            name="uq_muni_geo_dataset_relationship",
        ),
        UniqueConstraint(
            "dataset_version_id",
            "geographic_code",
            name="uq_muni_geo_dataset_geographic_code",
        ),
        UniqueConstraint(
            "dataset_version_id",
            "capital_ine_code",
            name="uq_muni_geo_dataset_capital_code",
        ),
        Index(
            "uq_muni_geo_current",
            "municipality_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_muni_geo_dataset_version_id",
            "dataset_version_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    municipality_id: Mapped[int] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"),
        nullable=False,
    )
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("reference_dataset_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    source_municipality_code: Mapped[str] = mapped_column(
        String(11),
        nullable=False,
    )
    relationship_id: Mapped[int] = mapped_column(Integer, nullable=False)
    geographic_code: Mapped[str] = mapped_column(String(5), nullable=False)
    source_province_code: Mapped[str] = mapped_column(String(2), nullable=False)
    source_province_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    source_municipality_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    source_population: Mapped[int] = mapped_column(Integer, nullable=False)
    surface_km2: Mapped[Decimal] = mapped_column(
        Numeric(12, 6),
        nullable=False,
    )
    perimeter_m: Mapped[Decimal] = mapped_column(
        Numeric(14, 3),
        nullable=False,
    )
    capital_ine_code: Mapped[str] = mapped_column(String(11), nullable=False)
    capital_name: Mapped[str] = mapped_column(String(255), nullable=False)
    capital_population: Mapped[int] = mapped_column(Integer, nullable=False)
    mtn25_sheet: Mapped[str] = mapped_column(String(50), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(12, 9), nullable=False)
    latitude: Mapped[Decimal] = mapped_column(Numeric(12, 9), nullable=False)
    coordinate_origin: Mapped[str] = mapped_column(String(100), nullable=False)
    altitude_m: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    altitude_origin: Mapped[str] = mapped_column(String(100), nullable=False)
    crs: Mapped[str] = mapped_column(
        String(32),
        default="EPSG:4258",
        server_default="EPSG:4258",
        nullable=False,
    )

    municipality: Mapped[Municipality] = relationship(
        "Municipality",
        back_populates="geography_snapshots",
        overlaps="official_geography",
    )
    dataset_version: Mapped[ReferenceDatasetVersion] = relationship(
        "ReferenceDatasetVersion",
        back_populates="geography_snapshots",
        lazy="joined",
    )
