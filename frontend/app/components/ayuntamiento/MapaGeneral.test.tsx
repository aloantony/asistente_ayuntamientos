// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GeoMapItem } from "../types";

const fetchAllGeoMapItems = vi.fn();
const mapProps = vi.fn();

vi.mock("../../lib/geo", async () => {
  const actual = await vi.importActual<typeof import("../../lib/geo")>("../../lib/geo");
  return {
    ...actual,
    fetchAllGeoMapItems: (...args: unknown[]) => fetchAllGeoMapItems(...args),
  };
});

// Leaflet no funciona en jsdom; lo que interesa aquí es qué recibe el mapa.
vi.mock("../MunicipalMap", () => ({
  MunicipalMap: (props: Record<string, unknown>) => {
    mapProps(props);
    return <div data-testid="mapa" />;
  },
}));

const { MapaGeneral, buildLayers, itemKey, matchesSearch } = await import(
  "./MapaGeneral"
);

function item(overrides: Partial<GeoMapItem> = {}): GeoMapItem {
  return {
    entity_type: "asset",
    entity_id: 1,
    role: "primary",
    layer_key: "alumbrado",
    layer_label: "Alumbrado público",
    layer_color: "#d8a13a",
    item_type: "Luminaria de forja",
    condition_status: "good",
    title: "Farola de la plaza",
    subtitle: null,
    status: "active",
    priority: null,
    organization_id: 1,
    organization_name: "Ayuntamiento",
    detail_path: "/inventario/1",
    location: {} as GeoMapItem["location"],
    ...overrides,
  };
}

afterEach(cleanup);
beforeEach(() => {
  fetchAllGeoMapItems.mockReset().mockResolvedValue([]);
  mapProps.mockReset();
});

describe("buildLayers", () => {
  it("agrupa por capa, cuenta y ordena por rótulo", () => {
    const layers = buildLayers([
      item({ entity_id: 1 }),
      item({ entity_id: 2 }),
      item({
        entity_id: 3,
        layer_key: "agua",
        layer_label: "Abastecimiento de agua",
        layer_color: "#2f7fb5",
      }),
    ]);

    expect(layers.map((layer) => layer.label)).toEqual([
      "Abastecimiento de agua",
      "Alumbrado público",
    ]);
    expect(layers[1].count).toBe(2);
    expect(layers[1].color).toBe("#d8a13a");
  });

  it("no inventa capas vacías", () => {
    // El árbol sale de los elementos: una capa sin nada dentro no existe.
    expect(buildLayers([])).toEqual([]);
  });
});

describe("itemKey", () => {
  it("distingue dos entidades distintas con el mismo id", () => {
    expect(itemKey(item({ entity_type: "asset", entity_id: 7 }))).not.toBe(
      itemKey(item({ entity_type: "requirement", entity_id: 7 })),
    );
  });
});

describe("matchesSearch", () => {
  it("busca en título, subtítulo y capa, sin distinguir mayúsculas", () => {
    const target = item({ subtitle: "Calle Mayor" });

    expect(matchesSearch(target, "farola")).toBe(true);
    expect(matchesSearch(target, "MAYOR")).toBe(true);
    expect(matchesSearch(target, "alumbrado")).toBe(true);
    expect(matchesSearch(target, "cementerio")).toBe(false);
  });

  it("una búsqueda vacía no filtra nada", () => {
    expect(matchesSearch(item(), "   ")).toBe(true);
  });
});

describe("MapaGeneral", () => {
  it("declara la falta de permiso sin llegar a pedir el mapa", () => {
    render(<MapaGeneral canViewMap={false} organizationId={1} />);

    expect(screen.getByText("Mapa no autorizado")).toBeTruthy();
    expect(fetchAllGeoMapItems).not.toHaveBeenCalled();
  });

  it("avisa cuando no hay nada situado todavía", async () => {
    render(<MapaGeneral canViewMap organizationId={1} />);

    await waitFor(() =>
      expect(screen.getByText(/Todavía no hay nada situado/)).toBeTruthy(),
    );
  });

  it("pasa al mapa los colores de cada capa", async () => {
    fetchAllGeoMapItems.mockResolvedValue([item()]);

    render(<MapaGeneral canViewMap organizationId={1} />);

    await waitFor(() => expect(mapProps).toHaveBeenCalled());
    const last = mapProps.mock.calls.at(-1)?.[0];
    expect(last.markerColors).toEqual({ alumbrado: "#d8a13a" });
  });

  it("apagar una capa la quita del mapa", async () => {
    fetchAllGeoMapItems.mockResolvedValue([
      item({ entity_id: 1 }),
      item({
        entity_id: 2,
        layer_key: "agua",
        layer_label: "Abastecimiento de agua",
      }),
    ]);

    render(<MapaGeneral canViewMap organizationId={1} />);
    await waitFor(() => expect(screen.getByText("2 de 2 elementos")).toBeTruthy());

    fireEvent.click(screen.getByRole("checkbox", { name: /Alumbrado/ }));

    await waitFor(() =>
      expect(screen.getByText("1 de 2 elementos (filtrados)")).toBeTruthy(),
    );
    const last = mapProps.mock.calls.at(-1)?.[0];
    expect(last.items).toHaveLength(1);
  });

  it("la búsqueda filtra sin volver a pedir el mapa", async () => {
    fetchAllGeoMapItems.mockResolvedValue([
      item({ entity_id: 1, title: "Farola de la plaza" }),
      item({ entity_id: 2, title: "Fuente del olmo" }),
    ]);

    render(<MapaGeneral canViewMap organizationId={1} />);
    await waitFor(() => expect(fetchAllGeoMapItems).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByPlaceholderText("Buscar en el mapa…"), {
      target: { value: "fuente" },
    });

    await waitFor(() =>
      expect(screen.getByText("1 de 2 elementos (filtrados)")).toBeTruthy(),
    );
    // Filtrar es local: el mapa ya está cargado entero.
    expect(fetchAllGeoMapItems).toHaveBeenCalledTimes(1);
  });

  it("ofrece reintentar cuando la carga falla", async () => {
    fetchAllGeoMapItems.mockRejectedValue(new Error("boom"));

    render(<MapaGeneral canViewMap organizationId={1} />);

    await waitFor(() => expect(screen.getByText("Mapa no disponible")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeTruthy();
  });
});
