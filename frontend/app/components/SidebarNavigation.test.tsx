// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { User } from "./types";
import { SidebarNavigation } from "./SidebarNavigation";

const mocks = vi.hoisted(() => ({
  deleteSidebarShortcuts: vi.fn(),
  pathname: "/",
  putSidebarShortcuts: vi.fn(),
  searchParams: new URLSearchParams(),
  setUser: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
  useSearchParams: () => mocks.searchParams,
}));

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>(
    "../lib/api",
  );
  return {
    ...actual,
    deleteSidebarShortcuts: mocks.deleteSidebarShortcuts,
    putSidebarShortcuts: mocks.putSidebarShortcuts,
  };
});

vi.mock("../lib/session", () => ({
  useSession: () => ({
    handleRequestError: (
      error: unknown,
      setMessage: (message: string) => void,
      fallback: string,
    ) => setMessage(error instanceof Error ? error.message : fallback),
    setUser: mocks.setUser,
  }),
}));

const USER_BASE: User = {
  id: 7,
  email: "alcaldesa@example.com",
  full_name: "Alcaldesa de Villaejemplo",
  is_active: true,
  is_superuser: true,
  permissions: [
    "assistant.use",
    "assistant.memory.review",
    "municipalities.view",
    "municipalities.manage",
    "ordinances.view",
    "requirements.view",
    "projects.view_all",
    "assets.view",
    "maintenance.view",
    "map.view",
    "users.manage",
    "groups.manage",
    "organizations.manage",
    "roles.manage",
  ],
  sidebar_shortcut_ids: null,
};

function renderSidebar(user: User = USER_BASE, isCollapsed = false) {
  return render(
    <SidebarNavigation
      isCollapsed={isCollapsed}
      isMenuOpen={true}
      navigationRef={createRef<HTMLElement>()}
      onNavigate={vi.fn()}
      requirementsTotal={4}
      user={user}
    />,
  );
}

beforeEach(() => {
  mocks.pathname = "/";
  mocks.searchParams = new URLSearchParams();
  mocks.setUser.mockReset();
  mocks.putSidebarShortcuts.mockReset();
  mocks.deleteSidebarShortcuts.mockReset();
  mocks.putSidebarShortcuts.mockResolvedValue({ shortcut_ids: [] });
  mocks.deleteSidebarShortcuts.mockResolvedValue({ shortcut_ids: null });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SidebarNavigation", () => {
  it("renders the prominent fixed core, dynamic defaults and fixed account link", () => {
    renderSidebar();
    const navigation = screen.getByRole("navigation", {
      name: "Navegación principal",
    });
    const linkLabels = within(navigation)
      .getAllByRole("link")
      .map((link) => link.textContent?.replace("BETA", "").trim());

    expect(linkLabels.slice(0, 4)).toEqual([
      "Anacleto",
      "Inicio",
      "Ayuntamiento",
      "Mapa municipal",
    ]);
    expect(linkLabels).toContain("Ordenanzas");
    expect(linkLabels).toContain("Necesidades4");
    expect(linkLabels.at(-1)).toBe("Mi cuenta");
    expect(
      screen.getByRole("link", { name: /Anacleto/i }).className,
    ).toContain("app-nav-link--primary");
  });

  it("distinguishes an explicit empty list from dynamic defaults", () => {
    renderSidebar({ ...USER_BASE, sidebar_shortcut_ids: [] });

    expect(screen.queryByRole("link", { name: /Necesidades/ })).toBeNull();
    expect(
      screen.getByText("Añade aquí las secciones que usas a diario."),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Todas las secciones" }),
    ).toBeTruthy();
  });

  it("marks only the most specific rendered query shortcut as current", () => {
    mocks.pathname = "/ayuntamiento";
    mocks.searchParams = new URLSearchParams("tab=administration");
    renderSidebar({
      ...USER_BASE,
      sidebar_shortcut_ids: ["municipal_facilities"],
    });

    expect(
      screen.getByRole("link", { name: "Instalaciones" }).getAttribute(
        "aria-current",
      ),
    ).toBe("page");
    expect(
      screen.getByRole("link", { name: "Ayuntamiento" }).getAttribute(
        "aria-current",
      ),
    ).toBeNull();
  });

  it("opens the full catalog and exposes fixed and pinned states", () => {
    renderSidebar();
    fireEvent.click(
      screen.getByRole("button", { name: "Todas las secciones" }),
    );

    expect(
      screen.getByRole("dialog", { name: "Todas las secciones" }),
    ).toBeTruthy();
    expect(screen.getAllByText("Fijado").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/En Mis accesos/).length).toBeGreaterThan(0);
  });

  it("reorders with buttons and saves the full explicit list", async () => {
    mocks.putSidebarShortcuts.mockResolvedValue({
      shortcut_ids: ["projects", "requirements"],
    });
    renderSidebar({
      ...USER_BASE,
      sidebar_shortcut_ids: ["requirements", "projects"],
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Todas las secciones" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Personalizar" }));
    fireEvent.click(screen.getByRole("button", { name: "Bajar Necesidades" }));

    const rows = screen.getAllByRole("listitem");
    expect(rows[0].textContent).toContain("Proyectos");
    expect(rows[1].textContent).toContain("Necesidades");

    fireEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() =>
      expect(mocks.putSidebarShortcuts).toHaveBeenCalledWith([
        "projects",
        "requirements",
      ]),
    );
    expect(mocks.setUser).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps the draft open when persistence fails", async () => {
    mocks.putSidebarShortcuts.mockRejectedValue(new Error("Sin conexión"));
    renderSidebar({
      ...USER_BASE,
      sidebar_shortcut_ids: ["requirements"],
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Todas las secciones" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Personalizar" }));
    fireEvent.click(screen.getByRole("button", { name: "Quitar Necesidades" }));
    fireEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));

    expect(await screen.findByText("Sin conexión")).toBeTruthy();
    expect(screen.getByRole("dialog", { name: "Mis accesos" })).toBeTruthy();
    expect(mocks.setUser).not.toHaveBeenCalled();
  });

  it("uses DELETE for reset and returns focus after Escape", async () => {
    renderSidebar({
      ...USER_BASE,
      sidebar_shortcut_ids: ["municipal_roadmap"],
    });
    const trigger = screen.getByRole("button", {
      name: "Todas las secciones",
    });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "Personalizar" }));
    fireEvent.click(screen.getByRole("button", { name: "Restablecer" }));
    fireEvent.click(screen.getByRole("button", { name: "Guardar cambios" }));

    await waitFor(() =>
      expect(mocks.deleteSidebarShortcuts).toHaveBeenCalledTimes(1),
    );

    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it("provides collapsed labels through native titles", () => {
    renderSidebar(USER_BASE, true);
    expect(screen.getByRole("link", { name: /Anacleto/i }).title).toBe(
      "Anacleto",
    );
    expect(
      screen.getByRole("button", { name: "Todas las secciones" }).title,
    ).toBe("Todas las secciones");
  });
});
