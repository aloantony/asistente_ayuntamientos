from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.municipalities.models import Municipality
    from app.organizations.models import Organization
    from app.users.models import User


class GeoLocation(TimestampMixin, Base):
    __tablename__ = "geo_locations"
    __table_args__ = (
        CheckConstraint("geometry_type in ('point', 'line', 'polygon')", name="ck_geo_locations_geometry_type"),
        CheckConstraint("source in ('user_provided', 'assistant_extracted', 'geocoded', 'imported', 'manual_review')", name="ck_geo_locations_source"),
        CheckConstraint("review_status in ('draft', 'proposed', 'reviewed', 'rejected')", name="ck_geo_locations_review_status"),
        CheckConstraint("latitude is null or (latitude >= -90 and latitude <= 90)", name="ck_geo_locations_latitude_range"),
        CheckConstraint("longitude is null or (longitude >= -180 and longitude <= 180)", name="ck_geo_locations_longitude_range"),
        CheckConstraint("confidence is null or (confidence >= 0 and confidence <= 1)", name="ck_geo_locations_confidence_range"),
        CheckConstraint("geometry_type != 'point' or (latitude is not null and longitude is not null)", name="ck_geo_locations_point_coordinates"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=True)
    municipality_id: Mapped[int | None] = mapped_column(ForeignKey("municipalities.id", ondelete="SET NULL"), index=True, nullable=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(30), default="point", server_default="point", nullable=False)
    geometry_json: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    address_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    place_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cadastral_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="user_provided", server_default="user_provided", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(30), default="proposed", server_default="proposed", nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)

    organization: Mapped["Organization | None"] = relationship("Organization")
    municipality: Mapped["Municipality | None"] = relationship("Municipality")
    created_by: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_id])
    reviewed_by: Mapped["User | None"] = relationship("User", foreign_keys=[reviewed_by_id])
    attachments: Mapped[list["EntityLocation"]] = relationship("EntityLocation", back_populates="location", cascade="all, delete-orphan")


class EntityLocation(Base):
    __tablename__ = "entity_locations"
    __table_args__ = (
        CheckConstraint("entity_type in ('requirement', 'project')", name="ck_entity_locations_entity_type"),
        CheckConstraint("role in ('primary', 'affected_area', 'reference')", name="ck_entity_locations_role"),
        UniqueConstraint("entity_type", "entity_id", "role", name="uq_entity_locations_entity_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("geo_locations.id", ondelete="CASCADE"), index=True, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(30), default="primary", server_default="primary", nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True)

    location: Mapped[GeoLocation] = relationship("GeoLocation", back_populates="attachments")
    created_by: Mapped["User | None"] = relationship("User")
