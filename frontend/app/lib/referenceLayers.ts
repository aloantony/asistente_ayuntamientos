import { adminRequest, API_BASE_URL } from "./api";

const SIUR_PROVIDER_KEY = "siur";
const TILE_SIZE = 256;
const LOCALLY_SERVING_MIRROR_STATUSES = new Set([
  "active",
  "serving_previous",
  "syncing",
]);

export type ReferenceCatalogSnapshot = {
  id: number;
  provider_key: string;
  content_sha256: string;
  definition_sha256: string;
  retrieved_at: string;
  service_count: number;
  group_count: number;
  layer_count: number;
  unresolved_count: number;
  status: string;
  is_current: boolean;
};

export type ReferenceService = {
  id: number;
  title: string;
  upstream_protocol: string;
  attribution: string | null;
  status: string;
  updated_at: string;
};

export type ReferenceLayer = {
  id: number;
  service_id: number | null;
  parent_id: number | null;
  source_key: string;
  node_type: string;
  title: string;
  description: string | null;
  role: string | null;
  renderer: string | null;
  delivery_mode: string | null;
  bounds_json: Record<string, unknown> | null;
  sort_order: number;
  default_visible: boolean;
  default_opacity: number;
  effective_visible: boolean;
  effective_opacity: number;
  min_zoom: number | null;
  max_zoom: number | null;
  downloadable: boolean;
  delivery_available: boolean;
  identify_available: boolean;
  delivery_blocker: string | null;
  available_style_ids: number[];
  legend_available: boolean;
  metadata_available: boolean;
  source_substitution_status:
    | "exact"
    | "substitute_degraded"
    | "blocked"
    | "invalid"
    | null;
  source_substitution_notice: string | null;
  source_substitution_selected_layer: string | null;
  source_substitution_profile: string | null;
  source_substitution_scope: "candidate" | "active_delivery" | null;
  source_substitution_attribution: string | null;
  source_substitution_content_sha256: string | null;
  mirror_status:
    | "not_applicable"
    | "legacy"
    | "pending"
    | "syncing"
    | "active"
    | "serving_previous"
    | "error"
    | "disabled";
  active_version_id: number | null;
  active_generation: number | null;
  active_source_version: string | null;
  active_reference_at: string | null;
  active_created_at: string | null;
  last_run_status: string | null;
  last_checked_at: string | null;
  last_sync_error_code: string | null;
  last_sync_error_summary: string | null;
  next_check_at: string | null;
  status: string;
  updated_at: string;
};

export type ReferenceLayerStyle = {
  id: number;
  layer_id: number;
  title: string;
  description: string | null;
  sort_order: number;
  is_default: boolean;
  legend_available: boolean;
  status: string;
  updated_at: string;
};

export type ReferenceCatalog = {
  snapshot: ReferenceCatalogSnapshot;
  organization_id: number | null;
  services: ReferenceService[];
  layers: ReferenceLayer[];
  styles: ReferenceLayerStyle[];
};

export type ReferenceLayerBounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

export type ReferenceLayerTreeNode = {
  layer: ReferenceLayer;
  children: ReferenceLayerTreeNode[];
};

export type ReferenceLayerTree = {
  roots: ReferenceLayerTreeNode[];
  layersInCanonicalOrder: ReferenceLayer[];
  warnings: string[];
};

export type SiurLayerControl = {
  visible: boolean;
  opacity: number;
  styleId: number | null;
};

export type SiurMapPreferences = {
  layers: Record<string, SiurLayerControl>;
  stackOrder: number[];
};

export type SiurMapLayer = {
  organizationId: number;
  layerId: number;
  versionId: number;
  generation: number;
  role: string | null;
  title: string;
  tileUrl: string;
  styleId: number | null;
  attribution: string | null;
  bounds: ReferenceLayerBounds | null;
  minZoom: number | null;
  maxZoom: number | null;
  opacity: number;
  visible: boolean;
  identifyAvailable: boolean;
  zIndex: number;
};

