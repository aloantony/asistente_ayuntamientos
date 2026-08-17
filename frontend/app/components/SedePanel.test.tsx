// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SedeContent, User } from "./types";

const fetchSedeContent = vi.fn();
const useSession = vi.fn();

vi.mock("../lib/session", () => ({
  useSession: () => useSession(),
}));

vi.mock("../lib/sede", async () => {
  const actual = await vi.importActual<typeof import("../lib/sede")>("../lib/sede");
  return {
    ...actual,
    fetchSedeContent: (...args: unknown[]) => fetchSedeContent(...args),
  };
});

const { SedePanel } = await import("./SedePanel");

function content(overrides: Partial<SedeContent> = {}): SedeContent {
  return {
    organization_id: 1,
    board: [],
    procedures: [],
    taxes: [],
    contracts: [],
    transparency: [],
    sessions: [],
    ordinances: [],
    ...overrides,
  };
}

const user = {
  id: 1,
  email: "vecina@example.com",
  full_name: "Vecina",
  is_active: true,
  is_superuser: false,
  permissions: ["sede.view"],
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
  window.history.replaceState({}, "", "/sede");
  fetchSedeContent.mockReset().mockResolvedValue(content());
  useSession.mockReset().mockReturnValue({ user, handleRequestError: vi.fn() });
});

describe("SedePanel", () => {
  it("abre el tablón por defecto y cuenta lo publicado", async () => {
    fetchSedeContent.mockResolvedValue(
      content({
        board: [
          {
            kind: "bando",
            id: 1,
            title: "Corte de agua",
            summary: null,
            published_on: "2026-08-05",
            expires_on: null,
          },
        ],
      }),
    );

    render(<SedePanel />);

    await waitFor(() => expect(screen.getByText("Corte de agua")).toBeTruthy());
    expect(
      screen.getByRole("tab", { name: /Tablón de anuncios \(1\)/ }),
    ).toBeTruthy();
  });

  it("respeta la sección pedida por el menú superior", async () => {
    window.history.replaceState({}, "", "/sede?seccion=tributos");
    fetchSedeContent.mockResolvedValue(
      content({
        taxes: [
          {
            id: 1,
            slug: "ibi",
            name: "Impuesto sobre bienes inmuebles",
            kind: "tax",
            rate_kind: "percentage",
            rate_value: "0.4000",
            rate_description: null,
            taxable_base: null,
            ordinance_id: 7,
          },
        ],
      }),
    );

    render(<SedePanel />);

    await waitFor(() =>
      expect(screen.getByText("Impuesto sobre bienes inmuebles")).toBeTruthy(),
    );
  });

  it("ignora una sección desconocida y abre el tablón", async () => {
    window.history.replaceState({}, "", "/sede?seccion=inventada");

    render(<SedePanel />);

    await waitFor(() => expect(fetchSedeContent).toHaveBeenCalled());
    expect(
      screen.getByRole("tab", { name: /Tablón/ }).getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("distingue una sección vacía de un fallo de carga", async () => {
    render(<SedePanel />);

    await waitFor(() =>
      expect(screen.getByText(/No hay anuncios publicados/)).toBeTruthy(),
    );
  });

  it("permite cambiar de sección sin volver a pedir la sede", async () => {
    fetchSedeContent.mockResolvedValue(
      content({
        procedures: [
          {
            id: 1,
            slug: "empadronamiento",
            name: "Alta en el padrón",
            description: null,
            channel: "in_person",
            deadline_days: 15,
            fee_description: null,
          },
        ],
      }),
    );

    render(<SedePanel />);
    await waitFor(() => expect(fetchSedeContent).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("tab", { name: /Trámites/ }));

    expect(screen.getByText("Alta en el padrón")).toBeTruthy();
    // La sede llega entera en una respuesta: cambiar de pestaña no vuelve a
    // pedirla.
    expect(fetchSedeContent).toHaveBeenCalledTimes(1);
  });

  it("ofrece reintentar cuando la carga falla", async () => {
    useSession.mockReturnValue({
      user,
      handleRequestError: (
        _reason: unknown,
        setter: (message: string) => void,
        fallback: string,
      ) => setter(fallback),
    });
    fetchSedeContent.mockRejectedValue(new Error("boom"));

    render(<SedePanel />);

    await waitFor(() => expect(screen.getByText("Sede no disponible")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeTruthy();
  });
});
