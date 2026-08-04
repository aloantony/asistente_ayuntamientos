// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Organization, StaffPost, StaffWorker } from "../types";
import { Personal } from "./Personal";
import { EMPTY_RESOURCE_ERRORS } from "./shared";

const fetchStaffWorkerDossier = vi.fn();

vi.mock("../../lib/municipalWorkspace", () => ({
  fetchStaffWorkerDossier: (...args: unknown[]) =>
    fetchStaffWorkerDossier(...args),
}));

afterEach(cleanup);
beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState({}, "", "/ayuntamiento?tab=people");
  fetchStaffWorkerDossier.mockReset();
  fetchStaffWorkerDossier.mockResolvedValue({
    absences: [],
    reports: [],
    invoices: [],
  });
});

function post(overrides: Partial<StaffPost> = {}): StaffPost {
  return {
    id: 1,
    organization_id: 1,
    parent_id: null,
    kind: "post",
    label: "Secretario",
    description: null,
    sort_order: 0,
    created_at: "2026-08-03T10:00:00Z",
    updated_at: "2026-08-03T10:00:00Z",
    ...overrides,
  };
}

function worker(overrides: Partial<StaffWorker> = {}): StaffWorker {
  return {
    id: 1,
    organization_id: 1,
    post_id: null,
    full_name: "Marina Alonso Ruiz",
    email: null,
    phone: null,
    description: null,
    status: "active",
    schedule_summary: null,
    schedule_days: [],
    weekly_hours: null,
    contract_type: null,
    contract_start_date: null,
    contract_end_date: null,
    vacation_days_limit: null,
    personal_days_limit: null,
    bills_invoices: false,
    created_by_id: null,
    updated_by_id: null,
    created_at: "2026-08-03T10:00:00Z",
    updated_at: "2026-08-03T10:00:00Z",
    post: null,
    ...overrides,
  };
}

const organization = {
  id: 1,
  name: "Ayuntamiento de prueba",
  status: "active",
  description: null,
  users: [],
} as unknown as Organization;

function renderPersonal({
  workers = [] as StaffWorker[],
  posts = [] as StaffPost[],
  canViewStaff = true,
  errors = EMPTY_RESOURCE_ERRORS,
} = {}) {
  return render(
    <Personal
      canViewStaff={canViewStaff}
      errors={errors}
      onRetry={vi.fn()}
      organization={organization}
      posts={{ items: posts, total: posts.length }}
      workers={{ items: workers, total: workers.length }}
    />,
  );
}

describe("Personal", () => {
  it("abre la ficha de la primera persona y pinta su semana", async () => {
    renderPersonal({
      workers: [
        worker({
          schedule_days: ["tuesday", "thursday"],
          schedule_summary: "Martes y jueves de 9 a 14",
        }),
      ],
    });

    await waitFor(() =>
      expect(fetchStaffWorkerDossier).toHaveBeenCalledWith(1, {
        includeInvoices: false,
        signal: expect.anything(),
      }),
    );
    expect(screen.getByText("Martes y jueves de 9 a 14")).toBeTruthy();
    expect(
      screen.getByLabelText("martes: atiende").textContent,
    ).toBe("M");
    expect(screen.getByLabelText("lunes: no atiende").textContent).toBe("L");
  });

  it("selecciona el puesto pedido por el menú superior con ?puesto=", async () => {
    window.history.replaceState(
      {},
      "",
      "/ayuntamiento?tab=people&puesto=alguacil",
    );
    const alguacil = post({ id: 2, label: "Alguacil" });

    renderPersonal({
      posts: [post(), alguacil],
      workers: [
        worker({ id: 1, full_name: "Ana Secretaria", post_id: 1, post: post() }),
        worker({
          id: 2,
          full_name: "Julio Alguacil",
          post_id: 2,
          post: alguacil,
        }),
      ],
    });

    await waitFor(() =>
      expect(fetchStaffWorkerDossier).toHaveBeenCalledWith(2, expect.anything()),
    );
    expect(
      screen.getByRole("heading", { level: 3, name: "Julio Alguacil" }),
    ).toBeTruthy();
  });

  it("pide las facturas sólo del personal que factura", async () => {
    renderPersonal({ workers: [worker({ bills_invoices: true })] });

    await waitFor(() =>
      expect(fetchStaffWorkerDossier).toHaveBeenCalledWith(1, {
        includeInvoices: true,
        signal: expect.anything(),
      }),
    );
  });

  it("cambia de ficha al elegir otra persona", async () => {
    renderPersonal({
      workers: [
        worker({ id: 1, full_name: "Ana Secretaria" }),
        worker({ id: 2, full_name: "Julio Vega" }),
      ],
    });

    await waitFor(() => expect(fetchStaffWorkerDossier).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /Julio Vega/ }));

    await waitFor(() =>
      expect(fetchStaffWorkerDossier).toHaveBeenCalledWith(2, expect.anything()),
    );
  });

  it("marca los puestos sin ocupar como vacantes", () => {
    renderPersonal({
      posts: [post({ id: 1, label: "Secretario" }), post({ id: 2, label: "Arquitecto" })],
      workers: [worker({ id: 1, post_id: 1, post: post() })],
    });

    expect(screen.getByText("Puestos vacantes")).toBeTruthy();
    expect(screen.getByText("Arquitecto")).toBeTruthy();
  });

  it("declara la falta de permiso en lugar de una plantilla vacía", () => {
    renderPersonal({ canViewStaff: false });

    expect(screen.getByText("Plantilla no autorizada")).toBeTruthy();
    expect(screen.queryByText("Plantilla sin registrar")).toBeNull();
    expect(fetchStaffWorkerDossier).not.toHaveBeenCalled();
  });

  it("separa el directorio de acceso de la plantilla", () => {
    renderPersonal({ workers: [worker()] });

    expect(
      screen.getByRole("button", { name: /Directorio de acceso/ }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Plantilla municipal/ }),
    ).toBeTruthy();
  });
});
