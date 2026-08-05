from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.geo.models import GeoLocation
    from app.municipalities.models import Municipality
    from app.organizations.models import Organization


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# De dónde sale el dato. El padrón oficial del INE ya vive en `municipalities`;
# estas tablas guardan la serie que el ayuntamiento mantiene a mano, y hay que
# poder distinguir una de otra al leerlas.
MUNICIPAL_DATA_SOURCES = ("municipal", "ine", "aemet", "other")

WATER_TREATMENTS = ("none", "chlorination", "filtration", "osmosis", "other")

WATER_METER_STATUSES = ("active", "inactive", "removed")


class PadronAnnualRecord(TimestampMixin, Base):
    """Empadronados a 1 de enero de un año, según los mantiene el ayuntamiento.

    Convive con la serie del INE de `municipalities`: el padrón municipal se
    cierra antes que la cifra oficial y los ayuntamientos trabajan con él
    durante meses. Guardar la procedencia evita que se confundan.
    """

    __tablename__ = "padron_annual_records"
    __table_args__ = (
        CheckConstraint(
            f"source in ({_sql_in(MUNICIPAL_DATA_SOURCES)})",
            name="ck_padron_annual_records_source",
        ),
        CheckConstraint(
            "reference_year between 1900 and 2200",
            name="ck_padron_annual_records_year",
        ),
        CheckConstraint(
            "population >= 0",
            name="ck_padron_annual_records_population",
        ),
        CheckConstraint(
            "men is null or men >= 0",
            name="ck_padron_annual_records_men",
        ),
        CheckConstraint(
            "women is null or women >= 0",
            name="ck_padron_annual_records_women",
        ),
        CheckConstraint(
            "births is null or births >= 0",
            name="ck_padron_annual_records_births",
        ),
        CheckConstraint(
            "deaths is null or deaths >= 0",
            name="ck_padron_annual_records_deaths",
        ),
        # El desglose por sexo no puede superar el total: sería un error de
        # captura que envenenaría cualquier gráfica.
        CheckConstraint(
            "men is null or women is null or men + women <= population",
            name="ck_padron_annual_records_breakdown",
        ),
        UniqueConstraint(
            "organization_id",
            "reference_year",
            name="uq_padron_annual_records_org_year",
        ),
        Index(
            "ix_padron_annual_records_org_year",
            "organization_id",
            "reference_year",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference_year: Mapped[int] = mapped_column(Integer, nullable=False)
    population: Mapped[int] = mapped_column(Integer, nullable=False)
    men: Mapped[int | None] = mapped_column(Integer, nullable=True)
    women: Mapped[int | None] = mapped_column(Integer, nullable=True)
    births: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deaths: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(
        String(20),
        default="municipal",
        server_default="municipal",
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class ClimateRecord(TimestampMixin, Base):
    """Serie climática del municipio, anual o mensual.

    `reference_month` nulo significa el resumen del año entero; con mes, la
    fila es de ese mes. Una única tabla con la escala explícita evita duplicar
    esquema para lo mismo a dos granularidades.
    """

    __tablename__ = "climate_records"
    __table_args__ = (
        CheckConstraint(
            f"source in ({_sql_in(MUNICIPAL_DATA_SOURCES)})",
            name="ck_climate_records_source",
        ),
        CheckConstraint(
            "reference_year between 1900 and 2200",
            name="ck_climate_records_year",
        ),
        CheckConstraint(
            "reference_month is null or reference_month between 1 and 12",
            name="ck_climate_records_month",
        ),
        CheckConstraint(
            "min_temperature_c is null"
            " or max_temperature_c is null"
            " or min_temperature_c <= max_temperature_c",
            name="ck_climate_records_temperature_range",
        ),
        CheckConstraint(
            "precipitation_mm is null or precipitation_mm >= 0",
            name="ck_climate_records_precipitation",
        ),
        UniqueConstraint(
            "organization_id",
            "reference_year",
            "reference_month",
            name="uq_climate_records_org_period",
        ),
        Index(
            "ix_climate_records_org_period",
            "organization_id",
            "reference_year",
            "reference_month",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference_year: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_temperature_c: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    min_temperature_c: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    max_temperature_c: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    precipitation_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 2),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(
        String(20),
        default="municipal",
        server_default="municipal",
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class HouseholdStat(TimestampMixin, Base):
    """Parque de viviendas del municipio en un año."""

    __tablename__ = "household_stats"
    __table_args__ = (
        CheckConstraint(
            f"source in ({_sql_in(MUNICIPAL_DATA_SOURCES)})",
            name="ck_household_stats_source",
        ),
        CheckConstraint(
            "reference_year between 1900 and 2200",
            name="ck_household_stats_year",
        ),
        CheckConstraint(
            "total_dwellings >= 0",
            name="ck_household_stats_total",
        ),
        CheckConstraint(
            "primary_dwellings is null or primary_dwellings >= 0",
            name="ck_household_stats_primary",
        ),
        CheckConstraint(
            "secondary_dwellings is null or secondary_dwellings >= 0",
            name="ck_household_stats_secondary",
        ),
        CheckConstraint(
            "empty_dwellings is null or empty_dwellings >= 0",
            name="ck_household_stats_empty",
        ),
        UniqueConstraint(
            "organization_id",
            "reference_year",
            name="uq_household_stats_org_year",
        ),
        Index(
            "ix_household_stats_org_year",
            "organization_id",
            "reference_year",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference_year: Mapped[int] = mapped_column(Integer, nullable=False)
    total_dwellings: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_dwellings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    secondary_dwellings: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    empty_dwellings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(
        String(20),
        default="municipal",
        server_default="municipal",
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class UtilitySupply(TimestampMixin, Base):
    """Abastecimiento de agua: de dónde viene y cómo se trata."""

    __tablename__ = "utility_supplies"
    __table_args__ = (
        CheckConstraint(
            f"treatment in ({_sql_in(WATER_TREATMENTS)})",
            name="ck_utility_supplies_treatment",
        ),
        CheckConstraint(
            "btrim(name) <> ''",
            name="ck_utility_supplies_name",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_utility_supplies_id_org",
        ),
        Index(
            "ix_utility_supplies_org",
            "organization_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    origin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    treatment: Mapped[str] = mapped_column(
        String(30),
        default="none",
        server_default="none",
        nullable=False,
    )
    last_analysis_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class WaterMeter(TimestampMixin, Base):
    """Contador de agua, situado sobre el mapa como el resto del inventario."""

    __tablename__ = "water_meters"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in(WATER_METER_STATUSES)})",
            name="ck_water_meters_status",
        ),
        CheckConstraint(
            "btrim(code) <> ''",
            name="ck_water_meters_code",
        ),
        ForeignKeyConstraint(
            ["supply_id", "organization_id"],
            ["utility_supplies.id", "utility_supplies.organization_id"],
            name="fk_water_meters_supply_org",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["organization_id", "municipality_id"],
            ["organizations.id", "organizations.municipality_id"],
            name="fk_water_meters_organization_municipality",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["location_id", "organization_id", "municipality_id"],
            [
                "geo_locations.id",
                "geo_locations.organization_id",
                "geo_locations.municipality_id",
            ],
            name="fk_water_meters_location_tenant",
            ondelete="NO ACTION",
            match="SIMPLE",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "organization_id",
            "code",
            name="uq_water_meters_org_code",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_water_meters_id_org",
        ),
        Index(
            "ix_water_meters_org_status",
            "organization_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Sin clave ajena directa a `municipalities`, a diferencia del inventario.
    # La compuesta hacia `organizations(id, municipality_id)` ya obliga a que el
    # municipio sea el de la organización, y `organizations.municipality_id`
    # apunta a su vez a `municipalities`: la integridad se mantiene por
    # transitividad. Añadir además la directa haría que soltar esta tabla en un
    # downgrade pidiera un lock exclusivo sobre `municipalities`, y bastaría un
    # escritor abierto para que la bajada se quedara esperando en vez de fallar.
    municipality_id: Mapped[int] = mapped_column(Integer, nullable=False)
    supply_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("geo_locations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
    )
    installed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    municipality: Mapped["Municipality"] = relationship("Municipality")
    supply: Mapped["UtilitySupply | None"] = relationship(
        "UtilitySupply",
        foreign_keys=[supply_id],
        overlaps="municipality",
    )
    location: Mapped["GeoLocation | None"] = relationship(
        "GeoLocation",
        foreign_keys=[location_id],
        overlaps="municipality,supply",
    )


class WaterMeterReading(TimestampMixin, Base):
    """Lectura de un contador. El consumo se deriva restando la anterior."""

    __tablename__ = "water_meter_readings"
    __table_args__ = (
        CheckConstraint(
            "reading_m3 >= 0",
            name="ck_water_meter_readings_value",
        ),
        ForeignKeyConstraint(
            ["meter_id", "organization_id"],
            ["water_meters.id", "water_meters.organization_id"],
            name="fk_water_meter_readings_meter_org",
            ondelete="CASCADE",
        ),
        # Dos lecturas del mismo contador el mismo día se contradicen.
        UniqueConstraint(
            "meter_id",
            "read_on",
            name="uq_water_meter_readings_meter_date",
        ),
        Index(
            "ix_water_meter_readings_meter_date",
            "meter_id",
            "read_on",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    read_on: Mapped[date] = mapped_column(Date, nullable=False)
    reading_m3: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
