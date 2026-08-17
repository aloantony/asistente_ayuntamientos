// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type {
  BudgetExecution,
  CouncilSession,
  MunicipalBudget,
  TreasuryMovement,
} from "../types";
import { Plenos, describeVote } from "./Plenos";
import { Presupuestos, executionRatio, formatEuros } from "./Presupuestos";

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

function budget(overrides: Partial<MunicipalBudget> = {}): MunicipalBudget {
  return {
    id: 1,
    organization_id: 1,
    reference_year: 2026,
    status: "approved",
    approved_on: "2025-12-20",
    notes: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function execution(overrides: Partial<BudgetExecution> = {}): BudgetExecution {
  return {
    budget_id: 1,
    reference_year: 2026,
    status: "approved",
    total_income: "50000.00",
    total_expense: "30000.00",
    approved_amendments: "5000.00",
    executed_expense: "12000.00",
    available_credit: "23000.00",
    ...overrides,
  };
}

function session(overrides: Partial<CouncilSession> = {}): CouncilSession {
  return {
    id: 1,
    organization_id: 1,
    kind: "ordinary",
    status: "held",
    held_on: "2026-03-27",
    summary: null,
    minutes_status: "pending",
    minutes_document_id: null,
    publish_to_sede: false,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    agenda_items: [],
    ...overrides,
  };
}

function agendaItem(overrides: Partial<CouncilSession["agenda_items"][number]> = {}) {
  return {
    id: 1,
    session_id: 1,
    organization_id: 1,
    position: 1,
    title: "Punto",
    description: null,
    votes_in_favour: null,
    votes_against: null,
    abstentions: null,
    outcome: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

const NO_MOVEMENTS: TreasuryMovement[] = [];

describe("formatEuros", () => {
  it("no inventa un importe cuando no lo hay", () => {
    expect(formatEuros(null)).toBe("—");
    expect(formatEuros(undefined)).toBe("—");
    expect(formatEuros("no es un numero")).toBe("—");
  });
});

describe("executionRatio", () => {
  it("calcula el porcentaje sobre el crédito total", () => {
    // 12000 sobre 30000 + 5000 = 34%
    expect(executionRatio(execution())).toBe(34);
  });

  it("no devuelve nada cuando no hay crédito", () => {
    // Dividir entre cero pintaría la barra llena, como si todo se hubiera
    // gastado; sin crédito no hay nada que ejecutar.
    expect(
      executionRatio(
        execution({ total_expense: "0.00", approved_amendments: "0.00" }),
      ),
    ).toBeNull();
    expect(executionRatio(null)).toBeNull();
  });

  it("no pasa del 100 aunque se gaste de más", () => {
    expect(
      executionRatio(
        execution({ executed_expense: "999999.00" }),
      ),
    ).toBe(100);
  });
});

describe("describeVote", () => {
  it("distingue un punto no votado de uno con cero votos", () => {
    expect(describeVote(agendaItem())).toBeNull();
    expect(
      describeVote(
        agendaItem({ votes_in_favour: 0, votes_against: 0, abstentions: 0 }),
      ),
    ).toBe("0 a favor · 0 en contra");
  });

  it("añade las abstenciones solo cuando las hay", () => {
    expect(
      describeVote(
        agendaItem({ votes_in_favour: 5, votes_against: 2, abstentions: 1 }),
      ),
    ).toContain("1 abstenciones");
  });
});

describe("Presupuestos", () => {
  it("declara la falta de permiso en lugar de un bloque vacío", () => {
    render(
      <Presupuestos
        budgets={[budget()]}
        canView={false}
        execution={execution()}
        movements={NO_MOVEMENTS}
      />,
    );

    expect(screen.getByText("Presupuesto no autorizado")).toBeTruthy();
  });

  it("distingue lo vacío de lo restringido", () => {
    render(
      <Presupuestos
        budgets={[]}
        canView
        execution={null}
        movements={NO_MOVEMENTS}
      />,
    );

    expect(screen.getByText("Sin presupuesto registrado")).toBeTruthy();
  });

  it("enseña la ejecución derivada del ejercicio", () => {
    render(
      <Presupuestos
        budgets={[budget()]}
        canView
        execution={execution()}
        movements={NO_MOVEMENTS}
      />,
    );

    expect(screen.getByText(/Ejecución 2026/)).toBeTruthy();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe(
      "34",
    );
  });
});

describe("Plenos", () => {
  it("distingue la falta de permiso de la falta de sesiones", () => {
    const { unmount } = render(<Plenos canView={false} sessions={[]} />);
    expect(screen.getByText("Plenos no autorizados")).toBeTruthy();
    unmount();

    render(<Plenos canView sessions={[]} />);
    expect(screen.getByText("Sin sesiones registradas")).toBeTruthy();
  });

  it("avisa cuando una sesión no tiene orden del día", () => {
    render(<Plenos canView sessions={[session()]} />);

    expect(screen.getByText("Sin orden del día registrado.")).toBeTruthy();
    expect(screen.getByText("Acta pendiente")).toBeTruthy();
  });

  it("lista el orden del día con su votación", () => {
    render(
      <Plenos
        canView
        sessions={[
          session({
            agenda_items: [
              agendaItem({ title: "Aprobación del presupuesto", votes_in_favour: 5, votes_against: 2 }),
            ],
          }),
        ]}
      />,
    );

    expect(screen.getByText("Aprobación del presupuesto")).toBeTruthy();
    expect(screen.getByText(/5 a favor/)).toBeTruthy();
  });
});
