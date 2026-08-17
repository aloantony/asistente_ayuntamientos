from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
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
    from app.organizations.models import Organization


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

PROCEDURE_CHANNELS = ("in_person", "online", "both")

PROCEDURE_STATUSES = ("active", "archived")

LICENCE_KINDS = ("works", "opening", "occupancy", "environmental", "other")

LICENCE_STATUSES = (
    "requested",
    "in_review",
    "granted",
    "denied",
    "expired",
    "withdrawn",
)

CONTRACT_PROCEDURES = ("minor", "open", "negotiated", "framework", "other")

CONTRACT_STATUSES = ("draft", "published", "awarded", "executed", "cancelled")

GRANT_STATUSES = ("open", "applied", "granted", "denied", "settled")

# Ejes de publicidad activa que la ley de transparencia obliga a publicar.
TRANSPARENCY_AREAS = (
    "institutional",
    "regulatory",
    "economic",
    "contracts",
    "grants",
    "other",
)


class OfficeHour(TimestampMixin, Base):
    """Horario de atención al público de una dependencia municipal."""

    __tablename__ = "office_hours"
    __table_args__ = (
        CheckConstraint(
            f"weekday in ({_sql_in(WEEKDAYS)})",
            name="ck_office_hours_weekday",
        ),
        CheckConstraint(
            "closes_at > opens_at",
            name="ck_office_hours_range",
        ),
        CheckConstraint(
            "btrim(office_name) <> ''",
            name="ck_office_hours_office_name",
        ),
        Index(
            "ix_office_hours_org_weekday",
            "organization_id",
            "weekday",
            "opens_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    office_name: Mapped[str] = mapped_column(String(255), nullable=False)
    weekday: Mapped[str] = mapped_column(String(20), nullable=False)
    # Minutos desde medianoche: comparar y ordenar franjas es aritmética simple,
    # y no arrastra la zona horaria que traería un TIME con tz.
    opens_at: Mapped[int] = mapped_column(Integer, nullable=False)
    closes_at: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class MunicipalProcedure(TimestampMixin, Base):
    """Trámite que la ciudadanía puede iniciar ante el ayuntamiento."""

    __tablename__ = "municipal_procedures"
    __table_args__ = (
        CheckConstraint(
            f"channel in ({_sql_in(PROCEDURE_CHANNELS)})",
            name="ck_municipal_procedures_channel",
        ),
        CheckConstraint(
            f"status in ({_sql_in(PROCEDURE_STATUSES)})",
            name="ck_municipal_procedures_status",
        ),
        CheckConstraint(
            "btrim(name) <> ''",
            name="ck_municipal_procedures_name",
        ),
        CheckConstraint(
            "deadline_days is null or deadline_days >= 0",
            name="ck_municipal_procedures_deadline",
        ),
        UniqueConstraint(
            "organization_id",
            "slug",
            name="uq_municipal_procedures_org_slug",
        ),
        Index(
            "ix_municipal_procedures_org_status",
            "organization_id",
            "status",
            "name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(
        String(20),
        default="in_person",
        server_default="in_person",
        nullable=False,
    )
    deadline_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fee_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # La sede electrónica de la fase 7 leerá esta bandera; hasta entonces el
    # trámite existe pero no se publica.
    publish_to_sede: Mapped[bool] = mapped_column(
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

    organization: Mapped["Organization"] = relationship("Organization")


class MunicipalLicence(TimestampMixin, Base):
    """Licencia solicitada o concedida por el ayuntamiento."""

    __tablename__ = "municipal_licences"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(LICENCE_KINDS)})",
            name="ck_municipal_licences_kind",
        ),
        CheckConstraint(
            f"status in ({_sql_in(LICENCE_STATUSES)})",
            name="ck_municipal_licences_status",
        ),
        CheckConstraint(
            "resolved_on is null or resolved_on >= requested_on",
            name="ck_municipal_licences_resolution_order",
        ),
        # Una licencia resuelta tiene fecha de resolución, y una sin resolver no.
        CheckConstraint(
            "(status in ('granted', 'denied')) = (resolved_on is not null)",
            name="ck_municipal_licences_resolution_presence",
        ),
        CheckConstraint(
            "fee_amount is null or fee_amount >= 0",
            name="ck_municipal_licences_fee",
        ),
        UniqueConstraint(
            "organization_id",
            "reference",
            name="uq_municipal_licences_org_reference",
        ),
        Index(
            "ix_municipal_licences_org_status_requested",
            "organization_id",
            "status",
            "requested_on",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    applicant: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="requested",
        server_default="requested",
        nullable=False,
    )
    requested_on: Mapped[date] = mapped_column(Date, nullable=False)
    resolved_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    fee_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )

    organization: Mapped["Organization"] = relationship("Organization")


class MunicipalContract(TimestampMixin, Base):
    """Contrato público del ayuntamiento, para el perfil de contratante."""

    __tablename__ = "municipal_contracts"
    __table_args__ = (
        CheckConstraint(
            f"procedure_type in ({_sql_in(CONTRACT_PROCEDURES)})",
            name="ck_municipal_contracts_procedure",
        ),
        CheckConstraint(
            f"status in ({_sql_in(CONTRACT_STATUSES)})",
            name="ck_municipal_contracts_status",
        ),
        CheckConstraint(
            "base_amount is null or base_amount >= 0",
            name="ck_municipal_contracts_base_amount",
        ),
        CheckConstraint(
            "awarded_amount is null or awarded_amount >= 0",
            name="ck_municipal_contracts_awarded_amount",
        ),
        # Adjudicado significa que hay a quién y por cuánto.
        CheckConstraint(
            "status <> 'awarded'"
            " or (awarded_to is not null and awarded_amount is not null)",
            name="ck_municipal_contracts_award_details",
        ),
        UniqueConstraint(
            "organization_id",
            "reference",
            name="uq_municipal_contracts_org_reference",
        ),
        Index(
            "ix_municipal_contracts_org_status",
            "organization_id",
            "status",
            "published_on",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    procedure_type: Mapped[str] = mapped_column(
        String(30),
        default="minor",
        server_default="minor",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    base_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    awarded_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    awarded_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    awarded_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class MunicipalGrant(TimestampMixin, Base):
    """Subvención que el ayuntamiento solicita o concede."""

    __tablename__ = "municipal_grants"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in(GRANT_STATUSES)})",
            name="ck_municipal_grants_status",
        ),
        CheckConstraint(
            "requested_amount is null or requested_amount >= 0",
            name="ck_municipal_grants_requested",
        ),
        CheckConstraint(
            "granted_amount is null or granted_amount >= 0",
            name="ck_municipal_grants_granted",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_municipal_grants_title",
        ),
        Index(
            "ix_municipal_grants_org_status",
            "organization_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    funder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="open",
        server_default="open",
        nullable=False,
    )
    requested_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    granted_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        nullable=True,
    )
    application_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    resolved_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class TransparencyItem(TimestampMixin, Base):
    """Elemento de publicidad activa del portal de transparencia."""

    __tablename__ = "transparency_items"
    __table_args__ = (
        CheckConstraint(
            f"area in ({_sql_in(TRANSPARENCY_AREAS)})",
            name="ck_transparency_items_area",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_transparency_items_title",
        ),
        Index(
            "ix_transparency_items_org_area",
            "organization_id",
            "area",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    area: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_period: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    publish_to_sede: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship("Organization")
