// @vitest-environment jsdom

import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildReferenceTileUrl,
  type SiurMapLayer,
} from "../lib/referenceLayers";
import { MunicipalMap } from "./MunicipalMap";

const leafletHarness = vi.hoisted(() => ({
  maps: [] as unknown[],
  tileLayers: [] as unknown[],
}));

vi.mock("leaflet", () => {
  type EventHandler = (event: unknown) => void;

  class FakeBounds {
    private valid: boolean;

    constructor(points: unknown) {
      this.valid = Array.isArray(points) && points.length > 0;
    }

    extend() {
      this.valid = true;
      return this;
    }

    getCenter() {
      return { lat: 42.3439, lng: -3.6969 };
    }

    getNorthEast() {
      return { lat: 42.3439, lng: -3.6969 };
    }

    getSouthWest() {
      return { lat: 42.3439, lng: -3.6969 };
    }

    isValid() {
      return this.valid;
    }

    pad() {
      return this;
    }
  }

  class FakeMap {
    readonly boxZoom = {
      disable: vi.fn(),
      enable: vi.fn(),
    };
    readonly dragging = {
      disable: vi.fn(),
      enable: vi.fn(),
    };
    readonly layers = new Set<unknown>();
    readonly touchZoom = {
      disable: vi.fn(),
      enable: vi.fn(),
    };
    readonly handlers = new Map<string, Set<EventHandler>>();
    readonly container: HTMLElement;
    zoom = 8;

    constructor(container: HTMLElement) {
      this.container = container;
    }

    closePopup() {
      return this;
    }

    createPane() {
      return document.createElement("div");
    }

    emit(eventName: string, event: unknown) {
      this.handlers
        .get(eventName)
        ?.forEach((handler) => handler(event));
    }

    fitBounds() {
      return this;
    }

    getContainer() {
      return this.container;
    }

    getZoom() {
      return this.zoom;
    }

    hasLayer(layer: unknown) {
      return this.layers.has(layer);
    }

    invalidateSize() {
      return this;
    }

    locate() {
      return this;
    }

    off(eventName: string, handler: EventHandler) {
      this.handlers.get(eventName)?.delete(handler);
      return this;
    }

    on(eventName: string, handler: EventHandler) {
      const handlers = this.handlers.get(eventName) ?? new Set<EventHandler>();
      handlers.add(handler);
      this.handlers.set(eventName, handlers);
      return this;
    }

    project() {
      return { x: 513.9, y: 767.2 };
    }

    remove() {
      this.handlers.clear();
      this.layers.clear();
      return this;
    }

    setView() {
      return this;
    }

    stopLocate() {
      return this;
    }

    wrapLatLng(latlng: unknown) {
      return latlng;
    }
  }

  class FakeTileLayer {
    readonly handlers = new Map<string, Set<EventHandler>>();
    readonly initialUrl: string;
    readonly setUrlHistory: string[] = [];
    map: FakeMap | null = null;
    opacity: number | null = null;
    url: string;
    zIndex: number | null = null;

    constructor(url: string) {
      this.initialUrl = url;
      this.url = url;
    }

    addTo(map: FakeMap) {
      this.map = map;
      map.layers.add(this);
      return this;
    }

    emit(eventName: string) {
      this.handlers
        .get(eventName)
        ?.forEach((handler) => handler({ type: eventName }));
    }

    on(eventName: string, handler: EventHandler) {
      const handlers = this.handlers.get(eventName) ?? new Set<EventHandler>();
      handlers.add(handler);
      this.handlers.set(eventName, handlers);
      return this;
    }

    remove() {
      this.map?.layers.delete(this);
      this.map = null;
      return this;
    }

    setOpacity(opacity: number) {
      this.opacity = opacity;
      return this;
    }

    setUrl(url: string) {
      this.url = url;
      this.setUrlHistory.push(url);
      return this;
    }

    setZIndex(zIndex: number) {
      this.zIndex = zIndex;
      return this;
    }
  }

  return {
    control: {
      zoom: () => ({
        addTo: () => undefined,
      }),
    },
    latLngBounds: (points: unknown) => new FakeBounds(points),
    map: (container: HTMLElement) => {
      const map = new FakeMap(container);
      leafletHarness.maps.push(map);
      return map;
    },
    tileLayer: (url: string) => {
      const tileLayer = new FakeTileLayer(url);
      leafletHarness.tileLayers.push(tileLayer);
      return tileLayer;
    },
  };
});