export type LegacyMapBaseLayerPreference = "street" | "topographic";

export type StoredMapBaseLayerPreference =
  | number
  | null
  | LegacyMapBaseLayerPreference
  | undefined;

export type SiurIdentifyPoint = {
  layer: SiurMapLayer;
  z: number;
  x: number;
  y: number;
  pixelX: number;
  pixelY: number;
};

export type ReferenceIdentifyFeature = {
  type: "Feature";
  properties: Record<string, unknown>;
  geometry: Record<string, unknown> | null;
};

export type ReferenceIdentifyResult = {
  type: "FeatureCollection";
  features: ReferenceIdentifyFeature[];
};

export class ReferenceCatalogIntegrityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReferenceCatalogIntegrityError";
  }
}

function requirePositiveInteger(value: number, label: string) {
  if (!Number.isInteger(value) || value < 1) {
    throw new TypeError(`${label} must be a positive integer`);
  }
  return value;
}

function requireNonNegativeInteger(value: number, label: string) {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError(`${label} must be a non-negative integer`);
  }
  return value;
}

function referenceDeliveryQuery(
  styleId: number | null | undefined,
  versionId?: number | null,
  generation?: number | null,
) {
  if ((versionId == null) !== (generation == null)) {
    throw new TypeError("versionId and generation must be provided together");
  }
  const query = new URLSearchParams();
  if (styleId != null) {
    query.set("style_id", String(requirePositiveInteger(styleId, "styleId")));
  }
  if (versionId != null && generation != null) {
    query.set(
      "version_id",
      String(requirePositiveInteger(versionId, "versionId")),
    );
    query.set(
      "generation",
      String(requirePositiveInteger(generation, "generation")),
    );
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : "";
}

function compareCatalogOrder(
  left: Pick<ReferenceLayer, "sort_order" | "id">,
  right: Pick<ReferenceLayer, "sort_order" | "id">,
) {
  return left.sort_order - right.sort_order || left.id - right.id;
}

function clampOpacity(value: number, fallback: number) {
  const candidate = Number.isFinite(value) ? value : fallback;
  return Math.min(1, Math.max(0, candidate));
}

function uniqueIds<T extends { id: number }>(items: T[], label: string) {
  const ids = new Set<number>();
  for (const item of items) {
    if (!Number.isInteger(item.id) || item.id < 1 || ids.has(item.id)) {
      throw new ReferenceCatalogIntegrityError(
        `El catálogo SIUR contiene un ${label} duplicado o no válido.`,
      );
    }
    ids.add(item.id);
  }
  return ids;
}

export function validateReferenceCatalog(
  catalog: ReferenceCatalog,
  organizationId: number,
) {
  requirePositiveInteger(organizationId, "organizationId");
  if (
    catalog.snapshot.provider_key !== SIUR_PROVIDER_KEY ||
    !catalog.snapshot.is_current ||
    catalog.snapshot.status !== "applied"
  ) {
    throw new ReferenceCatalogIntegrityError(
      "La instantánea SIUR devuelta no es la instantánea aplicada actual.",
    );
  }
  if (catalog.organization_id !== organizationId) {
    throw new ReferenceCatalogIntegrityError(
      "El catálogo SIUR no corresponde a la organización solicitada.",
    );
  }

  const serviceIds = uniqueIds(catalog.services, "servicio");
  uniqueIds(catalog.layers, "nodo");
  const styleIds = uniqueIds(catalog.styles, "estilo");
  const groupCount = catalog.layers.filter(
    (layer) => layer.node_type === "group",
  ).length;
  const layerCount = catalog.layers.filter(
    (layer) => layer.node_type === "layer",
  ).length;
  if (
    catalog.snapshot.service_count !== catalog.services.length ||
    catalog.snapshot.group_count !== groupCount ||
    catalog.snapshot.layer_count !== layerCount ||
    groupCount + layerCount !== catalog.layers.length
  ) {
    throw new ReferenceCatalogIntegrityError(
      "Los recuentos de la instantánea SIUR no coinciden con el catálogo recibido.",
    );
  }

  for (const layer of catalog.layers) {
    const availableStyleIds = new Set<number>();
    const hasSubstitution = layer.source_substitution_status !== null;
    if (
      hasSubstitution !== (layer.source_substitution_scope !== null) ||
      (layer.source_substitution_scope === "candidate" &&
        layer.source_substitution_content_sha256 !== null) ||
      (layer.source_substitution_scope === "active_delivery" &&
        (layer.active_version_id === null ||
          layer.source_substitution_content_sha256 === null ||
          !/^[0-9a-f]{64}$/.test(
            layer.source_substitution_content_sha256,
          ))) ||
      ((layer.source_substitution_status === "exact" ||
        layer.source_substitution_status === "substitute_degraded") &&
        !layer.source_substitution_attribution)
    ) {
      throw new ReferenceCatalogIntegrityError(
        "La clasificación de la sustitución no corresponde a los bytes anunciados.",
      );
    }
    if (
      (layer.node_type === "group" && layer.mirror_status !== "not_applicable") ||
      (layer.node_type === "layer" && layer.mirror_status === "not_applicable")
    ) {
      throw new ReferenceCatalogIntegrityError(
        "El estado del espejo local no corresponde al tipo de nodo SIUR.",
      );
    }
    if (
      (layer.active_version_id === null) !==
        (layer.active_generation === null) ||
      (layer.active_version_id !== null &&
        (!Number.isInteger(layer.active_version_id) ||
          layer.active_version_id < 1 ||
          !Number.isInteger(layer.active_generation) ||
          (layer.active_generation ?? 0) < 1))
    ) {
      throw new ReferenceCatalogIntegrityError(
        "La versión activa del espejo local no es coherente.",
      );
    }
    if (layer.service_id !== null && !serviceIds.has(layer.service_id)) {
      throw new ReferenceCatalogIntegrityError(
        "Una capa SIUR referencia un servicio ausente.",
      );
    }
    for (const styleId of layer.available_style_ids) {
      if (availableStyleIds.has(styleId) || !styleIds.has(styleId)) {
        throw new ReferenceCatalogIntegrityError(
          "Una capa SIUR anuncia un estilo interno ausente o duplicado.",
        );
      }
      availableStyleIds.add(styleId);
    }
  }
  const layersById = new Map(catalog.layers.map((layer) => [layer.id, layer]));
  for (const style of catalog.styles) {
    const layer = layersById.get(style.layer_id);
    if (!layer || layer.node_type !== "layer") {
      throw new ReferenceCatalogIntegrityError(
        "Un estilo SIUR referencia una capa ausente.",
      );
    }
  }
  for (const layer of catalog.layers) {
    for (const styleId of layer.available_style_ids) {
      if (
        !catalog.styles.some(
          (style) => style.id === styleId && style.layer_id === layer.id,
        )
      ) {
        throw new ReferenceCatalogIntegrityError(
          "Una capa SIUR anuncia un estilo de otra capa.",
        );
      }
    }
  }
}

export async function fetchReferenceCatalog(
  organizationId: number,
  accessToken: string,
  signal?: AbortSignal,
) {
  const safeOrganizationId = requirePositiveInteger(
    organizationId,
    "organizationId",
  );
  const catalog = await adminRequest<ReferenceCatalog>(
    `/reference-layers/catalog?provider_key=${SIUR_PROVIDER_KEY}&organization_id=${safeOrganizationId}`,
    accessToken,
    "No se pudo cargar la cartografía SIUR.",
    { signal },
  );
  validateReferenceCatalog(catalog, safeOrganizationId);
  return catalog;
}

export function buildReferenceTileUrl(
  organizationId: number,
  layerId: number,
  styleId?: number | null,
  versionId?: number | null,
  generation?: number | null,
) {
  return (
    `${API_BASE_URL}/organizations/${requirePositiveInteger(organizationId, "organizationId")}` +
    `/reference-layers/${requirePositiveInteger(layerId, "layerId")}` +
    `/tiles/{z}/{x}/{y}.png${referenceDeliveryQuery(
      styleId,
      versionId,
      generation,
    )}`
  );
}

export function buildReferenceLegendUrl(
  organizationId: number,
  layerId: number,
  styleId?: number | null,
  versionId?: number | null,
  generation?: number | null,
) {
  return (
    `${API_BASE_URL}/organizations/${requirePositiveInteger(organizationId, "organizationId")}` +
    `/reference-layers/${requirePositiveInteger(layerId, "layerId")}` +
    `/legend.png${referenceDeliveryQuery(styleId, versionId, generation)}`
  );
}

export function buildReferenceMetadataUrl(
  organizationId: number,
  layerId: number,
) {
  return (
    `${API_BASE_URL}/organizations/${requirePositiveInteger(organizationId, "organizationId")}` +
    `/reference-layers/${requirePositiveInteger(layerId, "layerId")}` +
    "/metadata.json"
  );
}

export function buildReferenceIdentifyPath(point: SiurIdentifyPoint) {
  const { layer } = point;
  const query = new URLSearchParams({
    z: String(requireNonNegativeInteger(point.z, "z")),
    x: String(requireNonNegativeInteger(point.x, "x")),
    y: String(requireNonNegativeInteger(point.y, "y")),
    pixel_x: String(requireNonNegativeInteger(point.pixelX, "pixelX")),
    pixel_y: String(requireNonNegativeInteger(point.pixelY, "pixelY")),
    feature_count: "5",
  });
  if (point.pixelX > 255 || point.pixelY > 255) {
    throw new TypeError("identify pixels must be inside a 256 pixel tile");
  }
  if (layer.styleId !== null) {
    query.set(
      "style_id",
      String(requirePositiveInteger(layer.styleId, "styleId")),
    );
  }
  query.set(
    "version_id",
    String(requirePositiveInteger(layer.versionId, "versionId")),
  );
  query.set(
    "generation",
    String(requirePositiveInteger(layer.generation, "generation")),
  );
  return (
    `/organizations/${requirePositiveInteger(layer.organizationId, "organizationId")}` +
    `/reference-layers/${requirePositiveInteger(layer.layerId, "layerId")}` +
    `/identify?${query.toString()}`
  );
}

export async function fetchReferenceIdentify(
  point: SiurIdentifyPoint,
  accessToken: string,
  signal?: AbortSignal,
) {
  const payload = await adminRequest<ReferenceIdentifyResult>(
    buildReferenceIdentifyPath(point),
    accessToken,
    "No se pudo consultar la capa SIUR.",
    { signal },
  );
  if (
    payload.type !== "FeatureCollection" ||
    !Array.isArray(payload.features) ||
    payload.features.length > 5 ||
    payload.features.some(
      (feature) =>
        feature?.type !== "Feature" ||
        feature.properties === null ||
        typeof feature.properties !== "object" ||
        Array.isArray(feature.properties),
    )
  ) {
    throw new ReferenceCatalogIntegrityError(
      "La respuesta de consulta SIUR no tiene el formato esperado.",
    );
  }
  return payload;
}

export function buildReferenceLayerTree(
  layers: ReferenceLayer[],
): ReferenceLayerTree {
  const sortedLayers = [...layers].sort(compareCatalogOrder);
  const layersById = new Map(sortedLayers.map((layer) => [layer.id, layer]));
  const parentById = new Map<number, number | null>();
  const warnings: string[] = [];

  for (const layer of sortedLayers) {
    const parent =
      layer.parent_id === null ? null : layersById.get(layer.parent_id);
    if (layer.parent_id === layer.id) {
      warnings.push(`El nodo «${layer.title}» se ha separado de sí mismo.`);
      parentById.set(layer.id, null);
    } else if (layer.parent_id !== null && !parent) {
      warnings.push(`El nodo «${layer.title}» no tiene su grupo padre.`);
      parentById.set(layer.id, null);
    } else if (parent && parent.node_type !== "group") {
      warnings.push(`El nodo «${layer.title}» tiene un padre que no es grupo.`);
      parentById.set(layer.id, null);
    } else {
      parentById.set(layer.id, parent?.id ?? null);
    }
  }

  for (const layer of sortedLayers) {
    const path: number[] = [];
    const positions = new Map<number, number>();
    let cursor: number | null = layer.id;
    while (cursor !== null) {
      const cycleStart = positions.get(cursor);
      if (cycleStart !== undefined) {
        const cycleIds = path.slice(cycleStart);
        const detachedId = Math.min(...cycleIds);
        parentById.set(detachedId, null);
        const detached = layersById.get(detachedId);
        warnings.push(
          `Se ha interrumpido un ciclo de jerarquía en «${detached?.title ?? detachedId}».`,
        );
        break;
      }
      positions.set(cursor, path.length);
      path.push(cursor);
      cursor = parentById.get(cursor) ?? null;
    }
  }

  const childrenByParent = new Map<number | null, ReferenceLayer[]>();
  for (const layer of sortedLayers) {
    const parentId = parentById.get(layer.id) ?? null;
    const children = childrenByParent.get(parentId) ?? [];
    children.push(layer);
    childrenByParent.set(parentId, children);
  }
  for (const children of childrenByParent.values()) {
    children.sort(compareCatalogOrder);
  }

  const layersInCanonicalOrder: ReferenceLayer[] = [];
  const toNode = (layer: ReferenceLayer): ReferenceLayerTreeNode => {
    layersInCanonicalOrder.push(layer);
    return {
      layer,
      children: (childrenByParent.get(layer.id) ?? []).map(toNode),
    };
  };
  const roots = (childrenByParent.get(null) ?? []).map(toNode);
  return { roots, layersInCanonicalOrder, warnings };
}

export function availableStylesForLayer(
  catalog: ReferenceCatalog,
  layer: ReferenceLayer,
) {
  const availableIds = new Set(layer.available_style_ids);
  return catalog.styles
    .filter(
      (style) =>
        style.layer_id === layer.id &&
        availableIds.has(style.id) &&
        (style.status === "active" || style.status === "degraded"),
    )
    .sort(
      (left, right) => left.sort_order - right.sort_order || left.id - right.id,
    );
}

export function preferredStyleId(
  catalog: ReferenceCatalog,
  layer: ReferenceLayer,
) {
  const styles = availableStylesForLayer(catalog, layer);
  return styles.find((style) => style.is_default)?.id ?? styles[0]?.id ?? null;
}

export function parseReferenceLayerBounds(
  value: Record<string, unknown> | null,
): ReferenceLayerBounds | null | undefined {
  if (value === null) {
    return null;
  }
  const entries = [value.west, value.south, value.east, value.north];
  if (
    entries.some(
      (entry) => typeof entry !== "number" || !Number.isFinite(entry),
    )
  ) {
    return undefined;
  }
  const [west, south, east, north] = entries as number[];
  if (
    west < -180 ||
    east > 180 ||
    south < -90 ||
    north > 90 ||
    west >= east ||
    south >= north
  ) {
    return undefined;
  }
  return { west, south, east, north };
}

export function referenceLayerBlocker(
  catalog: ReferenceCatalog,
  layer: ReferenceLayer,
) {
  if (parseReferenceLayerBounds(layer.bounds_json) === undefined) {
    return "bounds_invalid";
  }
  if (layer.node_type !== "layer") {
    return null;
  }
  if (layer.status !== "active" && layer.status !== "degraded") {
    return layer.status;
  }
  if (!layer.delivery_available) {
    return layer.delivery_blocker ?? "not_deliverable";
  }
  if (
    layer.active_version_id === null ||
    layer.active_generation === null ||
    !LOCALLY_SERVING_MIRROR_STATUSES.has(layer.mirror_status)
  ) {
    return "local_delivery_not_active";
  }
  const availableStyleIds = new Set(layer.available_style_ids);
  const hasIncompleteStyleCoverage = catalog.styles.some(
    (style) =>
      style.layer_id === layer.id &&
      (style.status === "active" || style.status === "degraded") &&
      !availableStyleIds.has(style.id),
  );
  if (hasIncompleteStyleCoverage) {
    return "style_coverage_incomplete";
  }
  if (layer.delivery_blocker) {
    return layer.delivery_blocker;
  }
  return null;
}

export function reconcileSiurPreferences(
  catalog: ReferenceCatalog,
  previous?: Partial<SiurMapPreferences> | null,
): SiurMapPreferences {
  const tree = buildReferenceLayerTree(catalog.layers);
  const leafLayers = tree.layersInCanonicalOrder.filter(
    (layer) => layer.node_type === "layer",
  );
  const leafIds = new Set(leafLayers.map((layer) => layer.id));
  const previousStackOrder = Array.isArray(previous?.stackOrder)
    ? previous.stackOrder.filter(
        (value): value is number => Number.isInteger(value),
      )
    : [];
  const previousLayers =
    previous?.layers &&
    typeof previous.layers === "object" &&
    !Array.isArray(previous.layers)
      ? previous.layers
      : {};
  const stackOrder = previousStackOrder.filter(
    (id, index, values) => leafIds.has(id) && values.indexOf(id) === index,
  );
  for (const layer of leafLayers) {
    if (!stackOrder.includes(layer.id)) {
      stackOrder.push(layer.id);
    }
  }

  const controls: Record<string, SiurLayerControl> = {};
  for (const layer of leafLayers) {
    const candidateControl = previousLayers[String(layer.id)];
    const previousControl =
      candidateControl &&
      typeof candidateControl === "object" &&
      typeof candidateControl.visible === "boolean" &&
      typeof candidateControl.opacity === "number"
        ? candidateControl
        : undefined;
    const allowedStyleIds = new Set(
      availableStylesForLayer(catalog, layer).map((style) => style.id),
    );
    controls[String(layer.id)] = {
      visible: previousControl?.visible ?? layer.effective_visible,
      opacity: clampOpacity(
        previousControl?.opacity ?? layer.effective_opacity,
        layer.default_opacity,
      ),
      styleId:
        previousControl?.styleId !== null &&
        previousControl?.styleId !== undefined &&
        allowedStyleIds.has(previousControl.styleId)
          ? previousControl.styleId
          : preferredStyleId(catalog, layer),
    };
  }
  return { layers: controls, stackOrder };
}

function escapeLeafletAttribution(value: string) {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character] ?? character,
  );
}

