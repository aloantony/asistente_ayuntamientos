import type {
  BudgetExecution,
  CouncilSession,
  MunicipalBudget,
  TreasuryMovement,
} from "../components/types";
import { adminRequest, adminRequestWithTotal } from "./api";

// Presupuesto, tesorería y plenos (ADR-041).

export function fetchBudgets(organizationId: number, signal?: AbortSignal) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "10",
  });

  return adminRequestWithTotal<MunicipalBudget[]>(
    `/budgets?${params.toString()}`,
    "",
    "No se pudo cargar el presupuesto municipal.",
    { signal },
  );
}

/** La ejecución se calcula en el servidor en cada consulta, no se guarda. */
export function fetchBudgetExecution(budgetId: number, signal?: AbortSignal) {
  return adminRequest<BudgetExecution>(
    `/budgets/${budgetId}/execution`,
    "",
    "No se pudo calcular la ejecución presupuestaria.",
    { signal },
  );
}

export function fetchTreasuryMovements(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "12",
  });

  return adminRequestWithTotal<TreasuryMovement[]>(
    `/budgets/treasury/movements?${params.toString()}`,
    "",
    "No se pudo cargar la tesorería.",
    { signal },
  );
}

export function fetchCouncilSessions(
  organizationId: number,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    organization_id: String(organizationId),
    limit: "8",
  });

  return adminRequestWithTotal<CouncilSession[]>(
    `/plenos/sessions?${params.toString()}`,
    "",
    "No se pudieron cargar los plenos.",
    { signal },
  );
}