type FakeMapHandle = {
  emit: (eventName: string, event: unknown) => void;
};

type FakeTileLayerHandle = {
  emit: (eventName: string) => void;
  initialUrl: string;
  opacity: number | null;
  setUrlHistory: string[];
  url: string;
  zIndex: number | null;
};

function makeLayer(
  overrides: Partial<SiurMapLayer> = {},
): SiurMapLayer {
  const layer = {
    organizationId: 7,
    layerId: 20,
    versionId: 21,
    generation: 1,
    role: "overlay",
    title: "Clasificación del suelo",
    styleId: 12,
    attribution: null,
    bounds: null,
    minZoom: null,
    maxZoom: null,
    opacity: 0.65,
    visible: true,
    identifyAvailable: true,
    zIndex: 4,
    ...overrides,
  } satisfies Omit<SiurMapLayer, "tileUrl">;

  return {
    ...layer,
    tileUrl:
      overrides.tileUrl ??
      buildReferenceTileUrl(
        layer.organizationId,
        layer.layerId,
        layer.styleId,
        layer.versionId,
        layer.generation,
      ),
  };
}

function mapProps(
  overrides: Partial<React.ComponentProps<typeof MunicipalMap>> = {},
): React.ComponentProps<typeof MunicipalMap> {
  return {
    items: [],
    onSelectItem: vi.fn(),
    ...overrides,
  };
}

function findTileLayer(url: string) {
  const tileLayer = leafletHarness.tileLayers.find(
    (candidate) =>
      (candidate as FakeTileLayerHandle).initialUrl === url,
  );
  if (!tileLayer) {
    throw new Error(`Expected Leaflet tile layer for ${url}`);
  }
  return tileLayer as FakeTileLayerHandle;
}