export function buildSiurMapLayers(
  catalog: ReferenceCatalog,
  preferences: SiurMapPreferences,
): SiurMapLayer[] {
  const organizationId = requirePositiveInteger(
    catalog.organization_id ?? 0,
    "organizationId",
  );
  const layersById = new Map(catalog.layers.map((layer) => [layer.id, layer]));
  const servicesById = new Map(
    catalog.services.map((service) => [service.id, service]),
  );
  const result: SiurMapLayer[] = [];

  preferences.stackOrder.forEach((layerId, index) => {
    const layer = layersById.get(layerId);
    if (!layer || referenceLayerBlocker(catalog, layer)) {
      return;
    }
    const control = preferences.layers[String(layer.id)];
    if (!control) {
      return;
    }
    const availableStyles = availableStylesForLayer(catalog, layer);
    const validStyleId = availableStyles.some(
      (style) => style.id === control.styleId,
    )
      ? control.styleId
      : null;
    if (availableStyles.length > 0 && validStyleId === null) {
      return;
    }
    const bounds = parseReferenceLayerBounds(layer.bounds_json);
    if (bounds === undefined) {
      return;
    }
    const attribution =
      (layer.source_substitution_scope === "active_delivery"
        ? layer.source_substitution_attribution
        : null) ??
      (layer.service_id === null
        ? null
        : (servicesById.get(layer.service_id)?.attribution ?? null));
    result.push({
      organizationId,
      layerId: layer.id,
      versionId: requirePositiveInteger(
        layer.active_version_id ?? 0,
        "versionId",
      ),
      generation: requirePositiveInteger(
        layer.active_generation ?? 0,
        "generation",
      ),
      role: layer.role,
      title: layer.title,
      tileUrl: buildReferenceTileUrl(
        organizationId,
        layer.id,
        validStyleId,
        layer.active_version_id,
        layer.active_generation,
      ),
      styleId: validStyleId,
      attribution: attribution ? escapeLeafletAttribution(attribution) : null,
      bounds,
      minZoom: layer.min_zoom,
      maxZoom: layer.max_zoom,
      opacity: clampOpacity(control.opacity, layer.default_opacity),
      visible: control.visible,
      identifyAvailable: layer.identify_available,
      zIndex: index + 1,
    });
  });
  return result;
}

