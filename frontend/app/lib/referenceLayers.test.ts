import { describe, expect, it } from "vitest";
import {
  buildReferenceIdentifyPath,
  buildReferenceLayerTree,
  buildReferenceTileUrl,
  buildSiurMapLayers,
  parseReferenceLayerBounds,
  reconcileSiurPreferences,
  selectLocalBaseMapLayer,
  selectTopIdentifyLayer,
  tileCoordinatesForProjectedPoint,
  validateReferenceCatalog,
  type ReferenceCatalog,
  type ReferenceLayer,
  type ReferenceLayerStyle,
  type SiurMapLayer,
} from "./referenceLayers";

const UPDATED_AT = "2026-07-17T10:00:00Z";

function makeLayer(overrides: Partial<ReferenceLayer>): ReferenceLayer {
  return {
    id: 1,
    service_id: null,
    parent_id: null,
    source_key: "layer:one",
    node_type: "layer",
    title: "Capa uno",
    description: null,
    role: "overlay",
    renderer: "raster_tile",
    delivery_mode: "proxy",
    bounds_json: null,
    sort_order: 0,
    default_visible: false,
    default_opacity: 1,
    effective_visible: false,
    effective_opacity: 1,
    min_zoom: null,
    max_zoom: null,
    downloadable: false,
    delivery_available: true,
    identify_available: true,
    delivery_blocker: null,
    available_style_ids: [],
    legend_available: true,
    metadata_available: false,
    mirror_status: "active",
    active_version_id: 21,
    active_generation: 1,
    active_source_version: "v1",
    active_reference_at: UPDATED_AT,
    active_created_at: UPDATED_AT,
    last_run_status: "succeeded",
    last_checked_at: UPDATED_AT,
    last_sync_error_code: null,
    last_sync_error_summary: null,
    next_check_at: null,
    status: "active",
    updated_at: UPDATED_AT,
    ...overrides,
  };
}

function makeStyle(
  overrides: Partial<ReferenceLayerStyle>,
): ReferenceLayerStyle {
  return {
    id: 10,
    layer_id: 2,
    title: "Estilo",
    description: null,
    sort_order: 0,
    is_default: false,
    legend_available: true,
    status: "active",
    updated_at: UPDATED_AT,
    ...overrides,
  };
}

function makeCatalog(
  layers: ReferenceLayer[],
  styles: ReferenceLayerStyle[] = [],
): ReferenceCatalog {
  return {
    snapshot: {
      id: 91,
      provider_key: "siur",
      content_sha256: "a".repeat(64),
      definition_sha256: "b".repeat(64),
      retrieved_at: UPDATED_AT,
      service_count: 1,
      group_count: layers.filter((layer) => layer.node_type === "group").length,
      layer_count: layers.filter((layer) => layer.node_type === "layer").length,
      unresolved_count: 0,
      status: "applied",
      is_current: true,
    },
    organization_id: 7,
    services: [
      {
        id: 8,
        title: "Servicio SIUR",
        upstream_protocol: "wms",
        attribution: "Junta <script>alert(1)</script>",
        status: "active",
        updated_at: UPDATED_AT,
      },
    ],
    layers,
    styles,
  };
}

function makeMapLayer(overrides: Partial<SiurMapLayer>): SiurMapLayer {
  return {
    organizationId: 7,
    layerId: 1,
    role: "overlay",
    title: "Capa",
    tileUrl: "/api/organizations/7/reference-layers/1/tiles/{z}/{x}/{y}.png",
    styleId: null,
    attribution: null,
    bounds: null,
    minZoom: null,
    maxZoom: null,
    opacity: 1,
    visible: true,
    identifyAvailable: true,
    zIndex: 1,
    ...overrides,
  };
}