beforeEach(() => {
  leafletHarness.maps.length = 0;
  leafletHarness.tileLayers.length = 0;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MunicipalMap SIUR tile runtime", () => {
  it("deduplicates overlay tile errors and recovers after a version URL change and tile load", async () => {
    const onSiurTileError = vi.fn();
    const onSiurTileLoad = vi.fn();
    const overlayV1 = makeLayer();
    const base = makeLayer({
      identifyAvailable: false,
      layerId: 10,
      opacity: 1,
      role: "base",
      styleId: null,
      title: "IMAGEN",
      versionId: 3,
      zIndex: 1,
    });
    const props = mapProps({
      baseLayerId: base.layerId,
      onSiurTileError,
      onSiurTileLoad,
      siurLayers: [base, overlayV1],
    });
    const { rerender } = render(<MunicipalMap {...props} />);

    await waitFor(() => expect(leafletHarness.tileLayers).toHaveLength(2));
    const overlayTileLayer = findTileLayer(overlayV1.tileUrl);

    expect(overlayTileLayer.initialUrl).toBe(
      "/api/organizations/7/reference-layers/20/tiles/{z}/{x}/{y}.png?style_id=12&version_id=21&generation=1",
    );
    expect(overlayTileLayer.initialUrl.startsWith("/api/")).toBe(true);
    expect(overlayTileLayer.opacity).toBe(0.65);
    expect(overlayTileLayer.zIndex).toBe(4);

    act(() => {
      overlayTileLayer.emit("tileerror");
      overlayTileLayer.emit("tileerror");
    });
    expect(onSiurTileError).toHaveBeenCalledTimes(1);
    expect(onSiurTileError).toHaveBeenLastCalledWith(
      overlayV1.layerId,
      overlayV1.title,
    );

    const overlayV2 = makeLayer({
      generation: 2,
      versionId: 22,
    });
    rerender(
      <MunicipalMap
        {...props}
        siurLayers={[base, overlayV2]}
      />,
    );

    await waitFor(() =>
      expect(overlayTileLayer.setUrlHistory).toEqual([overlayV2.tileUrl]),
    );
    expect(overlayTileLayer.url).toBe(
      "/api/organizations/7/reference-layers/20/tiles/{z}/{x}/{y}.png?style_id=12&version_id=22&generation=2",
    );
    expect(leafletHarness.tileLayers).toHaveLength(2);

    act(() => overlayTileLayer.emit("tileerror"));
    expect(onSiurTileError).toHaveBeenCalledTimes(2);

    act(() => {
      overlayTileLayer.emit("tileload");
      overlayTileLayer.emit("tileerror");
    });
    expect(onSiurTileLoad).toHaveBeenCalledTimes(1);
    expect(onSiurTileLoad).toHaveBeenCalledWith(overlayV2.layerId);
    expect(onSiurTileError).toHaveBeenCalledTimes(3);
  });

  it("deduplicates base tile errors and rearms the notification after a successful tile", async () => {
    const onSiurTileError = vi.fn();
    const onSiurTileLoad = vi.fn();
    const base = makeLayer({
      identifyAvailable: false,
      layerId: 10,
      opacity: 1,
      role: "base",
      styleId: null,
      title: "RELIEVE",
      versionId: 5,
      zIndex: 1,
    });

    render(
      <MunicipalMap
        {...mapProps({
          baseLayerId: base.layerId,
          onSiurTileError,
          onSiurTileLoad,
          siurLayers: [base],
        })}
      />,
    );

    await waitFor(() => expect(leafletHarness.tileLayers).toHaveLength(1));
    const baseTileLayer = findTileLayer(base.tileUrl);
    expect(baseTileLayer.initialUrl).toBe(
      "/api/organizations/7/reference-layers/10/tiles/{z}/{x}/{y}.png?version_id=5&generation=1",
    );
    expect(baseTileLayer.zIndex).toBe(0);

    act(() => {
      baseTileLayer.emit("tileerror");
      baseTileLayer.emit("tileerror");
    });
    expect(onSiurTileError).toHaveBeenCalledTimes(1);

    act(() => {
      baseTileLayer.emit("tileload");
      baseTileLayer.emit("tileerror");
    });
    expect(onSiurTileLoad).toHaveBeenCalledTimes(1);
    expect(onSiurTileLoad).toHaveBeenCalledWith(base.layerId);
    expect(onSiurTileError).toHaveBeenCalledTimes(2);
  });

  it("identifies the top local layer using the projected tile pixel", async () => {
    const onSiurIdentify = vi.fn();
    const bottom = makeLayer({
      layerId: 20,
      title: "Planeamiento",
      zIndex: 1,
    });
    const top = makeLayer({
      layerId: 21,
      styleId: 13,
      title: "Clasificación",
      versionId: 31,
      zIndex: 2,
    });

    render(
      <MunicipalMap
        {...mapProps({
          onSiurIdentify,
          siurLayers: [bottom, top],
        })}
      />,
    );

    await waitFor(() => expect(leafletHarness.maps).toHaveLength(1));
    const map = leafletHarness.maps[0] as FakeMapHandle;
    act(() => {
      map.emit("click", {
        latlng: { lat: 42, lng: -4 },
        originalEvent: { target: null },
      });
    });

    expect(onSiurIdentify).toHaveBeenCalledTimes(1);
    expect(onSiurIdentify).toHaveBeenCalledWith({
      layer: top,
      pixelX: 1,
      pixelY: 255,
      x: 2,
      y: 2,
      z: 8,
    });
  });
});