function isLocalReferenceTileLayer(layer: SiurMapLayer) {
  try {
    return (
      layer.tileUrl ===
      buildReferenceTileUrl(
        layer.organizationId,
        layer.layerId,
        layer.styleId,
        layer.versionId,
        layer.generation,
      )
    );
  } catch {
    return false;
  }
}

export function listLocalBaseMapLayers(layers: SiurMapLayer[]) {
  return layers
    .filter(
      (layer) =>
        layer.role === "base" &&
        layer.opacity > 0 &&
        isLocalReferenceTileLayer(layer),
    )
    .sort(
      (left, right) =>
        left.zIndex - right.zIndex || left.layerId - right.layerId,
    );
}

export function parseStoredMapBaseLayerPreference(
  preferences: unknown,
): StoredMapBaseLayerPreference {
  if (
    !preferences ||
    typeof preferences !== "object" ||
    Array.isArray(preferences)
  ) {
    return undefined;
  }
  const record = preferences as Record<string, unknown>;
  if (Object.prototype.hasOwnProperty.call(record, "baseLayerId")) {
    if (record.baseLayerId === null) {
      return null;
    }
    return Number.isInteger(record.baseLayerId) &&
      (record.baseLayerId as number) > 0
      ? (record.baseLayerId as number)
      : undefined;
  }
  return record.baseLayer === "street" || record.baseLayer === "topographic"
    ? record.baseLayer
    : undefined;
}