describe("local base map selection", () => {
  it("selects only internal base descriptors and falls back deterministically", () => {
    const street = makeMapLayer({
      layerId: 10,
      role: "base",
      zIndex: 1,
      tileUrl: "/api/local/street/{z}/{x}/{y}.png",
    });
    const topographic = makeMapLayer({
      layerId: 11,
      role: "base",
      zIndex: 2,
      tileUrl: "/api/local/topographic/{z}/{x}/{y}.png",
    });
    const overlay = makeMapLayer({ layerId: 12, role: "overlay", zIndex: 3 });

    expect(selectLocalBaseMapLayer([overlay, topographic, street], "street"))
      .toBe(street);
    expect(
      selectLocalBaseMapLayer([overlay, topographic, street], "topographic"),
    ).toBe(topographic);
    expect(selectLocalBaseMapLayer([overlay, street], "topographic")).toBe(
      street,
    );
    expect(selectLocalBaseMapLayer([overlay], "street")).toBeNull();
    expect([street, topographic].every((layer) => layer.tileUrl.startsWith("/")))
      .toBe(true);
  });

  it("never renders a base disabled by the SIUR layer controls", () => {
    const hiddenStreet = makeMapLayer({
      layerId: 10,
      role: "base",
      visible: false,
      zIndex: 1,
    });
    const visibleTopographic = makeMapLayer({
      layerId: 11,
      role: "base",
      visible: true,
      zIndex: 2,
    });

    expect(
      selectLocalBaseMapLayer(
        [hiddenStreet, visibleTopographic],
        "street",
      ),
    ).toBe(visibleTopographic);
    expect(
      selectLocalBaseMapLayer(
        [
          hiddenStreet,
          { ...visibleTopographic, visible: false },
        ],
        "topographic",
      ),
    ).toBeNull();
    expect(
      selectLocalBaseMapLayer(
        [{ ...visibleTopographic, opacity: 0 }],
        "topographic",
      ),
    ).toBeNull();
  });
});

describe("reference catalog integrity and hierarchy", () => {
  it("fails closed when snapshot counts differ from the returned arrays", () => {
    const catalog = makeCatalog([makeLayer({ service_id: 8 })]);
    catalog.snapshot.layer_count = 2;

    expect(() => validateReferenceCatalog(catalog, 7)).toThrow(
      "recuentos",
    );
  });

  it("preserves every node while sorting siblings and recovering bad parents", () => {
    const layers = [
      makeLayer({
        id: 1,
        node_type: "group",
        source_key: "group:root",
        title: "Raíz",
        service_id: null,
        sort_order: 20,
      }),
      makeLayer({ id: 3, parent_id: 1, title: "Segunda", sort_order: 2 }),
      makeLayer({ id: 2, parent_id: 1, title: "Primera", sort_order: 1 }),
      makeLayer({ id: 4, parent_id: 999, title: "Huérfana", sort_order: 10 }),
      makeLayer({
        id: 5,
        node_type: "group",
        parent_id: 6,
        title: "Ciclo A",
        sort_order: 0,
      }),
      makeLayer({
        id: 6,
        node_type: "group",
        parent_id: 5,
        title: "Ciclo B",
        sort_order: 0,
      }),
      makeLayer({ id: 7, parent_id: 5, title: "Dentro del ciclo" }),
    ];

    const tree = buildReferenceLayerTree(layers);

    expect(tree.layersInCanonicalOrder.map((layer) => layer.id).sort()).toEqual([
      1, 2, 3, 4, 5, 6, 7,
    ]);
    const root = tree.roots.find((node) => node.layer.id === 1);
    expect(root?.children.map((node) => node.layer.id)).toEqual([2, 3]);
    expect(tree.warnings).toHaveLength(2);
  });
});

