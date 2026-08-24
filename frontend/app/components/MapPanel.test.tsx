// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ReferenceCatalog,
  SiurIdentifyPoint,
  SiurMapLayer,
} from "../lib/referenceLayers";
import type { User } from "./types";
import { MapPanel } from "./MapPanel";

const runtimeHarness = vi.hoisted(() => ({
  createEntityLocation: vi.fn(),
  fetchAllGeoMapItems: vi.fn(),
  fetchMunicipalAssets: vi.fn(),
  fetchReferenceCatalog: vi.fn(),
  fetchReferenceIdentify: vi.fn(),
  getStoredToken: vi.fn(),
  handleRequestError: vi.fn(),
  municipalMapProps: null as Record<string, unknown> | null,
}));

vi.mock("next/link", () => ({
  default: () => null,
}));

vi.mock("next/navigation", () => {
  const searchParams = new URLSearchParams();
  // La vista del mapa vive en la URL, así que el panel lee también la ruta y
  // el router para reescribirla sin recargar.
  return {
    useSearchParams: () => searchParams,
    usePathname: () => "/mapa",
    useRouter: () => ({
      replace: () => {},
      push: () => {},
      refresh: () => {},
    }),
  };
});

vi.mock("../lib/geo", () => ({
  createEntityLocation: runtimeHarness.createEntityLocation,
  fetchAllGeoMapItems: runtimeHarness.fetchAllGeoMapItems,
  fetchMunicipalAssets: runtimeHarness.fetchMunicipalAssets,
}));

vi.mock("../lib/referenceLayers", async (importOriginal) => {
  const original =
    await importOriginal<typeof import("../lib/referenceLayers")>();
  return {
    ...original,
    fetchReferenceCatalog: runtimeHarness.fetchReferenceCatalog,
    fetchReferenceIdentify: runtimeHarness.fetchReferenceIdentify,
  };
});

vi.mock("../lib/session", () => ({
  useSession: () => ({
    getStoredToken: runtimeHarness.getStoredToken,
    handleRequestError: runtimeHarness.handleRequestError,
  }),
}));

vi.mock("./AssetMaintenancePanel", () => ({
  AssetMaintenancePanel: () => null,
}));

vi.mock("./MunicipalityMapDirectory", () => ({
  MunicipalityMapDirectory: () => null,
}));

vi.mock("./MunicipalMap", () => ({
  MunicipalMap: (props: Record<string, unknown>) => {
    runtimeHarness.municipalMapProps = props;
    return null;
  },
}));

const USER: User = {
  id: 1,
  email: "mapa@example.test",
  full_name: "Responsable del mapa",
  is_active: true,
  is_superuser: true,
  organizations: [
    {
      id: 7,
      municipality_id: 9,
      name: "Ayuntamiento de prueba",
      status: "active",
    },
  ],
};

const UPDATED_AT = "2026-07-27T09:00:00Z";

function catalogFixture(): ReferenceCatalog {
  return {
    snapshot: {
      id: 9,
      provider_key: "siur",
      content_sha256: "a".repeat(64),
      definition_sha256: "b".repeat(64),
      retrieved_at: UPDATED_AT,
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
        title: "Servicio local",
        upstream_protocol: "wms",
        attribution: "IDECyL",
        status: "active",
        updated_at: UPDATED_AT,
      },
    ],
    layers: [
      {
        id: 20,
        service_id: 8,
        parent_id: null,
        source_key: "layer:planning",
        node_type: "layer",
        title: "Clasificación del suelo",
        description: null,
        role: "overlay",
        renderer: "raster_tile",
        delivery_mode: "local",
        bounds_json: {
          west: -5,
          south: 41,
          east: -3,
          north: 43,
        },
        sort_order: 0,
        default_visible: true,
        default_opacity: 0.75,
        effective_visible: true,
        effective_opacity: 0.75,
        min_zoom: 4,
        max_zoom: 18,
        downloadable: false,
        delivery_available: true,
        identify_available: true,
        delivery_blocker: null,
        available_style_ids: [12],
        legend_available: true,
        metadata_available: true,
        source_substitution_status: null,
        source_substitution_notice: null,
        source_substitution_selected_layer: null,
        source_substitution_profile: null,
        source_substitution_scope: null,
        source_substitution_attribution: null,
        source_substitution_content_sha256: null,
        mirror_status: "active",
        active_version_id: 21,
        active_generation: 3,
        active_source_version: "2026-07-27",
        active_reference_at: UPDATED_AT,
        active_created_at: UPDATED_AT,
        last_run_status: "succeeded",
        last_checked_at: UPDATED_AT,
        last_sync_error_code: null,
        last_sync_error_summary: null,
        next_check_at: null,
        status: "active",
        updated_at: UPDATED_AT,
      },
    ],
    styles: [
      {
        id: 12,
        layer_id: 20,
        title: "Estilo local",
        description: null,
        sort_order: 0,
        is_default: true,
        legend_available: true,
        status: "active",
        updated_at: UPDATED_AT,
      },
    ],
  };
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function currentMapProps() {
  if (!runtimeHarness.municipalMapProps) {
    throw new Error("MunicipalMap has not rendered");
  }
  return runtimeHarness.municipalMapProps;
}