export function resolveLocalBaseMapLayerId(
  layers: SiurMapLayer[],
  preference: StoredMapBaseLayerPreference,
) {
  const candidates = listLocalBaseMapLayers(layers);
  const defaultCandidate =
    candidates.find((layer) => layer.visible) ?? candidates[0] ?? null;
  if (preference === null) {
    return null;
  }
  if (typeof preference === "number") {
    return candidates.some((layer) => layer.layerId === preference)
      ? preference
      : defaultCandidate?.layerId ?? null;
  }
  if (preference === "street" || preference === "topographic") {
    const legacyIndex = preference === "topographic" ? 1 : 0;
    return candidates[legacyIndex]?.layerId ?? defaultCandidate?.layerId ?? null;
  }
  return defaultCandidate?.layerId ?? null;
}

export function serializeMapBaseLayerPreference(
  preference: StoredMapBaseLayerPreference,
  resolvedLayerId: number | null,
  resolved: boolean,
): {
  baseLayerId?: number | null;
  baseLayer?: LegacyMapBaseLayerPreference;
} {
  if (resolved) {
    return { baseLayerId: resolvedLayerId };
  }
  if (preference === "street" || preference === "topographic") {
    return { baseLayer: preference };
  }
  if (preference === null || typeof preference === "number") {
    return { baseLayerId: preference };
  }
  return {};
}

