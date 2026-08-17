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
    from app.organizations.models import Organization


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


BUDGET_STATUSES = ("draft", "approved", "executing", "settled")

# Ingreso o gasto: el signo no se guarda en el importe, se dice aquí.
BUDGET_LINE_KINDS = ("income", "expense")

AMENDMENT_KINDS = ("credit_transfer", "extraordinary_credit", "supplement", "other")

AMENDMENT_STATUSES = ("draft", "approved", "rejected")

TREASURY_DIRECTIONS = ("inflow", "outflow")


class MunicipalBudget(TimestampMixin, Base):
    """Presupuesto anual del ayuntamiento.

    Un año, un presupuesto. Las modificaciones no crean otro: se anotan como
    `budget_amendments`, para que el inicial siga siendo legible tal como se
    aprobó.
    """

    __tablename__ = "municipal_budgets"
    __table_args__ = (
        CheckConstraint(
            f"status in ({_sql_in(BUDGET_STATUSES)})",
            name="ck_municipal_budgets_status",
        ),
        CheckConstraint(
            "reference_year between 1900 and 2200",
            name="ck_municipal_budgets_year",
        ),
        CheckConstraint(
            "(status <> 'draft') = (approved_on is not null)",
            name="ck_municipal_budgets_approved_on",
        ),
        UniqueConstraint(
            "organization_id",
            "reference_year",
            name="uq_municipal_budgets_org_year",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_municipal_budgets_id_org",
        ),
        Index(
            "ix_municipal_budgets_org_year",
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
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    approved_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship("Organization")


class BudgetLine(TimestampMixin, Base):
    """Partida del presupuesto, de ingreso o de gasto."""

    __tablename__ = "budget_lines"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(BUDGET_LINE_KINDS)})",
            name="ck_budget_lines_kind",
        ),
        CheckConstraint(
            "amount >= 0",
            name="ck_budget_lines_amount",
        ),
        CheckConstraint(
            "btrim(code) <> ''",
            name="ck_budget_lines_code",
        ),
        ForeignKeyConstraint(
            ["budget_id", "organization_id"],
            ["municipal_budgets.id", "municipal_budgets.organization_id"],
            name="fk_budget_lines_budget_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "budget_id",
            "code",
            name="uq_budget_lines_budget_code",
        ),
        UniqueConstraint(
            "id",
            "organization_id",
            name="uq_budget_lines_id_org",
        ),
        Index(
            "ix_budget_lines_budget_kind",
            "budget_id",
            "kind",
            "code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # Importe siempre positivo: la dirección la marca `kind`, para que sumar
    # ingresos y gastos por separado no dependa de leer bien un signo.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


class BudgetAmendment(TimestampMixin, Base):
    """Modificación de crédito sobre el presupuesto aprobado."""

    __tablename__ = "budget_amendments"
    __table_args__ = (
        CheckConstraint(
            f"kind in ({_sql_in(AMENDMENT_KINDS)})",
            name="ck_budget_amendments_kind",
        ),
        CheckConstraint(
            f"status in ({_sql_in(AMENDMENT_STATUSES)})",
            name="ck_budget_amendments_status",
        ),
        CheckConstraint(
            "amount <> 0",
            name="ck_budget_amendments_amount",
        ),
        CheckConstraint(
            "(status = 'approved') = (approved_on is not null)",
            name="ck_budget_amendments_approved_on",
        ),
        ForeignKeyConstraint(
            ["budget_id", "organization_id"],
            ["municipal_budgets.id", "municipal_budgets.organization_id"],
            name="fk_budget_amendments_budget_org",
            ondelete="CASCADE",
        ),
        Index(
            "ix_budget_amendments_budget_status",
            "budget_id",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
    )
    # Aquí el signo sí importa: una modificación puede quitar crédito.
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class BudgetExpense(TimestampMixin, Base):
    """Gasto imputado a una partida. La ejecución se deriva sumándolos."""

    __tablename__ = "budget_expenses"
    __table_args__ = (
        CheckConstraint(
            "amount >= 0",
            name="ck_budget_expenses_amount",
        ),
        CheckConstraint(
            "btrim(concept) <> ''",
            name="ck_budget_expenses_concept",
        ),
        ForeignKeyConstraint(
            ["line_id", "organization_id"],
            ["budget_lines.id", "budget_lines.organization_id"],
            name="fk_budget_expenses_line_org",
            ondelete="CASCADE",
        ),
        Index(
            "ix_budget_expenses_line_date",
            "line_id",
            "incurred_on",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(Integer, nullable=False)
    concept: Mapped[str] = mapped_column(String(255), nullable=False)
    supplier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    invoice_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)


class TreasuryMovement(TimestampMixin, Base):
    """Movimiento de tesorería. Independiente del presupuesto a propósito.

    El dinero entra y sale con su propio calendario: una factura puede
    imputarse a una partida de un año y pagarse en el siguiente. Atar la
    tesorería al presupuesto obligaría a mentir en una de las dos fechas.
    """

    __tablename__ = "treasury_movements"
    __table_args__ = (
        CheckConstraint(
            f"direction in ({_sql_in(TREASURY_DIRECTIONS)})",
            name="ck_treasury_movements_direction",
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_treasury_movements_amount",
        ),
        CheckConstraint(
            "btrim(concept) <> ''",
            name="ck_treasury_movements_concept",
        ),
        Index(
            "ix_treasury_movements_org_date",
            "organization_id",
            "moved_on",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    concept: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    moved_on: Mapped[date] = mapped_column(Date, nullable=False)
    account_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
