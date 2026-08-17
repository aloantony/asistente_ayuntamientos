"use client";

import { Coins, Landmark, ShieldCheck, Wallet } from "lucide-react";
import { SectionShell } from "./SectionShell";
import { ResourceState, formatDate } from "./shared";
import styles from "./Administracion.module.css";
import type {
  BudgetExecution,
  MunicipalBudget,
  TreasuryMovement,
} from "../types";

const BUDGET_STATUS_LABELS: Record<MunicipalBudget["status"], string> = {
  draft: "Borrador",
  approved: "Aprobado",
  executing: "En ejecución",
  settled: "Liquidado",
};

/** Los importes viajan como cadena para no perder precisión en JSON. */
export function formatEuros(value: string | null | undefined) {
  if (value === null || value === undefined) {
    return "—";
  }
  const amount = Number.parseFloat(value);
  if (!Number.isFinite(amount)) {
    return "—";
  }
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(amount);
}

/**
 * Porcentaje ejecutado sobre el crédito total. Devuelve null cuando no hay
 * crédito: dividir entre cero daría un infinito que la barra pintaría lleno,
 * como si todo estuviera gastado.
 */
export function executionRatio(execution: BudgetExecution | null) {
  if (execution === null) {
    return null;
  }
  const total =
    Number.parseFloat(execution.total_expense) +
    Number.parseFloat(execution.approved_amendments);
  const executed = Number.parseFloat(execution.executed_expense);
  if (!Number.isFinite(total) || !Number.isFinite(executed) || total <= 0) {
    return null;
  }
  return Math.min(Math.round((executed / total) * 100), 100);
}

export function Presupuestos({
  budgets,
  execution,
  movements,
  canView,
}: {
  budgets: MunicipalBudget[];
  execution: BudgetExecution | null;
  movements: TreasuryMovement[];
  canView: boolean;
}) {
  const ratio = executionRatio(execution);

  return (
    <SectionShell
      count={canView ? budgets.length : null}
      icon={Coins}
      sectionKey="presupuestos"
      title="Presupuesto y tesorería"
    >
      {!canView ? (
        <ResourceState
          description="Tu cuenta no dispone del permiso budgets.view en esta organización."
          icon={ShieldCheck}
          title="Presupuesto no autorizado"
          tone="restricted"
        />
      ) : budgets.length === 0 ? (
        <ResourceState
          description="Todavía no se ha registrado ningún presupuesto anual."
          icon={Coins}
          title="Sin presupuesto registrado"
        />
      ) : (
        <div className={styles.blocks}>
          <section className={styles.block}>
            <h3>
              <Landmark aria-hidden="true" size={15} strokeWidth={1.7} />
              Ejercicios
            </h3>
            <ul className={styles.rows}>
              {budgets.map((budget) => (
                <li key={budget.id}>
                  <div>
                    <strong>Presupuesto {budget.reference_year}</strong>
                    <small>
                      {budget.approved_on
                        ? `Aprobado el ${formatDate(budget.approved_on)}`
                        : "Sin aprobar"}
                    </small>
                  </div>
                  <span className={styles.badge} data-tone={budget.status}>
                    {BUDGET_STATUS_LABELS[budget.status]}
                  </span>
                </li>
              ))}
            </ul>
          </section>

          {execution !== null ? (
            <section className={styles.block}>
              <h3>
                <Coins aria-hidden="true" size={15} strokeWidth={1.7} />
                Ejecución {execution.reference_year}
              </h3>
              <dl className={styles.facts}>
                <div>
                  <dt>Ingresos</dt>
                  <dd>{formatEuros(execution.total_income)}</dd>
                </div>
                <div>
                  <dt>Gasto presupuestado</dt>
                  <dd>{formatEuros(execution.total_expense)}</dd>
                </div>
                <div>
                  <dt>Modificaciones aprobadas</dt>
                  <dd>{formatEuros(execution.approved_amendments)}</dd>
                </div>
                <div>
                  <dt>Gasto ejecutado</dt>
                  <dd>{formatEuros(execution.executed_expense)}</dd>
                </div>
                <div>
                  <dt>Crédito disponible</dt>
                  <dd>{formatEuros(execution.available_credit)}</dd>
                </div>
              </dl>
              {ratio !== null ? (
                <div
                  aria-label={`Ejecutado el ${ratio}% del crédito`}
                  aria-valuemax={100}
                  aria-valuemin={0}
                  aria-valuenow={ratio}
                  className={styles.meter}
                  role="progressbar"
                >
                  <span style={{ width: `${ratio}%` }} />
                </div>
              ) : null}
            </section>
          ) : null}

          {movements.length > 0 ? (
            <section className={styles.block}>
              <h3>
                <Wallet aria-hidden="true" size={15} strokeWidth={1.7} />
                Tesorería
              </h3>
              <ul className={styles.rows}>
                {movements.map((movement) => (
                  <li key={movement.id}>
                    <div>
                      <strong>{movement.concept}</strong>
                      <small>{movement.account_label ?? "Sin cuenta"}</small>
                    </div>
                    <span
                      className={styles.badge}
                      data-tone={movement.direction === "inflow" ? "granted" : "denied"}
                    >
                      {movement.direction === "inflow" ? "Entrada" : "Salida"}
                    </span>
                    <span className={styles.date}>
                      {formatEuros(movement.amount)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      )}
    </SectionShell>
  );
}