export function applyLocalBaseMapSelection(
  layers: SiurMapLayer[],
  preferences: SiurMapPreferences,
  selectedLayerId: number | null,
) {
  const candidates = listLocalBaseMapLayers(layers);
  const candidateIds = new Set(candidates.map((layer) => layer.layerId));
  const validSelectedLayerId =
    selectedLayerId !== null && candidateIds.has(selectedLayerId)
      ? selectedLayerId
      : null;
  let changed = false;
  const nextLayers = { ...preferences.layers };
  for (const layerId of candidateIds) {
    const key = String(layerId);
    const control = preferences.layers[key];
    if (!control) {
      continue;
    }
    const visible = layerId === validSelectedLayerId;
    if (control.visible !== visible) {
      changed = true;
      nextLayers[key] = { ...control, visible };
    }
  }
  return changed ? { ...preferences, layers: nextLayers } : preferences;
}

export function selectLocalBaseMapLayer(
  layers: SiurMapLayer[],
  selectedLayerId: number | null,
) {
  if (selectedLayerId === null) {
    return null;
  }
  return (
    listLocalBaseMapLayers(layers).find(
      (layer) => layer.layerId === selectedLayerId && layer.visible,
    ) ?? null
  );
}

/**
 * El fondo por omisión para un mapa que no tiene controles de capas propios.
 *
 * El panel de `/mapa` deja elegir y recordar el mapa base; las pantallas que
 * sólo necesitan algo debajo de sus marcadores no, y no pasarles nada es lo que
 * dejaba el mapa del Ayuntamiento sobre una cuadrícula vacía. Aquí se resuelve
 * el fondo que el propio catálogo trae por omisión y se fuerza su visibilidad:
 * un fondo predeterminado que llega oculto no pinta nada, y el visor sólo sabe
 * dibujar bases locales.
 */
