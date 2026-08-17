// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ArchiveItem, HeritageAsset } from "../types";
import { Patrimonio, describePeriod } from "./Patrimonio";

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

function asset(overrides: Partial<HeritageAsset> = {}): HeritageAsset {
  return {
    id: 1,
    organization_id: 1,
    slug: "ermita-san-roque",
    name: "Ermita de San Roque",
    kind: "building",
    period: "siglo XVI",
    description: null,
    protection_level: "none",
    protection_reference: null,
    conservation_state: "fair",
    last_survey_date: null,
    location_id: null,
    notes: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

function archiveItem(overrides: Partial<ArchiveItem> = {}): ArchiveItem {
  return {
    id: 1,
    organization_id: 1,
    reference: "AR-001",
    title: "Libro de actas",
    kind: "book",
    description: null,
    start_year: null,
    end_year: null,
    physical_location: null,
    conservation_state: "unknown",
    digitisation_state: "not_digitised",
    document_id: null,
    heritage_asset_id: null,
    notes: null,
    created_at: "2026-08-05T10:00:00Z",
    updated_at: "2026-08-05T10:00:00Z",
    ...overrides,
  };
}

describe("describePeriod", () => {
  it("dice «sin fechar» en lugar de fingir un intervalo", () => {
    expect(describePeriod(archiveItem())).toBe("Sin fechar");
  });

  it("colapsa un año único y muestra el tramo cuando lo hay", () => {
    expect(describePeriod(archiveItem({ start_year: 1712, end_year: 1712 }))).toBe(
      "1712",
    );
    expect(describePeriod(archiveItem({ start_year: 1712, end_year: 1750 }))).toBe(
      "1712–1750",
    );
  });

  it("se apaña con un solo extremo conocido", () => {
    expect(describePeriod(archiveItem({ start_year: 1900 }))).toBe("1900");
    expect(describePeriod(archiveItem({ end_year: 1950 }))).toBe("1950");
  });
});

describe("Patrimonio", () => {
  it("declara la falta de permiso en lugar de un bloque vacío", () => {
    render(<Patrimonio archive={[]} assets={[asset()]} canView={false} />);

    expect(screen.getByText("Patrimonio no autorizado")).toBeTruthy();
    expect(screen.queryByText("Ermita de San Roque")).toBeNull();
  });

  it("distingue lo vacío de lo restringido", () => {
    render(<Patrimonio archive={[]} assets={[]} canView />);

    expect(screen.getByText("Sin catalogar")).toBeTruthy();
  });

  it("no llama declarado a lo que no lo está", () => {
    render(<Patrimonio archive={[]} assets={[asset()]} canView />);

    // Mucho patrimonio de un pueblo es valioso sin estar declarado.
    expect(screen.getByText("Sin declarar")).toBeTruthy();
    expect(screen.getByText(/siglo XVI/)).toBeTruthy();
  });

  it("enseña la figura de protección cuando existe", () => {
    render(
      <Patrimonio
        archive={[]}
        assets={[
          asset({ protection_level: "bic", protection_reference: "BOCyL 12/2004" }),
        ]}
        canView
      />,
    );

    expect(screen.getByText("Bien de Interés Cultural")).toBeTruthy();
  });

  it("muestra dónde está el papel", () => {
    render(
      <Patrimonio
        archive={[archiveItem({ physical_location: "Armario 2, balda 3" })]}
        assets={[]}
        canView
      />,
    );

    expect(screen.getByText(/Armario 2, balda 3/)).toBeTruthy();
  });

  it("omite el bloque que no tiene nada que enseñar", () => {
    render(<Patrimonio archive={[archiveItem()]} assets={[]} canView />);

    expect(screen.getByText("Archivo municipal")).toBeTruthy();
    expect(screen.queryByText("Bienes patrimoniales")).toBeNull();
  });
});
