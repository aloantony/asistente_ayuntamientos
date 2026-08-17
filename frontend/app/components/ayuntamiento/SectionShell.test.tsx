// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SectionShell } from "./SectionShell";

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

describe("SectionShell", () => {
  it("despliega y pliega el contenido, y lo anuncia en el encabezado", () => {
    render(
      <SectionShell sectionKey="prueba" title="Estructura de Gobierno">
        <p>Contenido de la sección</p>
      </SectionShell>,
    );

    const header = screen.getByRole("button", { name: /Estructura de Gobierno/ });
    expect(header.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Contenido de la sección")).toBeTruthy();

    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Contenido de la sección")).toBeNull();
  });

  it("recuerda el estado plegado entre montajes", () => {
    const { unmount } = render(
      <SectionShell sectionKey="patrimonio" title="Patrimonio">
        <p>Bienes municipales</p>
      </SectionShell>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Patrimonio/ }));
    unmount();

    render(
      <SectionShell sectionKey="patrimonio" title="Patrimonio">
        <p>Bienes municipales</p>
      </SectionShell>,
    );

    expect(
      screen.getByRole("button", { name: /Patrimonio/ }).getAttribute("aria-expanded"),
    ).toBe("false");
  });

  it("no mezcla la preferencia de secciones distintas", () => {
    render(
      <>
        <SectionShell sectionKey="normativa" title="Normativa">
          <p>Ordenanzas</p>
        </SectionShell>
        <SectionShell sectionKey="personal" title="Personal">
          <p>Plantilla</p>
        </SectionShell>
      </>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Normativa/ }));

    expect(screen.queryByText("Ordenanzas")).toBeNull();
    expect(screen.getByText("Plantilla")).toBeTruthy();
  });

  it("muestra el conteo real cuando se le pasa uno", () => {
    render(
      <SectionShell count={12} sectionKey="licencias" title="Licencias">
        <p>Listado</p>
      </SectionShell>,
    );

    expect(screen.getByText("12")).toBeTruthy();
  });
});