export function defaultLocalBaseMapSelection(catalog: ReferenceCatalog): {
  layers: SiurMapLayer[];
  baseLayerId: number | null;
} {
  const preferences = reconcileSiurPreferences(catalog, null);
  const candidates = buildSiurMapLayers(catalog, preferences);
  const baseLayerId = resolveLocalBaseMapLayerId(candidates, undefined);
  if (baseLayerId === null) {
    return { layers: [], baseLayerId: null };
  }
  const selected = applyLocalBaseMapSelection(
    candidates,
    preferences,
    baseLayerId,
  );
  return {
    layers: listLocalBaseMapLayers(buildSiurMapLayers(catalog, selected)),
    baseLayerId,
  };
}

export function selectTopIdentifyLayer(
  layers: SiurMapLayer[],
  zoom: number,
  latitude: number,
  longitude: number,
) {
  for (let index = layers.length - 1; index >= 0; index -= 1) {
    const layer = layers[index];
    if (
      !layer.visible ||
      layer.opacity <= 0 ||
      !layer.identifyAvailable ||
      (layer.minZoom !== null && zoom < layer.minZoom) ||
      (layer.maxZoom !== null && zoom > layer.maxZoom)
    ) {
      continue;
    }
    if (
      layer.bounds &&
      (longitude < layer.bounds.west ||
        longitude > layer.bounds.east ||
        latitude < layer.bounds.south ||
        latitude > layer.bounds.north)
    ) {
      continue;
    }
    return layer;
  }
  return null;
}

export function tileCoordinatesForProjectedPoint(
  projectedX: number,
  projectedY: number,
  zoom: number,
) {
  requireNonNegativeInteger(zoom, "zoom");
  if (!Number.isFinite(projectedX) || !Number.isFinite(projectedY)) {
    throw new TypeError("projected coordinates must be finite");
  }
  const maximumTile = 2 ** zoom - 1;
  const rawX = Math.floor(projectedX / TILE_SIZE);
  const rawY = Math.floor(projectedY / TILE_SIZE);
  const x = Math.min(maximumTile, Math.max(0, rawX));
  const y = Math.min(maximumTile, Math.max(0, rawY));
  const pixelX = Math.min(
    TILE_SIZE - 1,
    Math.max(0, Math.floor(projectedX - rawX * TILE_SIZE)),
  );
  const pixelY = Math.min(
    TILE_SIZE - 1,
    Math.max(0, Math.floor(projectedY - rawY * TILE_SIZE)),
  );
  return { x, y, pixelX, pixelY };
}
