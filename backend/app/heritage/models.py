from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.documents.models import Document
    from app.geo.models import GeoLocation
    from app.organizations.models import Organization


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


HERITAGE_KINDS = (
    "building",
    "archaeological",
    "natural",
    "movable",
    "intangible",
    "other",
)

# Figura de protección. `none` es una respuesta legítima: mucho patrimonio de
# un pueblo es valioso sin estar declarado.
PROTECTION_LEVELS = ("none", "local", "regional", "bic", "unesco")

CONSERVATION_STATES = ("good", "fair", "poor", "ruin", "unknown")

ARCHIVE_KINDS = ("document", "photograph", "map", "book", "audio", "video", "other")

DIGITISATION_STATES = ("not_digitised", "in_progress", "digitised")


class HeritageAsset(TimestampMixin, Base):
    """Bien patrimonial del municipio.

    Se separa del inventario de `municipal_assets` a propósito: aquel existe
    para mantener cosas —una farola se repara y se sustituye—, y este para
    conservarlas. Una ermita del XVI y una luminaria no comparten ni ciclo de
    vida ni vocabulario, y mezclarlas obligaría a que cada consulta del
    mantenimiento filtrase lo que no debe tocar.
    """

    __tablename__ = "heritage_assets"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(HERITAGE_KINDS)})",
            name="ck_heritage_assets_kind",
        ),
        CheckConstraint(
            f"protection_level in ({_sql_in(PROTECTION_LEVELS)})",
            name="ck_heritage_assets_protection",
        ),
        CheckConstraint(
            f"conservation_state in ({_sql_in(CONSERVATION_STATES)})",
            name="ck_heritage_assets_conservation",
        ),
        CheckConstraint(
            "btrim(name) <> ''",
            name="ck_heritage_assets_name",
        ),
        # Un bien declarado tiene expediente; sin él la declaración no consta.
        CheckConstraint(
            "protection_level = 'none' or protection_reference is not null",
            name="ck_heritage_assets_protection_reference",
        ),
        UniqueConstraint(
            "organization_id",
            "slug",
            name="uq_heritage_assets_org_slug",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_heritage_assets_id_org",
        ),
        Index(
            "ix_heritage_assets_org_kind",
            "organization_id",
            "kind",
            "name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(140), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20),
        default="building",
        server_default="building",
        nullable=False,
    )
    # Época en texto libre: «siglo XVI», «finales del XIX», «indeterminada».
    # Forzar un año sería inventar precisión que la fuente no tiene.
    period: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    protection_level: Mapped[str] = mapped_column(
        String(20),
        default="none",
        server_default="none",
        nullable=False,
    )
    protection_reference: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    conservation_state: Mapped[str] = mapped_column(
        String(20),
        default="unknown",
        server_default="unknown",
        nullable=False,
    )
    last_survey_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("geo_locations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    location: Mapped["GeoLocation | None"] = relationship("GeoLocation")


class ArchiveItem(TimestampMixin, Base):
    """Pieza del archivo municipal, con su ubicación física real.

    Un archivo de pueblo vive en cajas y estantes, y lo que más se busca es
    dónde está el papel. La signatura y la ubicación física son lo primero, no
    un adorno: sin ellas la ficha no sirve para lo que se consulta.
    """

    __tablename__ = "archive_items"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(ARCHIVE_KINDS)})",
            name="ck_archive_items_kind",
        ),
        CheckConstraint(
            f"digitisation_state in ({_sql_in(DIGITISATION_STATES)})",
            name="ck_archive_items_digitisation",
        ),
        CheckConstraint(
            f"conservation_state in ({_sql_in(CONSERVATION_STATES)})",
            name="ck_archive_items_conservation",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_archive_items_title",
        ),
        # Digitalizado significa que existe el fichero; si no, es una promesa.
        CheckConstraint(
            "digitisation_state <> 'digitised' or document_id is not null",
            name="ck_archive_items_digitised_document",
        ),
        CheckConstraint(
            "end_year is null or start_year is null or end_year >= start_year",
            name="ck_archive_items_year_range",
        ),
        UniqueConstraint(
            "organization_id",
            "reference",
            name="uq_archive_items_org_reference",
        ),
        Index(
            "ix_archive_items_org_kind",
            "organization_id",
            "kind",
            "title",
        ),
        Index(
            "ix_archive_items_org_years",
            "organization_id",
            "start_year",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20),
        default="document",
        server_default="document",
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Años sueltos en lugar de fechas: de una caja se sabe el periodo que
    # abarca, casi nunca el día.
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    physical_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conservation_state: Mapped[str] = mapped_column(
        String(20),
        default="unknown",
        server_default="unknown",
        nullable=False,
    )
    digitisation_state: Mapped[str] = mapped_column(
        String(20),
        default="not_digitised",
        server_default="not_digitised",
        nullable=False,
    )
    # El escaneo, cuando existe, vive en `documents` como el escudo (ADR-038).
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    heritage_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("heritage_assets.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    document: Mapped["Document | None"] = relationship("Document")
    heritage_asset: Mapped["HeritageAsset | None"] = relationship("HeritageAsset")
