// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GovernmentMember } from "../types";
import { EstructuraGobierno } from "./EstructuraGobierno";

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

function member(overrides: Partial<GovernmentMember> = {}): GovernmentMember {
  return {
    id: 1,
    organization_id: 1,
    level: "concejalia",
    full_name: "Marina Alonso Ruiz",
    role_title: "Concejala de Urbanismo",
    political_group: null,
    email: null,
    phone: null,
    biography: null,
    term_start_date: null,
    term_end_date: null,
    sort_order: 0,
    status: "active",
    created_by_id: null,
    updated_by_id: null,
    created_at: "2026-08-03T10:00:00Z",
    updated_at: "2026-08-03T10:00:00Z",
    ...overrides,
  };
}

describe("EstructuraGobierno", () => {
  it("agrupa los cargos por nivel en orden protocolario", () => {
    const members = [
      member({ id: 3, level: "secretaria", full_name: "Secretaria" }),
      member({ id: 1, level: "alcaldia", full_name: "Alcalde" }),
      member({ id: 2, level: "concejalia", full_name: "Concejala" }),
    ];

    render(
      <EstructuraGobierno
        canView
        error=""
        members={{ items: members, total: 3 }}
        onRetry={vi.fn()}
      />,
    );

    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((heading) => heading.textContent);

    expect(headings).toEqual(["Alcaldía", "Concejalía", "Secretaría"]);
  });

  it("declara la falta de permiso en lugar de un listado vacío", () => {
    render(
      <EstructuraGobierno
        canView={false}
        error=""
        members={null}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByText("Corporación no autorizada")).toBeTruthy();
    expect(screen.queryByText("Corporación sin registrar")).toBeNull();
  });

  it("ofrece reintentar cuando la carga ha fallado", () => {
    render(
      <EstructuraGobierno
        canView
        error="No se pudo cargar la corporación municipal."
        members={null}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByText("Corporación no disponible")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeTruthy();
  });

  it("distingue una corporación vacía de un error", () => {
    render(
      <EstructuraGobierno
        canView
        error=""
        members={{ items: [], total: 0 }}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByText("Corporación sin registrar")).toBeTruthy();
  });
});
