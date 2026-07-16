from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Float, Integer, SmallInteger, String, Text
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