beforeEach(() => {
  runtimeHarness.createEntityLocation.mockReset();
  runtimeHarness.fetchAllGeoMapItems.mockReset();
  runtimeHarness.fetchMunicipalAssets.mockReset();
  runtimeHarness.fetchReferenceCatalog.mockReset();
  runtimeHarness.fetchReferenceIdentify.mockReset();
  runtimeHarness.getStoredToken.mockReset();
  runtimeHarness.handleRequestError.mockReset();
  runtimeHarness.municipalMapProps = null;

  runtimeHarness.fetchAllGeoMapItems.mockResolvedValue([]);
  runtimeHarness.fetchMunicipalAssets.mockResolvedValue([]);
  runtimeHarness.getStoredToken.mockReturnValue("stored-token");
  runtimeHarness.handleRequestError.mockImplementation(
    (
      _error: unknown,
      setMessage: (message: string) => void,
      fallback: string,
    ) => setMessage(fallback),
  );
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("MapPanel SIUR runtime", () => {
  it("polls after 15 seconds and preserves the loaded local catalog when refresh fails", async () => {
    vi.useFakeTimers();
    const catalog = catalogFixture();
    runtimeHarness.fetchReferenceCatalog
      .mockResolvedValueOnce(catalog)
      .mockRejectedValueOnce(new Error("catalog unavailable"));

    render(<MapPanel user={USER} />);
    await flushPromises();

    expect(runtimeHarness.fetchReferenceCatalog).toHaveBeenCalledTimes(1);
    expect(runtimeHarness.fetchReferenceCatalog).toHaveBeenCalledWith(
      7,
      "stored-token",
      expect.any(AbortSignal),
    );
    expect(currentMapProps().siurLayers).toEqual([
      expect.objectContaining({
        generation: 3,
        layerId: 20,
        tileUrl:
          "/api/organizations/7/reference-layers/20/tiles/{z}/{x}/{y}.png?style_id=12&version_id=21&generation=3",
        versionId: 21,
      }),
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(14_999);
    });
    expect(runtimeHarness.fetchReferenceCatalog).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(runtimeHarness.fetchReferenceCatalog).toHaveBeenCalledTimes(2);
    expect(
      screen.getByText(
        "No se pudo actualizar la cartografía SIUR; se mantiene la versión cargada.",
      ),
    ).toBeTruthy();
    expect(currentMapProps().siurLayers).toEqual([
      expect.objectContaining({
        generation: 3,
        layerId: 20,
        versionId: 21,
      }),
    ]);
  });

  it("forwards an authenticated local identify and renders its properties", async () => {
    const catalog = catalogFixture();
    runtimeHarness.fetchReferenceCatalog.mockResolvedValue(catalog);
    runtimeHarness.fetchReferenceIdentify.mockResolvedValue({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: {
            clase: "Suelo urbano",
            codigo: 42,
          },
          geometry: null,
        },
      ],
    });

    render(<MapPanel user={USER} />);
    await flushPromises();

    const layer = (currentMapProps().siurLayers as SiurMapLayer[])[0];
    const point: SiurIdentifyPoint = {
      layer,
      z: 8,
      x: 120,
      y: 95,
      pixelX: 4,
      pixelY: 250,
    };
    act(() => {
      (
        currentMapProps().onSiurIdentify as (
          identifyPoint: SiurIdentifyPoint,
        ) => void
      )(point);
    });
    await flushPromises();

    expect(runtimeHarness.fetchReferenceIdentify).toHaveBeenCalledWith(
      point,
      "stored-token",
      expect.any(AbortSignal),
    );
    expect(
      screen.getByRole("heading", { name: "Resultado 1" }),
    ).toBeTruthy();
    expect(screen.getByText("clase")).toBeTruthy();
    expect(screen.getByText("Suelo urbano")).toBeTruthy();
    expect(screen.getByText("codigo")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
  });
});
