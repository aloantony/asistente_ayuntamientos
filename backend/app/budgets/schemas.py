from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BudgetStatus = Literal["draft", "approved", "executing", "settled"]
BudgetLineKind = Literal["income", "expense"]
AmendmentKind = Literal[
    "credit_transfer",
    "extraordinary_credit",
    "supplement",
    "other",
]
AmendmentStatus = Literal["draft", "approved", "rejected"]
TreasuryDirection = Literal["inflow", "outflow"]


def blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class BudgetRead(BaseModel):
    id: int
    organization_id: int
    reference_year: int
    status: BudgetStatus
    approved_on: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BudgetCreate(BaseModel):
    organization_id: int
    reference_year: int = Field(ge=1900, le=2200)
    status: BudgetStatus = "draft"
    approved_on: date | None = None
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("notes", mode="before")(blank_to_none)

    @model_validator(mode="after")
    def check_approval(self):
        # Salir de borrador significa que consta cuándo se aprobó.
        if (self.status != "draft") != (self.approved_on is not None):
            raise ValueError(
                "approved_on is required exactly when the budget leaves draft"
            )
        return self


class BudgetLineRead(BaseModel):
    id: int
    budget_id: int
    organization_id: int
    code: str
    name: str
    kind: BudgetLineKind
    amount: Decimal
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BudgetLineCreate(BaseModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=255)
    kind: BudgetLineKind
    # Siempre positivo: la dirección la marca `kind`.
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class BudgetAmendmentRead(BaseModel):
    id: int
    budget_id: int
    organization_id: int
    reference: str | None
    kind: AmendmentKind
    status: AmendmentStatus
    amount: Decimal
    reason: str | None
    approved_on: date | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BudgetAmendmentCreate(BaseModel):
    reference: str | None = Field(default=None, max_length=100)
    kind: AmendmentKind
    status: AmendmentStatus = "draft"
    # Aquí el signo sí importa: una modificación puede retirar crédito.
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    reason: str | None = None
    approved_on: date | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("reference", "reason", mode="before")(
        blank_to_none
    )

    @model_validator(mode="after")
    def check_amount_and_approval(self):
        if self.amount == 0:
            raise ValueError("An amendment that moves nothing is not an amendment")
        if (self.status == "approved") != (self.approved_on is not None):
            raise ValueError(
                "approved_on is required exactly when the amendment is approved"
            )
        return self


class BudgetExpenseRead(BaseModel):
    id: int
    line_id: int
    organization_id: int
    concept: str
    supplier: str | None
    amount: Decimal
    incurred_on: date
    invoice_reference: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BudgetExpenseCreate(BaseModel):
    concept: str = Field(min_length=1, max_length=255)
    supplier: str | None = Field(default=None, max_length=255)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    incurred_on: date
    invoice_reference: str | None = Field(default=None, max_length=100)

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator(
        "supplier", "invoice_reference", mode="before"
    )(blank_to_none)


class TreasuryMovementRead(BaseModel):
    id: int
    organization_id: int
    direction: TreasuryDirection
    concept: str
    amount: Decimal
    moved_on: date
    account_label: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TreasuryMovementCreate(BaseModel):
    organization_id: int
    direction: TreasuryDirection
    concept: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    moved_on: date
    account_label: str | None = Field(default=None, max_length=120)
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    _blank_optional_text = field_validator("account_label", "notes", mode="before")(
        blank_to_none
    )


class BudgetExecution(BaseModel):
    """Ejecución derivada, nunca guardada.

    Los totales se calculan sumando partidas, modificaciones aprobadas y gastos
    en cada consulta. Guardarlos obligaría a recalcularlos en cada escritura y a
    convivir con un total que dejó de cuadrar tras un fallo a medio camino.
    """

    budget_id: int
    reference_year: int
    status: BudgetStatus
    total_income: Decimal
    total_expense: Decimal
    approved_amendments: Decimal
    # Crédito disponible = gasto presupuestado + modificaciones - gasto real.
    executed_expense: Decimal
    available_credit: Decimal
