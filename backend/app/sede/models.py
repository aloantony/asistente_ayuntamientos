from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.ordinances.models import Ordinance
    from app.organizations.models import Organization


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


TAX_KINDS = ("tax", "fee", "special_levy", "price", "other")

# Cómo se expresa la cuota: un tipo sobre una base, un importe fijo, o un texto
# cuando la ordenanza usa una tarifa que no cabe en un número.
TAX_RATE_KINDS = ("percentage", "fixed_amount", "tariff")


class MunicipalTax(TimestampMixin, Base):
    """Tributo municipal, opcionalmente respaldado por su ordenanza fiscal.

    Es el único dominio que estrena la sede electrónica: todo lo demás que
    publica ya existe en administración, comunicación, plenos y normativa. Un
    tributo puede existir antes de que su ordenanza esté cargada en el
    repositorio, así que la referencia es opcional.
    """

    __tablename__ = "municipal_taxes"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(TAX_KINDS)})",
            name="ck_municipal_taxes_kind",
        ),
        CheckConstraint(
            f"rate_kind in ({_sql_in(TAX_RATE_KINDS)})",
            name="ck_municipal_taxes_rate_kind",
        ),
        CheckConstraint(
            "btrim(name) <> ''",
            name="ck_municipal_taxes_name",
        ),
        CheckConstraint(
            "rate_value is null or rate_value >= 0",
            name="ck_municipal_taxes_rate_value",
        ),
        # Un tipo o un importe necesitan su número; una tarifa, su descripción.
        CheckConstraint(
            "(rate_kind = 'tariff') = (rate_value is null)",
            name="ck_municipal_taxes_rate_shape",
        ),
        UniqueConstraint(
            "organization_id",
            "slug",
            name="uq_municipal_taxes_org_slug",
        ),
        Index(
            "ix_municipal_taxes_org_kind",
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
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20),
        default="tax",
        server_default="tax",
        nullable=False,
    )
    rate_kind: Mapped[str] = mapped_column(
        String(20),
        default="tariff",
        server_default="tariff",
        nullable=False,
    )
    rate_value: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )
    rate_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    taxable_base: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # La ordenanza fiscal vive en `ordinances`; aquí sólo se referencia, y sin
    # exigirla: el tributo puede estar vigente y la ordenanza sin digitalizar.
    ordinance_id: Mapped[int | None] = mapped_column(
        ForeignKey("ordinances.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    publish_to_sede: Mapped[bool] = mapped_column(
        default=True,
        server_default="true",
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship("Organization")
    ordinance: Mapped["Ordinance | None"] = relationship("Ordinance")
