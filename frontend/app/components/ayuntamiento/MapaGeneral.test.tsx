// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReferenceCatalog } from "../../lib/referenceLayers";
import type { GeoMapItem } from "../types";

const fetchAllGeoMapItems = vi.fn();
const fetchReferenceCatalog = vi.fn();
const mapProps = vi.fn();

vi.mock("../../lib/referenceLayers", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/referenceLayers")>(
      "../../lib/referenceLayers",
    );
  return {
    ...actual,
    fetchReferenceCatalog: (...args: unknown[]) =>
      fetchReferenceCatalog(...args),
  };
});

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

const CATALOG_AT = "2026-09-03T10:00:00Z";

/** Catálogo mínimo con un fondo local, que es lo único que esta pantalla usa. */
function catalogWithBaseMap(): ReferenceCatalog {
  return {
    snapshot: {
      id: 91,
      provider_key: "siur",
      content_sha256: "a".repeat(64),
      definition_sha256: "b".repeat(64),
      retrieved_at: CATALOG_AT,
      service_count: 1,
      group_count: 0,
      layer_count: 1,
      unresolved_count: 0,
      status: "applied",
      is_current: true,
    },
    organization_id: 7,
    services: [
      {
        id: 8,
        title: "IGN Base",
        upstream_protocol: "wms",
        attribution: "CC BY 4.0 scne.es",
        status: "active",
        updated_at: CATALOG_AT,
      },
    ],
    layers: [
      {
        id: 4,
        service_id: 8,
        parent_id: null,
        source_key: "layer:siur:fondo",
        node_type: "layer",
        title: "MAPA",
        description: null,
        role: "base",
        renderer: "raster_tile",
        delivery_mode: "local",
        bounds_json: null,
        sort_order: 0,
        default_visible: true,
        default_opacity: 1,
        effective_visible: true,
        effective_opacity: 1,
        min_zoom: null,
        max_zoom: null,
        downloadable: false,
        delivery_available: true,
        identify_available: false,
        delivery_blocker: null,
        available_style_ids: [],
        legend_available: false,
        metadata_available: false,
        source_substitution_status: null,
        source_substitution_notice: null,
        source_substitution_selected_layer: null,
        source_substitution_profile: null,
        source_substitution_scope: null,
        source_substitution_attribution: null,
        source_substitution_content_sha256: null,
        mirror_status: "active",
        active_version_id: 21,
        active_generation: 1,
        active_source_version: "v1",
        active_reference_at: CATALOG_AT,
        active_created_at: CATALOG_AT,
        last_run_status: "succeeded",
        last_checked_at: CATALOG_AT,
        last_sync_error_code: null,
        last_sync_error_summary: null,
        next_check_at: null,
        status: "active",
        updated_at: CATALOG_AT,
      },
    ],
    styles: [],
  };
}

afterEach(cleanup);
beforeEach(() => {
  fetchAllGeoMapItems.mockReset().mockResolvedValue([]);
  fetchReferenceCatalog.mockReset().mockRejectedValue(new Error("sin catálogo"));
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

  it("pone bajo los marcadores el fondo del espejo cartográfico", async () => {
    // Sin esto la pestaña montaba el visor sin una sola capa y el ayuntamiento
    // veía sus elementos flotando sobre una cuadrícula vacía.
    fetchAllGeoMapItems.mockResolvedValue([item()]);
    fetchReferenceCatalog.mockResolvedValue(catalogWithBaseMap());

    render(<MapaGeneral canViewMap organizationId={7} />);

    await waitFor(() => {
      const last = mapProps.mock.calls.at(-1)?.[0];
      expect(last.baseLayerId).toBe(4);
    });
    const last = mapProps.mock.calls.at(-1)?.[0];
    expect(last.siurLayers).toHaveLength(1);
    expect(last.siurLayers[0].role).toBe("base");
  });

  it("sin catálogo sigue enseñando los elementos y lo dice", async () => {
    fetchAllGeoMapItems.mockResolvedValue([item()]);

    render(<MapaGeneral canViewMap organizationId={7} />);

    await waitFor(() =>
      expect(
        screen.getByText("Sin fondo cartográfico disponible"),
      ).toBeTruthy(),
    );
    const last = mapProps.mock.calls.at(-1)?.[0];
    expect(last.baseLayerId).toBeNull();
    expect(last.items).toHaveLength(1);
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
