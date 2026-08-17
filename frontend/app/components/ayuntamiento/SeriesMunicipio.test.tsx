// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ClimateRecord, HouseholdStat, PadronRecord } from "../types";
import { SeriesMunicipio } from "./SeriesMunicipio";

afterEach(cleanup);
beforeEach(() => window.localStorage.clear());

function padron(year: number, population: number): PadronRecord {
  return {
    id: year,
    organization_id: 1,
    reference_year: year,
    population,
    men: null,
    women: null,
    births: null,
    deaths: null,
    source: "municipal",
    notes: null,
    created_at: "2026-08-04T10:00:00Z",
    updated_at: "2026-08-04T10:00:00Z",
  };
}

function climate(
  year: number,
  month: number | null,
  overrides: Partial<ClimateRecord> = {},
): ClimateRecord {
  return {
    id: year * 100 + (month ?? 0),
    organization_id: 1,
    reference_year: year,
    reference_month: month,
    avg_temperature_c: null,
    min_temperature_c: null,
    max_temperature_c: null,
    precipitation_mm: null,
    source: "aemet",
    notes: null,
    created_at: "2026-08-04T10:00:00Z",
    updated_at: "2026-08-04T10:00:00Z",
    ...overrides,
  };
}

function households(year: number, overrides: Partial<HouseholdStat> = {}): HouseholdStat {
  return {
    id: year,
    organization_id: 1,
    reference_year: year,
    total_dwellings: 200,
    primary_dwellings: 120,
    secondary_dwellings: 60,
    empty_dwellings: 20,
    source: "ine",
    notes: null,
    created_at: "2026-08-04T10:00:00Z",
    updated_at: "2026-08-04T10:00:00Z",
    ...overrides,
  };
}

describe("SeriesMunicipio", () => {
  it("distingue una serie vacía de un error", () => {
    render(<SeriesMunicipio climate={[]} households={[]} padron={[]} />);

    expect(
      screen.getByText(/Todavía no hay series registradas/),
    ).toBeTruthy();
  });

  it("lee la serie de padrón en orden cronológico y destaca el último año", () => {
    // El backend la devuelve del año más reciente hacia atrás.
    render(
      <SeriesMunicipio
        climate={[]}
        households={[]}
        padron={[padron(2026, 318), padron(2025, 330), padron(2024, 341)]}
      />,
    );

    expect(screen.getByText(/en 2026/)).toBeTruthy();
    expect(screen.getByText("Empadronamiento")).toBeTruthy();
  });

  it("no dibuja una línea con un solo punto", () => {
    render(
      <SeriesMunicipio climate={[]} households={[]} padron={[padron(2026, 318)]} />,
    );

    expect(screen.queryByText("Empadronamiento")).toBeNull();
  });

  it("usa el detalle mensual y descarta la fila anual de resumen", () => {
    render(
      <SeriesMunicipio
        climate={[
          climate(2026, null, { avg_temperature_c: "12.40" }),
          climate(2026, 1, { avg_temperature_c: "4.10" }),
          climate(2026, 7, { avg_temperature_c: "22.30" }),
        ]}
        households={[]}
        padron={[]}
      />,
    );

    expect(screen.getByText(/Temperatura media · 2026/)).toBeTruthy();
  });

  it("omite el reparto de viviendas cuando no hay desglose", () => {
    render(
      <SeriesMunicipio
        climate={[]}
        households={[
          households(2026, {
            primary_dwellings: null,
            secondary_dwellings: null,
            empty_dwellings: null,
          }),
        ]}
        padron={[]}
      />,
    );

    expect(screen.queryByText(/^Viviendas/)).toBeNull();
  });

  it("dibuja el reparto de viviendas del año más reciente", () => {
    render(
      <SeriesMunicipio
        climate={[]}
        households={[households(2024), households(2026)]}
        padron={[]}
      />,
    );

    expect(screen.getByText(/Viviendas · 2026/)).toBeTruthy();
  });
});
