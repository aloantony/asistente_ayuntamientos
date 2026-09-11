// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MunicipalTask, MunicipalTaskSummary, User } from "./types";

const fetchTaskSummary = vi.fn();
const fetchTasks = vi.fn();
const fetchRoadmapGovernment = vi.fn();
const useSession = vi.fn();

vi.mock("../lib/session", () => ({
  useSession: () => useSession(),
  shouldShowProjectsPanel: () => true,
  shouldShowRequirementsPanel: () => true,
}));

vi.mock("../lib/roadmap", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/roadmap")>("../lib/roadmap");
  return {
    ...actual,
    fetchTaskSummary: (...args: unknown[]) => fetchTaskSummary(...args),
    fetchTasks: (...args: unknown[]) => fetchTasks(...args),
    fetchRoadmapGovernment: (...args: unknown[]) =>
      fetchRoadmapGovernment(...args),
  };
});

const { RoadmapPanel, isOverdue } = await import("./RoadmapPanel");

const REFERENCE_DATE = "2026-08-04";

function summary(overrides: Partial<MunicipalTaskSummary> = {}) {
  return {
    total: 3,
    pending: 1,
    in_progress: 1,
    blocked: 1,
    completed: 0,
    cancelled: 0,
    overdue: 1,
    unassigned: 1,
    reference_date: REFERENCE_DATE,
    ...overrides,
  } satisfies MunicipalTaskSummary;
}

function task(overrides: Partial<MunicipalTask> = {}): MunicipalTask {
  return {
    id: 1,
    organization_id: 1,
    title: "Reparar el alumbrado de la calle Mayor",
    description: null,
    status: "pending",
    priority: "normal",
    due_date: null,
    blocked_reason: null,
    completed_at: null,
    assignee_worker_id: null,
    project_id: null,
    created_by_id: null,
    updated_by_id: null,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:00:00Z",
    assignee: null,
    project: null,
    ...overrides,
  };
}

const user = {
  id: 1,
  email: "tecnico@example.com",
  full_name: "Técnica municipal",
  is_active: true,
  is_superuser: false,
  permissions: ["tasks.view", "government.view"],
  organizations: [
    {
      id: 1,
      name: "Ayuntamiento de prueba",
      status: "active",
      municipality: {
        id: 1,
        name: "Fuentelcésped",
        province: "Burgos",
        autonomous_community: "Castilla y León",
      },
    },
  ],
} as unknown as User;

afterEach(cleanup);
beforeEach(() => {
  window.localStorage.clear();
  fetchTaskSummary.mockReset().mockResolvedValue(summary());
  fetchTasks.mockReset().mockResolvedValue({ items: [task()], total: 1 });
  fetchRoadmapGovernment.mockReset().mockResolvedValue({ items: [], total: 0 });
  useSession.mockReset().mockReturnValue({
    user,
    handleRequestError: vi.fn(),
  });
});

describe("isOverdue", () => {
  it("marca vencida solo una tarea abierta con la fecha pasada", () => {
    expect(
      isOverdue(task({ due_date: "2026-08-03" }), REFERENCE_DATE),
    ).toBe(true);
    expect(isOverdue(task({ due_date: REFERENCE_DATE }), REFERENCE_DATE)).toBe(
      false,
    );
    expect(
      isOverdue(task({ due_date: "2026-09-01" }), REFERENCE_DATE),
    ).toBe(false);
    expect(isOverdue(task({ due_date: null }), REFERENCE_DATE)).toBe(false);
  });

  it("no llama vencida a una tarea ya cerrada aunque su fecha pasara", () => {
    expect(
      isOverdue(
        task({ due_date: "2026-01-01", status: "completed" }),
        REFERENCE_DATE,
      ),
    ).toBe(false);
    expect(
      isOverdue(
        task({ due_date: "2026-01-01", status: "cancelled" }),
        REFERENCE_DATE,
      ),
    ).toBe(false);
  });

  it("no decide nada sin la fecha de referencia del servidor", () => {
    expect(isOverdue(task({ due_date: "2020-01-01" }), null)).toBe(false);
  });
});

describe("RoadmapPanel", () => {
  it("muestra los conteos reales en los chips de filtro", async () => {
    render(<RoadmapPanel />);

    await waitFor(() => expect(fetchTaskSummary).toHaveBeenCalled());
    const blocked = screen.getByRole("button", { name: /Bloqueadas/ });
    const overdue = screen.getByRole("button", { name: /Vencidas/ });

    expect(blocked.textContent).toContain("1");
    expect(overdue.textContent).toContain("1");
  });

  it("avisa cuando hay bloqueadas o vencidas esperando decisión", async () => {
    render(<RoadmapPanel />);

    await waitFor(() =>
      expect(screen.getByText("Atención requerida")).toBeTruthy(),
    );
  });

  it("calla la banda de atención cuando no hay nada que decidir", async () => {
    fetchTaskSummary.mockResolvedValue(summary({ blocked: 0, overdue: 0 }));

    render(<RoadmapPanel />);

    await waitFor(() => expect(fetchTaskSummary).toHaveBeenCalled());
    expect(screen.queryByText("Atención requerida")).toBeNull();
  });

  it("vuelve a pedir la lista con el filtro elegido", async () => {
    render(<RoadmapPanel />);
    await waitFor(() => expect(fetchTasks).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: /Vencidas/ }));

    await waitFor(() => expect(fetchTasks).toHaveBeenCalledTimes(2));
    expect(fetchTasks).toHaveBeenLastCalledWith(
      1,
      { overdue: true },
      expect.anything(),
      0,
    );
  });

  it("marca en la tarjeta la tarea vencida", async () => {
    fetchTasks.mockResolvedValue({
      items: [task({ due_date: "2026-07-01" })],
      total: 1,
    });

    render(<RoadmapPanel />);

    await waitFor(() => expect(screen.getByText("Vencida")).toBeTruthy());
  });

  it("declara la falta de permiso en lugar de una lista vacía", async () => {
    useSession.mockReturnValue({
      user: { ...user, permissions: ["government.view"] },
      handleRequestError: vi.fn(),
    });

    render(<RoadmapPanel />);

    expect(screen.getByText("Hoja de ruta no autorizada")).toBeTruthy();
    expect(fetchTasks).not.toHaveBeenCalled();
  });

  it("agrupa por proyecto en su pestaña", async () => {
    fetchTasks.mockResolvedValue({
      items: [
        task({ id: 1, project: { id: 7, name: "Alumbrado" } }),
        task({ id: 2 }),
      ],
      total: 2,
    });

    render(<RoadmapPanel />);
    await waitFor(() => expect(fetchTasks).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("tab", { name: "Proyectos" }));

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Alumbrado" })).toBeTruthy(),
    );
    expect(screen.getByRole("heading", { name: "Sin proyecto" })).toBeTruthy();
  });
});

it("pagina y vuelve al principio al cambiar el filtro", async () => {
  fetchTasks.mockResolvedValue({ items: [task()], total: 101 });
  render(<RoadmapPanel />);
  await waitFor(() => expect(fetchTasks).toHaveBeenCalledWith(1, { includeClosed: true }, expect.anything(), 0));
  await waitFor(() => expect((screen.getByRole("button", { name: "Siguiente" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "Siguiente" }));
  await waitFor(() => expect(fetchTasks).toHaveBeenLastCalledWith(1, { includeClosed: true }, expect.anything(), 50));
  fireEvent.click(screen.getByRole("button", { name: /Vencidas/ }));
  await waitFor(() => expect(fetchTasks).toHaveBeenLastCalledWith(1, { overdue: true }, expect.anything(), 0));
});
