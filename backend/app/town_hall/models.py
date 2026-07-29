from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.organizations.models import Organization
    from app.users.models import User

# Tipos de bloque. Los tres últimos quedan reservados para la fase de contenido
# del módulo (epígrafes del Ayuntamiento): el check los admite desde ya para no
# necesitar otra migración, pero la API solo crea los dos de navegación.
BLOCK_TYPES = ("nav_section", "nav_item", "epigraph", "section", "item")


class MunicipalProfile(TimestampMixin, Base):
    """Presentación del Ayuntamiento para una organización.

    El contenido cuelga de la organización (el inquilino) y no del municipio,
    que es dato de referencia global compartido entre inquilinos.
    """

    __tablename__ = "municipal_profiles"
    __table_args__ = (
        CheckConstraint(
            "weather_latitude is null or (weather_latitude >= -90 and weather_latitude <= 90)",
            name="ck_municipal_profiles_weather_latitude_range",
        ),
        CheckConstraint(
            "weather_longitude is null or (weather_longitude >= -180 and weather_longitude <= 180)",
            name="ck_municipal_profiles_weather_longitude_range",
        ),
        CheckConstraint(
            "shield_size_bytes is null or shield_size_bytes >= 0",
            name="ck_municipal_profiles_shield_size_bytes_positive",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), unique=True, index=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    weather_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    weather_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    weather_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    weather_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    shield_storage_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    shield_content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shield_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    updated_by: Mapped["User | None"] = relationship("User")


class MunicipalBlock(TimestampMixin, Base):
    """Árbol genérico de contenido del Ayuntamiento.

    Hoy solo almacena la navegación configurable de la barra del municipio
    (`nav_section` con sus `nav_item`), pero la forma —padre, orden, título,
    cuerpo y carga libre en JSON— está pensada para absorber los epígrafes
    editables del diseño sin cambiar el esquema. Ver ADR-030.
    """

    __tablename__ = "municipal_blocks"
    __table_args__ = (
        CheckConstraint(
            "block_type in ('nav_section', 'nav_item', 'epigraph', 'section', 'item')",
            name="ck_municipal_blocks_block_type",
        ),
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_municipal_blocks_status",
        ),
        CheckConstraint("position >= 0", name="ck_municipal_blocks_position_positive"),
        Index("ix_municipal_blocks_org_parent_position", "organization_id", "parent_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("municipal_blocks.id", ondelete="CASCADE"), index=True, nullable=True)
    block_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", server_default="active", nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")
    parent: Mapped["MunicipalBlock | None"] = relationship("MunicipalBlock", back_populates="children", remote_side=[id])
    children: Mapped[list["MunicipalBlock"]] = relationship("MunicipalBlock", back_populates="parent", cascade="all, delete-orphan")
    created_by: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_id])
    updated_by: Mapped["User | None"] = relationship("User", foreign_keys=[updated_by_id])