describe("approved SIUR delivery descriptors", () => {
  it("falls back to the first allowed numeric style and builds only internal paths", () => {
    const group = makeLayer({
      id: 1,
      node_type: "group",
      source_key: "group:planning",
      title: "Planeamiento",
      mirror_status: "not_applicable",
      active_version_id: null,
      active_generation: null,
    });
    const layer = makeLayer({
      id: 2,
      service_id: 8,
      parent_id: 1,
      available_style_ids: [12],
      delivery_available: false,
      delivery_blocker: "style_unsupported",
      effective_visible: true,
      effective_opacity: 0.65,
    });
    const catalog = makeCatalog(
      [group, layer],
      [
        makeStyle({ id: 11, is_default: true, title: "No verificado" }),
        makeStyle({ id: 12, sort_order: 2, title: "Verificado" }),
      ],
    );
    validateReferenceCatalog(catalog, 7);

    const preferences = reconcileSiurPreferences(catalog, {
      layers: {
        "2": { visible: true, opacity: 0.4, styleId: 999 },
      },
      stackOrder: [999, 2, 2],
    });
    const descriptors = buildSiurMapLayers(catalog, preferences);

    expect(preferences.layers["2"].styleId).toBe(12);
    expect(preferences.stackOrder).toEqual([2]);
    expect(descriptors).toHaveLength(1);
    expect(descriptors[0].tileUrl).toBe(
      "/api/organizations/7/reference-layers/2/tiles/{z}/{x}/{y}.png?style_id=12",
    );
    expect(descriptors[0].attribution).toContain("&lt;script&gt;");
    expect(descriptors[0].attribution).not.toContain("<script>");
    expect(() => buildReferenceTileUrl(7, 2, -1)).toThrow(TypeError);
  });

  it("uses only the top visible queryable layer within zoom and bounds", () => {
    const bottom = makeMapLayer({ layerId: 1, zIndex: 1 });
    const outside = makeMapLayer({
      layerId: 2,
      zIndex: 2,
      bounds: { west: -10, south: 30, east: -8, north: 32 },
    });
    const transparent = makeMapLayer({ layerId: 3, zIndex: 3, opacity: 0 });
    const top = makeMapLayer({ layerId: 4, zIndex: 4, minZoom: 5 });

    expect(selectTopIdentifyLayer([bottom, outside, transparent, top], 8, 42, -4))
      .toMatchObject({ layerId: 4 });
    expect(selectTopIdentifyLayer([bottom, outside, transparent, top], 4, 42, -4))
      .toMatchObject({ layerId: 1 });
  });

  it("calculates bounded tile pixels and emits an allowlisted identify query", () => {
    expect(tileCoordinatesForProjectedPoint(513.9, 767.2, 2)).toEqual({
      x: 2,
      y: 2,
      pixelX: 1,
      pixelY: 255,
    });
    const layer = makeMapLayer({ layerId: 4, styleId: 12 });
    const path = buildReferenceIdentifyPath({
      layer,
      z: 8,
      x: 120,
      y: 95,
      pixelX: 4,
      pixelY: 250,
    });
    const query = new URLSearchParams(path.split("?")[1]);
    expect([...query.keys()].sort()).toEqual(
      [
        "feature_count",
        "pixel_x",
        "pixel_y",
        "style_id",
        "x",
        "y",
        "z",
      ].sort(),
    );
    for (const forbidden of [
      "bbox",
      "crs",
      "format",
      "layers",
      "request",
      "service",
      "url",
    ]) {
      expect(query.has(forbidden)).toBe(false);
    }
  });

  it("rejects malformed geographic bounds instead of rendering them", () => {
    expect(
      parseReferenceLayerBounds({
        west: "-7",
        south: 40,
        east: -1,
        north: 43,
      }),
    ).toBeUndefined();
  });

  it("does not treat stale approved-style IDs as an exception to license denial", () => {
    const layer = makeLayer({
      id: 2,
      service_id: 8,
      available_style_ids: [12],
      delivery_available: false,
      delivery_blocker: "license_not_approved",
    });
    const catalog = makeCatalog([layer], [makeStyle({ id: 12 })]);
    const preferences = reconcileSiurPreferences(catalog);

    expect(buildSiurMapLayers(catalog, preferences)).toEqual([]);
  });
});
