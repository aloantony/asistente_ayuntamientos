// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  activeTopNavSectionFor,
  shouldShowTopNav,
  visibleTopNavSections,
  TOP_NAV_SECTIONS,
} from "../lib/topNav";
import { TopBar } from "./TopBar";

afterEach(cleanup);

describe("modelo de navegación municipal", () => {
  it("sólo expone secciones y entradas ya construidas", () => {
    const sections = visibleTopNavSections();

    expect(sections.every((section) => section.enabled)).toBe(true);
    expect(
      sections.every((section) => section.items.every((item) => item.enabled)),
    ).toBe(true);
    // El modelo completo sí conserva lo pendiente, como índice de lo que falta.
    expect(TOP_NAV_SECTIONS.length).toBeGreaterThan(sections.length);
  });

  it("muestra la barra sólo en las rutas institucionales", () => {
    expect(shouldShowTopNav("/ayuntamiento")).toBe(true);
    expect(shouldShowTopNav("/sede")).toBe(true);
    expect(shouldShowTopNav("/hoja-de-ruta")).toBe(true);
    expect(shouldShowTopNav("/")).toBe(false);
    expect(shouldShowTopNav("/asistente")).toBe(false);
    expect(shouldShowTopNav("/proyectos")).toBe(false);
  });

  it("resuelve la sección activa a partir de la ruta", () => {
    expect(activeTopNavSectionFor("/ayuntamiento")).toBe("ayuntamiento");
    expect(activeTopNavSectionFor("/sede")).toBe("sede");
    expect(activeTopNavSectionFor("/proyectos")).toBeNull();
  });
});

describe("TopBar", () => {
  it("muestra el nombre del municipio y las secciones activas", () => {
    render(<TopBar municipalityName="Fuentelcésped" />);

    expect(screen.getByText("Fuentelcésped")).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Navegación municipal" })).toBeTruthy();
    expect(screen.getByText("AYUNTAMIENTO")).toBeTruthy();
    // Sede electrónica todavía no tiene pantalla: no debe aparecer.
    expect(screen.queryByText("SEDE ELECTRÓNICA")).toBeNull();
  });

  it("abre y cierra el desplegable de una sección con el teclado", () => {
    render(<TopBar municipalityName="Fuentelcésped" />);

    const trigger = screen.getByRole("button", { name: /AYUNTAMIENTO/ });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Información del municipio")).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Información del municipio")).toBeTruthy();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Información del municipio")).toBeNull();
  });

  it("marca la sección activa", () => {
    render(
      <TopBar activeSectionId="hoja-de-ruta" municipalityName="Fuentelcésped" />,
    );

    // "Hoja de ruta" no tiene submenú: se renderiza como enlace marcado.
    const link = screen.getByRole("link", { name: "HOJA DE RUTA" });
    expect(link.getAttribute("aria-current")).toBe("page");
  });
});
