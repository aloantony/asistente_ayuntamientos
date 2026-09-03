import { describe, expect, it } from "vitest";
import {
  applyLocalBaseMapSelection,
  buildReferenceIdentifyPath,
  buildReferenceLayerTree,
  buildReferenceMetadataUrl,
  buildReferenceTileUrl,
  buildSiurMapLayers,
  defaultLocalBaseMapSelection,
  listLocalBaseMapLayers,
  parseStoredMapBaseLayerPreference,
  parseReferenceLayerBounds,
  referenceLayerBlocker,
  reconcileSiurPreferences,
  resolveLocalBaseMapLayerId,
  serializeMapBaseLayerPreference,
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
  const layer = {
    organizationId: 7,
    layerId: 1,
    versionId: 1,
    generation: 1,
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

describe("local base map selection", () => {
  it("selects the requested local layer id independently of input order", () => {
    const image = makeMapLayer({
      layerId: 10,
      role: "base",
      title: "IMAGEN",
      zIndex: 1,
    });
    const map = makeMapLayer({
      layerId: 11,
      role: "base",
      title: "MAPA",
      zIndex: 2,
    });
    const relief = makeMapLayer({
      layerId: 12,
      role: "base",
      title: "RELIEVE",
      zIndex: 3,
    });
    const overlay = makeMapLayer({ layerId: 13, role: "overlay", zIndex: 4 });

    expect(
      selectLocalBaseMapLayer([relief, overlay, image, map], relief.layerId),
    ).toBe(relief);
    expect(selectLocalBaseMapLayer([overlay], image.layerId)).toBeNull();
    expect(selectLocalBaseMapLayer([image, map, relief], null)).toBeNull();
  });

  it("never renders a hidden, transparent, unavailable, or remote base", () => {
    const hidden = makeMapLayer({
      layerId: 10,
      role: "base",
      visible: false,
      zIndex: 1,
    });
    const transparent = makeMapLayer({
      layerId: 11,
      opacity: 0,
      role: "base",
      visible: true,
      zIndex: 2,
    });
    const remote = makeMapLayer({
      layerId: 12,
      role: "base",
      tileUrl: "https://tiles.example.test/{z}/{x}/{y}.png",
      visible: true,
      zIndex: 3,
    });

    expect(selectLocalBaseMapLayer([hidden], hidden.layerId)).toBeNull();
    expect(
      selectLocalBaseMapLayer([transparent], transparent.layerId),
    ).toBeNull();
    expect(selectLocalBaseMapLayer([remote], remote.layerId)).toBeNull();
    expect(listLocalBaseMapLayers([transparent, remote])).toEqual([]);
  });

  it("lists all selectable local bases in canonical order", () => {
    const image = makeMapLayer({
      layerId: 10,
      role: "base",
      title: "IMAGEN",
      visible: true,
      zIndex: 1,
    });
    const map = makeMapLayer({
      layerId: 11,
      role: "base",
      title: "MAPA",
      visible: false,
      zIndex: 2,
    });
    const relief = makeMapLayer({
      layerId: 12,
      role: "base",
      title: "RELIEVE",
      visible: false,
      zIndex: 3,
    });

    expect(
      listLocalBaseMapLayers([relief, map, image]).map((layer) => layer.title),
    ).toEqual(["IMAGEN", "MAPA", "RELIEVE"]);
  });

  it("migrates legacy preferences to a stable id and fails closed safely", () => {
    const image = makeMapLayer({
      layerId: 10,
      role: "base",
      title: "IMAGEN",
      visible: true,
      zIndex: 1,
    });
    const map = makeMapLayer({
      layerId: 11,
      role: "base",
      title: "MAPA",
      visible: false,
      zIndex: 2,
    });
    const relief = makeMapLayer({
      layerId: 12,
      role: "base",
      title: "RELIEVE",
      visible: false,
      zIndex: 3,
    });
    const shuffled = [relief, image, map];

    expect(
      resolveLocalBaseMapLayerId(
        shuffled,
        parseStoredMapBaseLayerPreference({ baseLayer: "street" }),
      ),
    ).toBe(image.layerId);
    expect(
      resolveLocalBaseMapLayerId(
        shuffled,
        parseStoredMapBaseLayerPreference({ baseLayer: "topographic" }),
      ),
    ).toBe(map.layerId);
    expect(
      resolveLocalBaseMapLayerId(
        shuffled,
        parseStoredMapBaseLayerPreference({ baseLayerId: relief.layerId }),
      ),
    ).toBe(relief.layerId);
    expect(resolveLocalBaseMapLayerId(shuffled, 999)).toBe(image.layerId);
    expect(resolveLocalBaseMapLayerId(shuffled, null)).toBeNull();
    expect(
      parseStoredMapBaseLayerPreference({
        baseLayerId: "11",
        baseLayer: "topographic",
      }),
    ).toBeUndefined();
    expect(
      serializeMapBaseLayerPreference("topographic", map.layerId, false),
    ).toEqual({ baseLayer: "topographic" });
    expect(
      serializeMapBaseLayerPreference("topographic", map.layerId, true),
    ).toEqual({ baseLayerChoice: { id: map.layerId } });
  });

  it("no guarda «sin fondo» cuando no había ningún fondo que elegir", () => {
    // Es el fallo que dejaba el mapa vacío para siempre: antes de sembrar el
    // espejo no hay ningún mapa base, y el null resuelto se guardaba como si el
    // ayuntamiento hubiera elegido no tener fondo.
    expect(
      serializeMapBaseLayerPreference(undefined, null, true, false),
    ).toEqual({});
    expect(serializeMapBaseLayerPreference(null, null, false, false)).toEqual(
      {},
    );
  });

  it("sí guarda «sin fondo» cuando el usuario lo elige de verdad", () => {
    expect(serializeMapBaseLayerPreference(null, null, true, true)).toEqual({
      baseLayerChoice: { id: null },
    });
    expect(
      parseStoredMapBaseLayerPreference({ baseLayerChoice: { id: null } }),
    ).toBeNull();
  });

  it("un «sin fondo» del formato antiguo no condena el mapa", () => {
    // No se puede distinguir de la carencia que lo escribía, y esa carencia la
    // vivieron todos los navegadores que abrieron el mapa antes de la siembra.
    expect(
      parseStoredMapBaseLayerPreference({ baseLayerId: null }),
    ).toBeUndefined();
    // El formato nuevo sí manda, incluso conviviendo con el viejo.
    expect(
      parseStoredMapBaseLayerPreference({
        baseLayerId: null,
        baseLayerChoice: { id: 11 },
      }),
    ).toBe(11);
  });

  it("makes a selected base exclusive without changing overlay controls", () => {
    const bases = [10, 11, 12].map((layerId, index) =>
      makeMapLayer({
        layerId,
        role: "base",
        visible: layerId === 10,
        zIndex: index + 1,
      }),
    );
    const overlay = makeMapLayer({
      layerId: 20,
      role: "overlay",
      visible: true,
      zIndex: 4,
    });
    const preferences = {
      layers: Object.fromEntries(
        [...bases, overlay].map((layer) => [
          String(layer.layerId),
          {
            opacity: layer.opacity,
            styleId: layer.styleId,
            visible: layer.visible,
          },
        ]),
      ),
      stackOrder: [...bases, overlay].map((layer) => layer.layerId),
    };

    const selected = applyLocalBaseMapSelection(
      [overlay, ...bases],
      preferences,
      12,
    );
    expect(selected.layers["10"].visible).toBe(false);
    expect(selected.layers["11"].visible).toBe(false);
    expect(selected.layers["12"].visible).toBe(true);
    expect(selected.layers["20"].visible).toBe(true);

    const withoutBackground = applyLocalBaseMapSelection(
      [overlay, ...bases],
      selected,
      null,
    );
    expect(
      bases.every(
        (layer) =>
          withoutBackground.layers[String(layer.layerId)].visible === false,
      ),
    ).toBe(true);
    expect(withoutBackground.layers["20"].visible).toBe(true);
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
  it("normalizes the style preference but rejects a backend-declared incomplete delivery", () => {
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
    expect(referenceLayerBlocker(catalog, layer)).toBe("style_unsupported");
    expect(descriptors).toEqual([]);
  });

  it("builds only internal paths for complete active local style coverage", () => {
    const layer = makeLayer({
      id: 2,
      service_id: 8,
      available_style_ids: [12],
      effective_visible: true,
      effective_opacity: 0.65,
      source_substitution_attribution:
        "Obra derivada de PNOA 2020 CC-BY 4.0 scne.es",
      source_substitution_status: "exact",
      source_substitution_scope: "active_delivery",
      source_substitution_content_sha256: "a".repeat(64),
    });
    const catalog = makeCatalog(
      [layer],
      [makeStyle({ id: 12, is_default: true, title: "Verificado" })],
    );
    const preferences = reconcileSiurPreferences(catalog, {
      layers: {
        "2": { visible: true, opacity: 0.4, styleId: 12 },
      },
      stackOrder: [2],
    });

    const descriptors = buildSiurMapLayers(catalog, preferences);

    expect(descriptors).toHaveLength(1);
    expect(descriptors[0].tileUrl).toBe(
      "/api/organizations/7/reference-layers/2/tiles/{z}/{x}/{y}.png?style_id=12&version_id=21&generation=1",
    );
    expect(descriptors[0].attribution).toBe(
      "Obra derivada de PNOA 2020 CC-BY 4.0 scne.es",
    );
    expect(() => buildReferenceTileUrl(7, 2, -1)).toThrow(TypeError);
    expect(buildReferenceTileUrl(7, 2, 12, 21, 4)).toBe(
      "/api/organizations/7/reference-layers/2/tiles/{z}/{x}/{y}.png?style_id=12&version_id=21&generation=4",
    );
    expect(() => buildReferenceTileUrl(7, 2, 12, 21, null)).toThrow(
      TypeError,
    );
    expect(buildReferenceMetadataUrl(7, 2)).toBe(
      "/api/organizations/7/reference-layers/2/metadata.json",
    );
    expect(() => buildReferenceMetadataUrl(0, 2)).toThrow(TypeError);
  });

  it("rejects locally active layers with only partial style coverage", () => {
    const layer = makeLayer({
      id: 2,
      service_id: 8,
      available_style_ids: [11],
      delivery_available: true,
      delivery_blocker: "style_unsupported",
    });
    const catalog = makeCatalog(
      [layer],
      [
        makeStyle({ id: 11, is_default: true, title: "Principal" }),
        makeStyle({ id: 12, sort_order: 2, title: "Alternativo" }),
      ],
    );
    const preferences = reconcileSiurPreferences(catalog);

    expect(referenceLayerBlocker(catalog, layer)).toBe(
      "style_coverage_incomplete",
    );
    expect(buildSiurMapLayers(catalog, preferences)).toEqual([]);
  });

  it("does not turn an attested remote proxy into a map descriptor", () => {
    const layer = makeLayer({
      id: 2,
      service_id: 8,
      delivery_available: true,
      mirror_status: "legacy",
      active_version_id: null,
      active_generation: null,
    });
    const catalog = makeCatalog([layer]);
    const preferences = reconcileSiurPreferences(catalog);
    const descriptors = buildSiurMapLayers(catalog, preferences);

    expect(referenceLayerBlocker(catalog, layer)).toBe(
      "local_delivery_not_active",
    );
    expect(descriptors).toEqual([]);
    expect(selectTopIdentifyLayer(descriptors, 8, 42, -4)).toBeNull();
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
        "generation",
        "pixel_x",
        "pixel_y",
        "style_id",
        "version_id",
        "x",
        "y",
        "z",
      ].sort(),
    );
    expect(query.get("version_id")).toBe("1");
    expect(query.get("generation")).toBe("1");
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

describe("defaultLocalBaseMapSelection", () => {
  it("devuelve el fondo por omisión y lo entrega visible", () => {
    // El caso que importa: el catálogo trae el fondo apagado. Una pantalla sin
    // controles de capas no tiene forma de encenderlo, así que si se entregara
    // tal cual el mapa saldría vacío, que es justo lo que pasaba.
    const base = makeLayer({
      id: 4,
      service_id: 8,
      source_key: "layer:fondo",
      title: "MAPA",
      role: "base",
      effective_visible: false,
      active_version_id: 21,
      active_generation: 1,
    });
    const overlay = makeLayer({
      id: 5,
      service_id: 8,
      source_key: "layer:encima",
      title: "Montes",
      role: "overlay",
      effective_visible: true,
      active_version_id: 22,
      active_generation: 1,
    });

    const selection = defaultLocalBaseMapSelection(makeCatalog([base, overlay]));

    expect(selection.baseLayerId).toBe(4);
    expect(selection.layers.map((layer) => layer.layerId)).toEqual([4]);
    expect(selection.layers[0].visible).toBe(true);
  });

  it("sin fondo local no inventa ninguno", () => {
    const overlay = makeLayer({
      id: 5,
      service_id: 8,
      source_key: "layer:encima",
      role: "overlay",
      active_version_id: 22,
      active_generation: 1,
    });

    expect(defaultLocalBaseMapSelection(makeCatalog([overlay]))).toEqual({
      layers: [],
      baseLayerId: null,
    });
  });
});
