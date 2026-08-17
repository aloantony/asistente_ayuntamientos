from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.budgets.access import (
    get_budgets_organization_for_read,
    get_budgets_organization_for_write,
    require_budgets_permission,
)
from app.budgets.models import (
    BudgetAmendment,
    BudgetExpense,
    BudgetLine,
    MunicipalBudget,
    TreasuryMovement,
)
from app.budgets.schemas import (
    BudgetAmendmentCreate,
    BudgetAmendmentRead,
    BudgetCreate,
    BudgetExecution,
    BudgetExpenseCreate,
    BudgetExpenseRead,
    BudgetLineCreate,
    BudgetLineKind,
    BudgetLineRead,
    BudgetRead,
    TreasuryDirection,
    TreasuryMovementCreate,
    TreasuryMovementRead,
)
from app.core.pagination import PageParams, page_params, paginate
from app.db.session import get_db
from app.users.models import User

router = APIRouter(prefix="/budgets", tags=["budgets"])

ZERO = Decimal("0.00")


@router.get("", response_model=list[BudgetRead])
def list_budgets(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[MunicipalBudget]:
    get_budgets_organization_for_read(db, organization_id)
    require_budgets_permission(db, current_user, organization_id, "budgets.view")

    query = (
        select(MunicipalBudget)
        .where(MunicipalBudget.organization_id == organization_id)
        .order_by(MunicipalBudget.reference_year.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post("", response_model=BudgetRead, status_code=status.HTTP_201_CREATED)
def create_budget(
    payload: BudgetCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MunicipalBudget:
    get_budgets_organization_for_write(db, payload.organization_id)
    require_budgets_permission(
        db,
        current_user,
        payload.organization_id,
        "budgets.edit",
    )

    budget = MunicipalBudget(**payload.model_dump())
    db.add(budget)
    commit_or_conflict(db, "A budget already exists for that year")
    db.refresh(budget)
    return budget


@router.get("/{budget_id}/lines", response_model=list[BudgetLineRead])
def list_budget_lines(
    budget_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    kind: BudgetLineKind | None = None,
) -> list[BudgetLine]:
    budget = get_readable_budget(db, budget_id, current_user)

    query = (
        select(BudgetLine)
        .where(BudgetLine.budget_id == budget.id)
        .order_by(BudgetLine.kind, BudgetLine.code)
    )
    if kind is not None:
        query = query.where(BudgetLine.kind == kind)
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/{budget_id}/lines",
    response_model=BudgetLineRead,
    status_code=status.HTTP_201_CREATED,
)
def create_budget_line(
    budget_id: int,
    payload: BudgetLineCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BudgetLine:
    budget = get_writable_budget(db, budget_id, current_user)
    ensure_budget_is_open(budget)

    line = BudgetLine(
        **payload.model_dump(),
        budget_id=budget.id,
        organization_id=budget.organization_id,
    )
    db.add(line)
    commit_or_conflict(db, "A line with that code already exists in the budget")
    db.refresh(line)
    return line


@router.get("/{budget_id}/amendments", response_model=list[BudgetAmendmentRead])
def list_budget_amendments(
    budget_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[BudgetAmendment]:
    budget = get_readable_budget(db, budget_id, current_user)

    query = (
        select(BudgetAmendment)
        .where(BudgetAmendment.budget_id == budget.id)
        .order_by(BudgetAmendment.id.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/{budget_id}/amendments",
    response_model=BudgetAmendmentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_budget_amendment(
    budget_id: int,
    payload: BudgetAmendmentCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BudgetAmendment:
    budget = get_writable_budget(db, budget_id, current_user)
    # Una modificación de crédito modifica algo ya aprobado; sobre un borrador
    # se cambia la partida directamente.
    if budget.status == "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A draft budget is edited directly, not amended",
        )
    if payload.status == "approved":
        require_budgets_permission(
            db,
            current_user,
            budget.organization_id,
            "budgets.manage",
        )

    amendment = BudgetAmendment(
        **payload.model_dump(),
        budget_id=budget.id,
        organization_id=budget.organization_id,
    )
    db.add(amendment)
    commit_or_conflict(db, "Amendment could not be saved")
    db.refresh(amendment)
    return amendment


@router.get("/lines/{line_id}/expenses", response_model=list[BudgetExpenseRead])
def list_line_expenses(
    line_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
) -> list[BudgetExpense]:
    line = get_existing_line(db, line_id)
    get_budgets_organization_for_read(db, line.organization_id)
    require_budgets_permission(
        db,
        current_user,
        line.organization_id,
        "budgets.view",
    )

    query = (
        select(BudgetExpense)
        .where(BudgetExpense.line_id == line.id)
        .order_by(BudgetExpense.incurred_on.desc(), BudgetExpense.id.desc())
    )
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/lines/{line_id}/expenses",
    response_model=BudgetExpenseRead,
    status_code=status.HTTP_201_CREATED,
)
def create_line_expense(
    line_id: int,
    payload: BudgetExpenseCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BudgetExpense:
    line = get_existing_line(db, line_id)
    get_budgets_organization_for_write(db, line.organization_id)
    require_budgets_permission(
        db,
        current_user,
        line.organization_id,
        "budgets.edit",
    )
    # Imputar gasto a una partida de ingresos no significa nada.
    if line.kind != "expense":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only expense lines take expenses",
        )

    expense = BudgetExpense(
        **payload.model_dump(),
        line_id=line.id,
        organization_id=line.organization_id,
    )
    db.add(expense)
    commit_or_conflict(db, "Expense could not be saved")
    db.refresh(expense)
    return expense


@router.get("/{budget_id}/execution", response_model=BudgetExecution)
def get_budget_execution(
    budget_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> BudgetExecution:
    budget = get_readable_budget(db, budget_id, current_user)

    def line_total(kind: str) -> Decimal:
        return db.scalar(
            select(func.coalesce(func.sum(BudgetLine.amount), 0)).where(
                BudgetLine.budget_id == budget.id,
                BudgetLine.kind == kind,
            )
        ) or ZERO

    approved_amendments = db.scalar(
        select(func.coalesce(func.sum(BudgetAmendment.amount), 0)).where(
            BudgetAmendment.budget_id == budget.id,
            # Solo las aprobadas mueven crédito; un borrador es una intención.
            BudgetAmendment.status == "approved",
        )
    ) or ZERO
    executed = db.scalar(
        select(func.coalesce(func.sum(BudgetExpense.amount), 0))
        .select_from(BudgetExpense)
        .join(BudgetLine, BudgetLine.id == BudgetExpense.line_id)
        .where(BudgetLine.budget_id == budget.id)
    ) or ZERO

    total_expense = line_total("expense")
    return BudgetExecution(
        budget_id=budget.id,
        reference_year=budget.reference_year,
        status=budget.status,
        total_income=line_total("income"),
        total_expense=total_expense,
        approved_amendments=approved_amendments,
        executed_expense=executed,
        available_credit=total_expense + approved_amendments - executed,
    )


@router.get("/treasury/movements", response_model=list[TreasuryMovementRead])
def list_treasury_movements(
    organization_id: int,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
    page: Annotated[PageParams, Depends(page_params)],
    direction: TreasuryDirection | None = None,
) -> list[TreasuryMovement]:
    get_budgets_organization_for_read(db, organization_id)
    require_budgets_permission(db, current_user, organization_id, "budgets.view")

    query = (
        select(TreasuryMovement)
        .where(TreasuryMovement.organization_id == organization_id)
        .order_by(TreasuryMovement.moved_on.desc(), TreasuryMovement.id.desc())
    )
    if direction is not None:
        query = query.where(TreasuryMovement.direction == direction)
    return list(db.scalars(paginate(db, query, page, response)))


@router.post(
    "/treasury/movements",
    response_model=TreasuryMovementRead,
    status_code=status.HTTP_201_CREATED,
)
def create_treasury_movement(
    payload: TreasuryMovementCreate,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TreasuryMovement:
    get_budgets_organization_for_write(db, payload.organization_id)
    require_budgets_permission(
        db,
        current_user,
        payload.organization_id,
        "budgets.edit",
    )

    movement = TreasuryMovement(**payload.model_dump())
    db.add(movement)
    commit_or_conflict(db, "Treasury movement could not be saved")
    db.refresh(movement)
    return movement


def get_existing_budget(db: Session, budget_id: int) -> MunicipalBudget:
    budget = db.get(MunicipalBudget, budget_id)
    if budget is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Budget not found",
        )
    return budget


def get_existing_line(db: Session, line_id: int) -> BudgetLine:
    line = db.get(BudgetLine, line_id)
    if line is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Budget line not found",
        )
    return line


def get_readable_budget(
    db: Session,
    budget_id: int,
    current_user: User,
) -> MunicipalBudget:
    budget = get_existing_budget(db, budget_id)
    get_budgets_organization_for_read(db, budget.organization_id)
    require_budgets_permission(
        db,
        current_user,
        budget.organization_id,
        "budgets.view",
    )
    return budget


def get_writable_budget(
    db: Session,
    budget_id: int,
    current_user: User,
) -> MunicipalBudget:
    budget = get_existing_budget(db, budget_id)
    get_budgets_organization_for_write(db, budget.organization_id)
    require_budgets_permission(
        db,
        current_user,
        budget.organization_id,
        "budgets.edit",
    )
    return budget


def ensure_budget_is_open(budget: MunicipalBudget) -> None:
    # Un presupuesto liquidado es historia cerrada: tocar sus partidas
    # cambiaría lo que ya se rindió.
    if budget.status == "settled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A settled budget cannot take new lines",
        )


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from None
