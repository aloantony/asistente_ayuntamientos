"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
} from "react";
import { adminRequest, ApiRequestError } from "../lib/api";
import {
  createEntityLocation,
  fetchAllGeoMapItems,
  fetchMunicipalAssets,
} from "../lib/geo";
import {
  buildReferenceLayerTree,
  buildSiurMapLayers,
  fetchReferenceCatalog,
  fetchReferenceIdentify,
  reconcileSiurPreferences,
  type ReferenceCatalog,
  type ReferenceIdentifyFeature,
  type SiurIdentifyPoint,
  type SiurLayerControl,
  type SiurMapPreferences,
} from "../lib/referenceLayers";
import { useSession } from "../lib/session";
import { AssetMaintenancePanel } from "./AssetMaintenancePanel";
import {
  MunicipalityMapDirectory,
  type MunicipalityMapFocus,
} from "./MunicipalityMapDirectory";
import {
  MunicipalMap,
  type MapBaseLayer,
  type MapBounds,
} from "./MunicipalMap";
import { SiurLayerTree } from "./SiurLayerTree";
import type {
  AssetStatus,
  GeoEntityType,
  GeoMapItem,
  MunicipalAsset,
  Project,
  Requirement,
  User,
} from "./types";
import { userHasPermission } from "./types";

type MapPanelProps = {
  user: User;
};

type CreatableMapEntityType = "requirement" | "project";
type MapView = "territory" | "municipalities";

type MapLayer = {
  key: string;
  label: string;
  entityType: GeoEntityType;
  color: string;
  group: "work" | "assets";
  count: number;
};

type LayeredGeoMapItem = GeoMapItem & {
  layer_key?: string;
  layer_label?: string;
  layer_color?: string | null;
  item_type?: string | null;
  condition_status?: string | null;
};

const MAP_PREFERENCES_KEY = "municipal-map-preferences-v1";
const SIUR_PREFERENCES_PREFIX = "siur-map-preferences-v1";
const MUNICIPAL_CAPITAL_ZOOM = 14;
const MAP_LAYER_COLORS: Record<GeoEntityType, string> = {
  requirement: "#c0603a",
  project: "#2f74d0",
  asset: "#3caf8c",
};
const STATUS_COLORS = [
  "#3caf8c",
  "#d9a520",
  "#c0603a",
  "#2f74d0",
  "#8a5cd1",
  "#17b3c4",
  "#c0568f",
] as const;

type MapContextMenu = {
  latitude: number;
  longitude: number;
  zoom: number;
  x: number;
  y: number;
  nearestItem: GeoMapItem | null;
  nearestDistanceMeters: number | null;
};

type MapRegistrationDraft = {
  kind: CreatableMapEntityType;
  latitude: number;
  longitude: number;
  organizationId: string;
  title: string;
  description: string;
};

type AssetLocationDraft = {
  latitude: number;
  longitude: number;
  organizationId: string;
  assetId: string;
  label: string;
  address: string;
};

type SiurIdentifyState = {
  layerId: number;
  layerTitle: string;
  features: ReferenceIdentifyFeature[];
  isLoading: boolean;
  error: string;
};

function getItemKey(item: GeoMapItem) {
  return `${item.entity_type}-${item.entity_id}-${item.role}`;
}

function entityTypeLabel(type: GeoEntityType) {
  if (type === "requirement") {
    return "Necesidad";
  }
  if (type === "project") {
    return "Proyecto";
  }
  if (type === "asset") {
    return "Activo municipal";
  }
  return "Elemento municipal";
}

function entityDetailLabel(type: GeoEntityType) {
  if (type === "requirement") {
    return "necesidad";
  }
  if (type === "project") {
    return "proyecto";
  }
  if (type === "asset") {
    return "activo";
  }
  return "elemento";
}

function formatStatus(value: string) {
  return value.replace(/_/g, " ");
}

function normalizeAssetStatus(value: string): AssetStatus {
  if (
    value === "active" ||
    value === "inactive" ||
    value === "retired" ||
    value === "archived"
  ) {
    return value;
  }
  return "archived";
}

function locationRoleLabel(role: GeoMapItem["role"]) {
  if (role === "primary") {
    return "Principal";
  }
  if (role === "affected_area") {
    return "Área afectada";
  }
  return "Referencia";
}

function formatCoordinates(latitude: number, longitude: number) {
  return `${latitude.toFixed(6)}, ${longitude.toFixed(6)}`;
}

function formatDistance(meters: number) {
  if (meters >= 1000) {
    return `${(meters / 1000).toFixed(1)} km`;
  }
  return `${Math.round(meters)} m`;
}

function distanceInMeters(
  latitudeA: number,
  longitudeA: number,
  latitudeB: number,
  longitudeB: number,
) {
  const earthRadiusMeters = 6_371_000;
  const toRadians = (value: number) => (value * Math.PI) / 180;
  const deltaLatitude = toRadians(latitudeB - latitudeA);
  const deltaLongitude = toRadians(longitudeB - longitudeA);
  const a =
    Math.sin(deltaLatitude / 2) * Math.sin(deltaLatitude / 2) +
    Math.cos(toRadians(latitudeA)) *
      Math.cos(toRadians(latitudeB)) *
      Math.sin(deltaLongitude / 2) *
      Math.sin(deltaLongitude / 2);

  return earthRadiusMeters * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function normalizeDetailPath(item: GeoMapItem) {
  if (item.entity_type === "requirement") {
    return `/requisitos?id=${item.entity_id}`;
  }
  if (item.entity_type === "project") {
    return item.detail_path || "/proyectos";
  }
  if (item.entity_type === "asset") {
    return (
      item.detail_path || `/mapa?entity_type=asset&entity_id=${item.entity_id}`
    );
  }
  return "/mapa";
}

function parseNumberParam(value: string | null) {
  if (value === null || value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseEntityTypeParam(value: string | null): GeoEntityType | null {
  return value === "requirement" || value === "project" || value === "asset"
    ? value
    : null;
}

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

function defaultOrganizationId(user: User) {
  return user.organizations?.[0]?.id ? String(user.organizations[0].id) : "";
}

function normalizeSearchText(value: string | null | undefined) {
  return (value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("es")
    .trim();
}

function safeLayerColor(value: string | null | undefined, fallback: string) {
  return value && /^#[0-9a-f]{6}$/i.test(value) ? value : fallback;
}

function mapItemLayerKey(item: GeoMapItem) {
  const layeredItem = item as LayeredGeoMapItem;
  if (layeredItem.layer_key) {
    return layeredItem.layer_key;
  }
  if (item.entity_type === "requirement") {
    return "requirements";
  }
  if (item.entity_type === "project") {
    return "projects";
  }
  return "assets";
}

function mapItemLayerLabel(item: GeoMapItem) {
  const layeredItem = item as LayeredGeoMapItem;
  return layeredItem.layer_label || entityTypeLabel(item.entity_type);
}

function mapItemLayerColor(item: GeoMapItem) {
  const layeredItem = item as LayeredGeoMapItem;
  return safeLayerColor(
    layeredItem.layer_color,
    MAP_LAYER_COLORS[item.entity_type],
  );
}

function mapItemState(item: GeoMapItem) {
  const layeredItem = item as LayeredGeoMapItem;
  return layeredItem.condition_status || item.status;
}

function mapItemMatchesSearch(item: GeoMapItem, normalizedQuery: string) {
  if (!normalizedQuery) {
    return true;
  }
  const layeredItem = item as LayeredGeoMapItem;
  return normalizeSearchText(
    [
      item.title,
      item.subtitle,
      item.status,
      item.priority,
      item.organization_name,
      item.location.label,
      item.location.address_text,
      item.location.place_name,
      item.location.cadastral_reference,
      layeredItem.layer_label,
      layeredItem.item_type,
      layeredItem.condition_status,
    ]
      .filter(Boolean)
      .join(" "),
  ).includes(normalizedQuery);
}

function statusColor(status: string) {
  const normalized = normalizeSearchText(status);
  if (
    ["active", "accepted", "completed", "reviewed", "good"].includes(
      normalized,
    )
  ) {
    return "#3caf8c";
  }
  if (
    [
      "draft",
      "submitted",
      "in_review",
      "planned",
      "scheduled",
      "paused",
      "fair",
    ].includes(normalized)
  ) {
    return "#d9a520";
  }
  if (
    [
      "rejected",
      "archived",
      "inactive",
      "retired",
      "cancelled",
      "poor",
    ].includes(normalized)
  ) {
    return "#c0603a";
  }

  let hash = 0;
  for (const character of normalized) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return STATUS_COLORS[hash % STATUS_COLORS.length];
}

function itemIsInsideBounds(item: GeoMapItem, bounds: MapBounds) {
  const latitude = item.location.latitude;
  const longitude = item.location.longitude;
  return (
    typeof latitude === "number" &&
    typeof longitude === "number" &&
    latitude >= bounds.south &&
    latitude <= bounds.north &&
    longitude >= bounds.west &&
    longitude <= bounds.east
  );
}

function csvCell(value: string | number | null | undefined) {
  const serialized = value == null ? "" : String(value);
  const safeValue = /^[=+\-@]/.test(serialized)
    ? `'${serialized}`
    : serialized;
  return `"${safeValue.replace(/"/g, '""')}"`;
}

function downloadTextFile(contents: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([contents], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function siurPreferencesKey(organizationId: number) {
  return `${SIUR_PREFERENCES_PREFIX}:${organizationId}`;
}

function formatIdentifyProperty(value: unknown) {
  if (value === null) {
    return "—";
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return String(value).slice(0, 500);
  }
  try {
    return JSON.stringify(value).slice(0, 500);
  } catch {
    return "Valor no representable";
  }
}

export function MapPanel({ user }: MapPanelProps) {
  const canViewMap =
    userHasPermission(user, "map.view") || userHasPermission(user, "map.manage");
  const canViewMunicipalities =
    userHasPermission(user, "municipalities.view") ||
    userHasPermission(user, "municipalities.manage");
  const searchParams = useSearchParams();
  const { getStoredToken, handleRequestError } = useSession();
  const [activeView, setActiveView] = useState<MapView>(
    canViewMap ? "territory" : "municipalities",
  );
  const [includeArchived, setIncludeArchived] = useState(false);
  const [items, setItems] = useState<GeoMapItem[]>([]);
  const [selectedItem, setSelectedItem] = useState<GeoMapItem | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [visibleLayerKeys, setVisibleLayerKeys] = useState<
    Record<string, boolean>
  >({});
  const [layerOrder, setLayerOrder] = useState<string[]>([]);
  const [layerPanelOpen, setLayerPanelOpen] = useState(true);
  const [stateLayerEnabled, setStateLayerEnabled] = useState(false);
  const [visibleStatuses, setVisibleStatuses] = useState<
    Record<string, boolean>
  >({});
  const [baseLayer, setBaseLayer] = useState<MapBaseLayer>("street");
  const [fitRequest, setFitRequest] = useState(0);
  const [locateRequest, setLocateRequest] = useState(0);
  const [areaSelectionEnabled, setAreaSelectionEnabled] = useState(false);
  const [areaBounds, setAreaBounds] = useState<MapBounds | null>(null);
  const [preferencesReady, setPreferencesReady] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [siurCatalog, setSiurCatalog] = useState<ReferenceCatalog | null>(null);
  const [siurPreferences, setSiurPreferences] =
    useState<SiurMapPreferences | null>(null);
  const [siurCatalogError, setSiurCatalogError] = useState("");
  const [isLoadingSiurCatalog, setIsLoadingSiurCatalog] = useState(false);
  const [siurIdentify, setSiurIdentify] = useState<SiurIdentifyState | null>(
    null,
  );
  const [mapContextMenu, setMapContextMenu] = useState<MapContextMenu | null>(
    null,
  );
  const [manualFocusLocation, setManualFocusLocation] = useState<{
    latitude: number;
    longitude: number;
    label: string;
  } | null>(null);
  const [manualFocusZoom, setManualFocusZoom] = useState<number | null>(null);
  const [registrationDraft, setRegistrationDraft] =
    useState<MapRegistrationDraft | null>(null);
  const [registrationError, setRegistrationError] = useState("");
  const [isRegistering, setIsRegistering] = useState(false);
  const [assetLocationDraft, setAssetLocationDraft] =
    useState<AssetLocationDraft | null>(null);
  const [assetSearch, setAssetSearch] = useState("");
  const [assetOptions, setAssetOptions] = useState<MunicipalAsset[]>([]);
  const [assetTotal, setAssetTotal] = useState(0);
  const [assetReloadVersion, setAssetReloadVersion] = useState(0);
  const [assetLoadError, setAssetLoadError] = useState("");
  const [assetLocationError, setAssetLocationError] = useState("");
  const [isLoadingAssets, setIsLoadingAssets] = useState(false);
  const [isLocatingAsset, setIsLocatingAsset] = useState(false);
  const mapRequestSequenceRef = useRef(0);
  const assetRequestSequenceRef = useRef(0);
  const mapAbortControllerRef = useRef<AbortController | null>(null);
  const siurCatalogAbortControllerRef = useRef<AbortController | null>(null);
  const siurIdentifyAbortControllerRef = useRef<AbortController | null>(null);
  const assetAbortControllerRef = useRef<AbortController | null>(null);
  const assetSearchRef = useRef<HTMLInputElement | null>(null);
  const layerDragKeyRef = useRef<string | null>(null);
  const handleRequestErrorRef = useRef(handleRequestError);

  const canEditMap =
    userHasPermission(user, "map.edit") || userHasPermission(user, "map.manage");
  const canCreateRequirements =
    canEditMap && userHasPermission(user, "requirements.create");
  const canCreateProjects =
    canEditMap && userHasPermission(user, "projects.create");
  const canViewAssets =
    userHasPermission(user, "assets.view") ||
    userHasPermission(user, "assets.manage");
  const canLocateAssets =
    canEditMap &&
    (userHasPermission(user, "assets.manage") ||
      (userHasPermission(user, "assets.view") &&
        userHasPermission(user, "assets.edit")));
  const activeAssetOrganizations = useMemo(
    () =>
      (user.organizations ?? []).filter(
        (organization) =>
          organization.status === "active" &&
          typeof organization.municipality_id === "number",
      ),
    [user.organizations],
  );
  const selectedAsset = useMemo(
    () =>
      assetLocationDraft
        ? (assetOptions.find(
            (asset) => String(asset.id) === assetLocationDraft.assetId,
          ) ?? null)
        : null,
    [assetLocationDraft, assetOptions],
  );
  const requestedFocusedEntityType = parseEntityTypeParam(
    searchParams.get("entity_type"),
  );
  const focusedEntityType =
    requestedFocusedEntityType === "asset" && !canViewAssets
      ? null
      : requestedFocusedEntityType;
  const focusedOrganizationId = parseNumberParam(
    searchParams.get("organization_id"),
  );
  const siurOrganizationId =
    focusedOrganizationId !== null && focusedOrganizationId > 0
      ? focusedOrganizationId
      : (user.organizations?.find(
          (organization) => organization.status === "active",
        )?.id ??
        user.organizations?.[0]?.id ??
        null);
  const focusedEntityId = parseNumberParam(searchParams.get("entity_id"));
  const focusedLatitude = parseNumberParam(searchParams.get("lat"));
  const focusedLongitude = parseNumberParam(searchParams.get("lng"));
  const focusedZoom = parseNumberParam(searchParams.get("zoom"));
  const focusedItemKey =
    focusedEntityType && focusedEntityId !== null
      ? `${focusedEntityType}-${focusedEntityId}-primary`
      : null;
  const explicitFocusLocation =
    focusedLatitude !== null && focusedLongitude !== null
      ? {
          latitude: focusedLatitude,
          longitude: focusedLongitude,
          label: searchParams.get("label") || undefined,
        }
      : null;

  const queryParams = useMemo(() => {
    const params = new URLSearchParams();
    if (focusedEntityType && focusedEntityId !== null) {
      params.set("entity_type", focusedEntityType);
      params.set("entity_id", String(focusedEntityId));
    }
    if (focusedOrganizationId !== null) {
      params.set("organization_id", String(focusedOrganizationId));
    }
    if (includeArchived) {
      params.set("include_archived", "true");
    }
    return params;
  }, [
    focusedEntityId,
    focusedEntityType,
    focusedOrganizationId,
    includeArchived,
  ]);

  useEffect(() => {
    handleRequestErrorRef.current = handleRequestError;
  }, [handleRequestError]);

  useEffect(() => {
    if (!canViewMap) {
      mapAbortControllerRef.current?.abort();
      mapAbortControllerRef.current = null;
      mapRequestSequenceRef.current += 1;
      setIsLoading(false);
      return;
    }

    mapAbortControllerRef.current?.abort();
    const controller = new AbortController();
    mapAbortControllerRef.current = controller;
    const requestSequence = ++mapRequestSequenceRef.current;
    setIsLoading(true);
    setMessage("");

    fetchAllGeoMapItems(queryParams, controller.signal)
      .then((mapItems) => {
        if (
          controller.signal.aborted ||
          requestSequence !== mapRequestSequenceRef.current
        ) {
          return;
        }
        const visibleMapItems = canViewAssets
          ? mapItems
          : mapItems.filter((item) => item.entity_type !== "asset");
        setItems(visibleMapItems);
        setSelectedItem((current) => {
          if (focusedItemKey) {
            return (
              visibleMapItems.find(
                (item) => getItemKey(item) === focusedItemKey,
              ) ?? null
            );
          }
          if (!current) {
            return null;
          }
          return (
            visibleMapItems.find(
              (item) => getItemKey(item) === getItemKey(current),
            ) ?? null
          );
        });
      })
      .catch((error) => {
        if (
          controller.signal.aborted ||
          requestSequence !== mapRequestSequenceRef.current ||
          isAbortError(error)
        ) {
          return;
        }
        handleRequestErrorRef.current(
          error,
          setMessage,
          "No se pudieron cargar los elementos del mapa.",
        );
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          requestSequence === mapRequestSequenceRef.current
        ) {
          setIsLoading(false);
        }
      });

    return () => {
      controller.abort();
      if (mapAbortControllerRef.current === controller) {
        mapAbortControllerRef.current = null;
      }
    };
  }, [canViewAssets, canViewMap, focusedItemKey, queryParams]);

  useEffect(() => {
    siurCatalogAbortControllerRef.current?.abort();
    siurIdentifyAbortControllerRef.current?.abort();
    siurIdentifyAbortControllerRef.current = null;
    setSiurIdentify(null);

    if (!canViewMap || siurOrganizationId === null) {
      setSiurCatalog(null);
      setSiurPreferences(null);
      setSiurCatalogError("");
      setIsLoadingSiurCatalog(false);
      return;
    }

    const controller = new AbortController();
    siurCatalogAbortControllerRef.current = controller;
    setSiurCatalog(null);
    setSiurPreferences(null);
    setSiurCatalogError("");
    setIsLoadingSiurCatalog(true);

    fetchReferenceCatalog(
      siurOrganizationId,
      getStoredToken(),
      controller.signal,
    )
      .then((catalog) => {
        if (controller.signal.aborted) {
          return;
        }
        let storedPreferences: Partial<SiurMapPreferences> | null = null;
        try {
          const serialized = window.localStorage.getItem(
            siurPreferencesKey(siurOrganizationId),
          );
          storedPreferences = serialized
            ? (JSON.parse(serialized) as Partial<SiurMapPreferences>)
            : null;
        } catch {
          // A valid catalog must remain usable when browser storage is denied.
          storedPreferences = null;
        }
        setSiurCatalog(catalog);
        setSiurPreferences(
          reconcileSiurPreferences(catalog, storedPreferences),
        );
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) {
          return;
        }
        setSiurCatalog(null);
        setSiurPreferences(null);
        handleRequestErrorRef.current(
          error,
          setSiurCatalogError,
          "No se pudo cargar la cartografía SIUR.",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoadingSiurCatalog(false);
        }
      });

    return () => {
      controller.abort();
      siurIdentifyAbortControllerRef.current?.abort();
      siurIdentifyAbortControllerRef.current = null;
      if (siurCatalogAbortControllerRef.current === controller) {
        siurCatalogAbortControllerRef.current = null;
      }
    };
  }, [canViewMap, getStoredToken, siurOrganizationId]);

  const mapLayers = useMemo(() => {
    const definitions = new Map<string, MapLayer>();
    definitions.set("requirements", {
      key: "requirements",
      label: "Necesidades",
      entityType: "requirement",
      color: MAP_LAYER_COLORS.requirement,
      group: "work",
      count: 0,
    });
    definitions.set("projects", {
      key: "projects",
      label: "Proyectos",
      entityType: "project",
      color: MAP_LAYER_COLORS.project,
      group: "work",
      count: 0,
    });

    for (const item of items) {
      const key = mapItemLayerKey(item);
      const current = definitions.get(key);
      if (current) {
        current.count += 1;
        continue;
      }
      definitions.set(key, {
        key,
        label: mapItemLayerLabel(item),
        entityType: item.entity_type,
        color: mapItemLayerColor(item),
        group: item.entity_type === "asset" ? "assets" : "work",
        count: 1,
      });
    }

    if (
      canViewAssets &&
      !Array.from(definitions.values()).some(
        (definition) => definition.entityType === "asset",
      )
    ) {
      definitions.set("assets", {
        key: "assets",
        label: "Activos municipales",
        entityType: "asset",
        color: MAP_LAYER_COLORS.asset,
        group: "assets",
        count: 0,
      });
    }

    return Array.from(definitions.values());
  }, [canViewAssets, items]);

  const orderedLayers = useMemo(() => {
    const orderIndex = new Map(
      layerOrder.map((layerKey, index) => [layerKey, index]),
    );
    return [...mapLayers].sort((layerA, layerB) => {
      const indexA = orderIndex.get(layerA.key);
      const indexB = orderIndex.get(layerB.key);
      if (indexA !== undefined || indexB !== undefined) {
        return (indexA ?? Number.MAX_SAFE_INTEGER) -
          (indexB ?? Number.MAX_SAFE_INTEGER);
      }
      if (layerA.group !== layerB.group) {
        return layerA.group === "work" ? -1 : 1;
      }
      return layerA.label.localeCompare(layerB.label, "es");
    });
  }, [layerOrder, mapLayers]);
  const siurTree = useMemo(
    () => (siurCatalog ? buildReferenceLayerTree(siurCatalog.layers) : null),
    [siurCatalog],
  );
  const siurMapLayers = useMemo(
    () =>
      siurCatalog && siurPreferences
        ? buildSiurMapLayers(siurCatalog, siurPreferences)
        : [],
    [siurCatalog, siurPreferences],
  );

  const availableStatuses = useMemo(
    () =>
      Array.from(new Set(items.map(mapItemState))).sort((statusA, statusB) =>
        formatStatus(statusA).localeCompare(formatStatus(statusB), "es"),
      ),
    [items],
  );
  const normalizedSearchQuery = normalizeSearchText(searchQuery);
  const displayedItems = useMemo(
    () =>
      items.filter((item) => {
        if (visibleLayerKeys[mapItemLayerKey(item)] === false) {
          return false;
        }
        const itemStatus = mapItemState(item);
        if (stateLayerEnabled && visibleStatuses[itemStatus] === false) {
          return false;
        }
        return mapItemMatchesSearch(item, normalizedSearchQuery);
      }),
    [
      items,
      normalizedSearchQuery,
      stateLayerEnabled,
      visibleLayerKeys,
      visibleStatuses,
    ],
  );
  const searchResults = useMemo(
    () => (normalizedSearchQuery ? displayedItems.slice(0, 12) : []),
    [displayedItems, normalizedSearchQuery],
  );
  const areaItems = useMemo(
    () =>
      areaBounds
        ? displayedItems.filter((item) => itemIsInsideBounds(item, areaBounds))
        : [],
    [areaBounds, displayedItems],
  );
  const markerColors = useMemo(
    () =>
      Object.fromEntries(
        displayedItems.map((item) => [
          getItemKey(item),
          stateLayerEnabled
            ? statusColor(mapItemState(item))
            : mapItemLayerColor(item),
        ]),
      ),
    [displayedItems, stateLayerEnabled],
  );

  useEffect(() => {
    if (
      selectedItem &&
      !displayedItems.some(
        (item) => getItemKey(item) === getItemKey(selectedItem),
      )
    ) {
      setSelectedItem(null);
      setManualFocusLocation(null);
      setManualFocusZoom(null);
    }
  }, [displayedItems, selectedItem]);

  useEffect(() => {
    try {
      const serialized = window.localStorage.getItem(MAP_PREFERENCES_KEY);
      if (serialized) {
        const preferences = JSON.parse(serialized) as {
          visibleLayerKeys?: Record<string, boolean>;
          layerOrder?: string[];
          layerPanelOpen?: boolean;
          stateLayerEnabled?: boolean;
          visibleStatuses?: Record<string, boolean>;
          baseLayer?: MapBaseLayer;
        };
        if (preferences.visibleLayerKeys) {
          setVisibleLayerKeys(preferences.visibleLayerKeys);
        }
        if (Array.isArray(preferences.layerOrder)) {
          setLayerOrder(preferences.layerOrder);
        }
        if (typeof preferences.layerPanelOpen === "boolean") {
          setLayerPanelOpen(preferences.layerPanelOpen);
        }
        if (typeof preferences.stateLayerEnabled === "boolean") {
          setStateLayerEnabled(preferences.stateLayerEnabled);
        }
        if (preferences.visibleStatuses) {
          setVisibleStatuses(preferences.visibleStatuses);
        }
        if (
          preferences.baseLayer === "street" ||
          preferences.baseLayer === "topographic"
        ) {
          setBaseLayer(preferences.baseLayer);
        }
      }
    } catch {
      window.localStorage.removeItem(MAP_PREFERENCES_KEY);
    } finally {
      setPreferencesReady(true);
    }
  }, []);

  useEffect(() => {
    setLayerOrder((currentOrder) => {
      const availableKeys = new Set(mapLayers.map((layer) => layer.key));
      const nextOrder = currentOrder.filter((key) => availableKeys.has(key));
      for (const layer of mapLayers) {
        if (!nextOrder.includes(layer.key)) {
          nextOrder.push(layer.key);
        }
      }
      return nextOrder.join("|") === currentOrder.join("|")
        ? currentOrder
        : nextOrder;
    });
  }, [mapLayers]);

  useEffect(() => {
    if (!preferencesReady) {
      return;
    }
    try {
      window.localStorage.setItem(
        MAP_PREFERENCES_KEY,
        JSON.stringify({
          visibleLayerKeys,
          layerOrder,
          layerPanelOpen,
          stateLayerEnabled,
          visibleStatuses,
          baseLayer,
        }),
      );
    } catch {
      // Preferences are optional when browser storage is unavailable.
    }
  }, [
    baseLayer,
    layerOrder,
    layerPanelOpen,
    preferencesReady,
    stateLayerEnabled,
    visibleLayerKeys,
    visibleStatuses,
  ]);

  useEffect(() => {
    if (!siurPreferences || siurOrganizationId === null) {
      return;
    }
    try {
      window.localStorage.setItem(
        siurPreferencesKey(siurOrganizationId),
        JSON.stringify(siurPreferences),
      );
    } catch {
      // SIUR display preferences remain optional in restricted browsers.
    }
  }, [siurOrganizationId, siurPreferences]);

  const isAssetDialogOpen = assetLocationDraft !== null;
  const assetOrganizationId = assetLocationDraft?.organizationId ?? "";

  useEffect(() => {
    if (!isAssetDialogOpen || !assetOrganizationId) {
      assetAbortControllerRef.current?.abort();
      assetAbortControllerRef.current = null;
      assetRequestSequenceRef.current += 1;
      setAssetOptions([]);
      setAssetTotal(0);
      setAssetLoadError("");
      setIsLoadingAssets(false);
      return;
    }

    const organizationId = Number.parseInt(assetOrganizationId, 10);
    if (!Number.isInteger(organizationId)) {
      setAssetOptions([]);
      setAssetTotal(0);
      setAssetLoadError("Selecciona una organización municipal.");
      setIsLoadingAssets(false);
      return;
    }

    assetAbortControllerRef.current?.abort();
    const controller = new AbortController();
    assetAbortControllerRef.current = controller;
    const requestSequence = ++assetRequestSequenceRef.current;
    setAssetOptions([]);
    setAssetTotal(0);
    setAssetLoadError("");
    setIsLoadingAssets(true);

    const debounceId = window.setTimeout(() => {
      void fetchMunicipalAssets(
        organizationId,
        assetSearch,
        controller.signal,
      )
        .then(({ items: assets, total }) => {
          if (
            controller.signal.aborted ||
            requestSequence !== assetRequestSequenceRef.current
          ) {
            return;
          }
          setAssetOptions(
            assets.filter((asset) => asset.status !== "archived"),
          );
          setAssetTotal(total);
        })
        .catch((error) => {
          if (
            controller.signal.aborted ||
            requestSequence !== assetRequestSequenceRef.current ||
            isAbortError(error)
          ) {
            return;
          }
          if (error instanceof ApiRequestError && error.status === 403) {
            setAssetLoadError(
              "No puedes consultar el inventario de esta organización. Selecciona otra organización activa o solicita acceso.",
            );
            return;
          }
          handleRequestErrorRef.current(
            error,
            setAssetLoadError,
            "No se pudo cargar el inventario municipal.",
          );
        })
        .finally(() => {
          if (
            !controller.signal.aborted &&
            requestSequence === assetRequestSequenceRef.current
          ) {
            setIsLoadingAssets(false);
          }
        });
    }, assetSearch.trim() ? 300 : 0);

    return () => {
      window.clearTimeout(debounceId);
      controller.abort();
      if (assetAbortControllerRef.current === controller) {
        assetAbortControllerRef.current = null;
      }
    };
  }, [
    assetOrganizationId,
    assetReloadVersion,
    assetSearch,
    isAssetDialogOpen,
  ]);

  useEffect(() => {
    if (!isAssetDialogOpen) {
      return;
    }
    assetSearchRef.current?.focus();
  }, [isAssetDialogOpen]);

  useEffect(() => {
    if (!mapContextMenu) {
      return;
    }

    function closeContextMenu(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        setMapContextMenu(null);
      }
    }

    window.addEventListener("keydown", closeContextMenu);
    return () => {
      window.removeEventListener("keydown", closeContextMenu);
    };
  }, [mapContextMenu]);

  useEffect(() => {
    if (!isAssetDialogOpen) {
      return;
    }

    function closeAssetDialog(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !isLocatingAsset) {
        setAssetLocationDraft(null);
        setAssetLocationError("");
      }
    }

    window.addEventListener("keydown", closeAssetDialog);
    return () => {
      window.removeEventListener("keydown", closeAssetDialog);
    };
  }, [isAssetDialogOpen, isLocatingAsset]);

  const handleSiurControlChange = useCallback(
    (layerId: number, control: SiurLayerControl) => {
      setSiurPreferences((current) => {
        if (!current || !current.layers[String(layerId)]) {
          return current;
        }
        return {
          ...current,
          layers: {
            ...current.layers,
            [String(layerId)]: {
              ...control,
              opacity: Math.min(1, Math.max(0, control.opacity)),
            },
          },
        };
      });
    },
    [],
  );

  const handleSiurMove = useCallback(
    (layerId: number, direction: "forward" | "backward") => {
      setSiurPreferences((current) => {
        if (!current) {
          return current;
        }
        const sourceIndex = current.stackOrder.indexOf(layerId);
        const targetIndex =
          direction === "forward" ? sourceIndex + 1 : sourceIndex - 1;
        if (
          sourceIndex < 0 ||
          targetIndex < 0 ||
          targetIndex >= current.stackOrder.length
        ) {
          return current;
        }
        const stackOrder = [...current.stackOrder];
        [stackOrder[sourceIndex], stackOrder[targetIndex]] = [
          stackOrder[targetIndex],
          stackOrder[sourceIndex],
        ];
        return { ...current, stackOrder };
      });
    },
    [],
  );

  const handleSiurIdentify = useCallback(
    (point: SiurIdentifyPoint) => {
      siurIdentifyAbortControllerRef.current?.abort();
      const controller = new AbortController();
      siurIdentifyAbortControllerRef.current = controller;
      setSelectedItem(null);
      setManualFocusLocation(null);
      setManualFocusZoom(null);
      setSiurIdentify({
        layerId: point.layer.layerId,
        layerTitle: point.layer.title,
        features: [],
        isLoading: true,
        error: "",
      });

      fetchReferenceIdentify(point, getStoredToken(), controller.signal)
        .then((result) => {
          if (controller.signal.aborted) {
            return;
          }
          setSiurIdentify({
            layerId: point.layer.layerId,
            layerTitle: point.layer.title,
            features: result.features,
            isLoading: false,
            error: "",
          });
        })
        .catch((error) => {
          if (controller.signal.aborted || isAbortError(error)) {
            return;
          }
          handleRequestErrorRef.current(
            error,
            (errorMessage) =>
              setSiurIdentify({
                layerId: point.layer.layerId,
                layerTitle: point.layer.title,
                features: [],
                isLoading: false,
                error: errorMessage,
              }),
            "No se pudo consultar la capa SIUR.",
          );
        })
        .finally(() => {
          if (siurIdentifyAbortControllerRef.current === controller) {
            siurIdentifyAbortControllerRef.current = null;
          }
        });
    },
    [getStoredToken],
  );

  const handleSelectItem = useCallback((item: GeoMapItem) => {
    siurIdentifyAbortControllerRef.current?.abort();
    siurIdentifyAbortControllerRef.current = null;
    setSiurIdentify(null);
    setSelectedItem(item);
    if (
      typeof item.location.latitude === "number" &&
      typeof item.location.longitude === "number"
    ) {
      setManualFocusLocation({
        latitude: item.location.latitude,
        longitude: item.location.longitude,
        label: item.location.label,
      });
      setManualFocusZoom(16);
    }
    setMapContextMenu(null);
  }, []);

  function toggleLayer(layerKey: string) {
    setVisibleLayerKeys((current) => ({
      ...current,
      [layerKey]: current[layerKey] === false,
    }));
  }

  function clearSelectedItem() {
    setSelectedItem(null);
    setManualFocusLocation(null);
    setManualFocusZoom(null);
  }

  function handleLayerDragStart(
    event: DragEvent<HTMLButtonElement>,
    layerKey: string,
  ) {
    layerDragKeyRef.current = layerKey;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", layerKey);
  }

  function handleLayerDrop(
    event: DragEvent<HTMLDivElement>,
    targetLayerKey: string,
  ) {
    event.preventDefault();
    const sourceLayerKey =
      layerDragKeyRef.current || event.dataTransfer.getData("text/plain");
    layerDragKeyRef.current = null;
    if (!sourceLayerKey || sourceLayerKey === targetLayerKey) {
      return;
    }
    setLayerOrder((currentOrder) => {
      const nextOrder = currentOrder.filter((key) => key !== sourceLayerKey);
      const targetIndex = nextOrder.indexOf(targetLayerKey);
      nextOrder.splice(
        targetIndex < 0 ? nextOrder.length : targetIndex,
        0,
        sourceLayerKey,
      );
      return nextOrder;
    });
  }

  function swapLayerPositions(
    sourceLayerKey: string,
    targetLayerKey: string | undefined,
  ) {
    if (!targetLayerKey) {
      return;
    }
    setLayerOrder((currentOrder) => {
      const sourceIndex = currentOrder.indexOf(sourceLayerKey);
      const targetIndex = currentOrder.indexOf(targetLayerKey);
      if (sourceIndex < 0 || targetIndex < 0) {
        return currentOrder;
      }
      const nextOrder = [...currentOrder];
      [nextOrder[sourceIndex], nextOrder[targetIndex]] = [
        nextOrder[targetIndex],
        nextOrder[sourceIndex],
      ];
      return nextOrder;
    });
  }

  function toggleAreaSelection() {
    if (
      !areaSelectionEnabled &&
      typeof window !== "undefined" &&
      !window.matchMedia("(any-pointer: fine)").matches
    ) {
      setMessage(
        "La selección rectangular requiere un ratón o puntero de precisión.",
      );
      return;
    }
    setAreaSelectionEnabled((current) => {
      if (current) {
        setAreaBounds(null);
      }
      return !current;
    });
  }

  function clearAreaSelection() {
    setAreaSelectionEnabled(false);
    setAreaBounds(null);
  }

  function exportAreaItems(format: "geojson" | "csv") {
    const timestamp = new Date().toISOString().slice(0, 10);
    if (format === "geojson") {
      const featureCollection = {
        type: "FeatureCollection",
        features: areaItems.flatMap((item) => {
          const latitude = item.location.latitude;
          const longitude = item.location.longitude;
          if (typeof latitude !== "number" || typeof longitude !== "number") {
            return [];
          }
          return [
            {
              type: "Feature",
              geometry: {
                type: "Point",
                coordinates: [longitude, latitude],
              },
              properties: {
                entity_type: item.entity_type,
                entity_id: item.entity_id,
                role: item.role,
                title: item.title,
                status: item.status,
                priority: item.priority,
                organization: item.organization_name,
                layer: mapItemLayerLabel(item),
                location: item.location.label,
              },
            },
          ];
        }),
      };
      downloadTextFile(
        JSON.stringify(featureCollection, null, 2),
        `recorte-mapa-${timestamp}.geojson`,
        "application/geo+json;charset=utf-8",
      );
      return;
    }

    const rows = [
      [
        "tipo",
        "id",
        "titulo",
        "capa",
        "estado",
        "prioridad",
        "organizacion",
        "ubicacion",
        "latitud",
        "longitud",
      ],
      ...areaItems.map((item) => [
        item.entity_type,
        item.entity_id,
        item.title,
        mapItemLayerLabel(item),
        item.status,
        item.priority,
        item.organization_name,
        item.location.label,
        item.location.latitude,
        item.location.longitude,
      ]),
    ];
    downloadTextFile(
      rows.map((row) => row.map(csvCell).join(",")).join("\n"),
      `recorte-mapa-${timestamp}.csv`,
      "text/csv;charset=utf-8",
    );
  }

  const handleMapContextMenu = useCallback(
    (payload: {
      latitude: number;
      longitude: number;
      zoom: number;
      x: number;
      y: number;
    }) => {
      const nearest = displayedItems.reduce<{
        item: GeoMapItem;
        distanceMeters: number;
      } | null>((currentNearest, item) => {
        const latitude = item.location.latitude;
        const longitude = item.location.longitude;
        if (typeof latitude !== "number" || typeof longitude !== "number") {
          return currentNearest;
        }

        const distanceMeters = distanceInMeters(
          payload.latitude,
          payload.longitude,
          latitude,
          longitude,
        );
        if (!currentNearest || distanceMeters < currentNearest.distanceMeters) {
          return { item, distanceMeters };
        }
        return currentNearest;
      }, null);

      setMapContextMenu({
        ...payload,
        x: Math.max(14, Math.min(payload.x, window.innerWidth - 324)),
        y: Math.max(14, Math.min(payload.y, window.innerHeight - 360)),
        nearestItem: nearest?.item ?? null,
        nearestDistanceMeters: nearest?.distanceMeters ?? null,
      });
    },
    [displayedItems],
  );

  function focusContextPoint() {
    if (!mapContextMenu) {
      return;
    }
    setManualFocusLocation({
      latitude: mapContextMenu.latitude,
      longitude: mapContextMenu.longitude,
      label: "Punto seleccionado en el mapa",
    });
    setManualFocusZoom(Math.max(mapContextMenu.zoom, 16));
    setMapContextMenu(null);
  }

  function openRegistrationDraft(kind: CreatableMapEntityType) {
    if (!mapContextMenu) {
      return;
    }
    setRegistrationDraft({
      kind,
      latitude: mapContextMenu.latitude,
      longitude: mapContextMenu.longitude,
      organizationId: defaultOrganizationId(user),
      title: "",
      description: "",
    });
    setRegistrationError("");
    setMapContextMenu(null);
  }

  function openAssetLocationDraft() {
    if (!mapContextMenu || !canLocateAssets) {
      return;
    }
    const defaultOrganization = activeAssetOrganizations[0];
    if (!defaultOrganization) {
      setMessage(
        "Necesitas una organización activa con municipio asociado para ubicar activos municipales.",
      );
      setMapContextMenu(null);
      return;
    }

    setAssetLocationDraft({
      latitude: mapContextMenu.latitude,
      longitude: mapContextMenu.longitude,
      organizationId: String(defaultOrganization.id),
      assetId: "",
      label: "",
      address: "",
    });
    setAssetSearch("");
    setAssetLoadError("");
    setAssetLocationError("");
    setMessage("");
    setMapContextMenu(null);
  }

  function selectNearestItem() {
    if (!mapContextMenu?.nearestItem) {
      return;
    }
    handleSelectItem(mapContextMenu.nearestItem);
  }

  async function handleCreateMapRegistration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!registrationDraft) {
      return;
    }

    const organizationId = Number.parseInt(registrationDraft.organizationId, 10);
    if (!Number.isInteger(organizationId)) {
      setRegistrationError("Selecciona la organización municipal.");
      return;
    }
    const title = registrationDraft.title.trim();
    if (!title) {
      setRegistrationError(
        registrationDraft.kind === "requirement"
          ? "Escribe un título para la necesidad."
          : "Escribe un nombre para el proyecto.",
      );
      return;
    }

    setIsRegistering(true);
    setRegistrationError("");
    setMessage("");

    try {
      const token = getStoredToken();
      const description = registrationDraft.description.trim();
      const createdEntity =
        registrationDraft.kind === "requirement"
          ? await adminRequest<Requirement>(
              "/requirements",
              token,
              "No se pudo crear la necesidad.",
              {
                method: "POST",
                body: JSON.stringify({
                  organization_id: organizationId,
                  title,
                  summary: description || null,
                  problem: description || null,
                  priority: "medium",
                  status: "draft",
                  source_type: "manual",
                }),
              },
            )
          : await adminRequest<Project>(
              "/projects",
              token,
              "No se pudo crear el proyecto.",
              {
                method: "POST",
                body: JSON.stringify({
                  organization_id: organizationId,
                  name: title,
                  description: description || null,
                  status: "active",
                }),
              },
            );

      const mapItem = await createEntityLocation(token, {
        entity_type: registrationDraft.kind,
        entity_id: createdEntity.id,
        role: "primary",
        location: {
          organization_id: organizationId,
          label: title,
          latitude: registrationDraft.latitude,
          longitude: registrationDraft.longitude,
          source: "user_provided",
          confidence: 1,
          review_status: "proposed",
        },
      });

      mapAbortControllerRef.current?.abort();
      mapAbortControllerRef.current = null;
      mapRequestSequenceRef.current += 1;
      setIsLoading(false);
      setItems((currentItems) => [
        mapItem,
        ...currentItems.filter((item) => getItemKey(item) !== getItemKey(mapItem)),
      ]);
      setSelectedItem(mapItem);
      setManualFocusLocation({
        latitude: registrationDraft.latitude,
        longitude: registrationDraft.longitude,
        label: title,
      });
      setManualFocusZoom(16);
      setRegistrationDraft(null);
      setMessage(
        registrationDraft.kind === "requirement"
          ? "Necesidad registrada en el mapa."
          : "Proyecto registrado en el mapa.",
      );
    } catch (registrationErrorCaught) {
      handleRequestError(
        registrationErrorCaught,
        setRegistrationError,
        "No se pudo registrar el elemento en el mapa.",
      );
    } finally {
      setIsRegistering(false);
    }
  }

  async function handleLocateExistingAsset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!assetLocationDraft || !canLocateAssets) {
      return;
    }

    const organizationId = Number.parseInt(
      assetLocationDraft.organizationId,
      10,
    );
    if (!Number.isInteger(organizationId)) {
      setAssetLocationError("Selecciona la organización municipal.");
      return;
    }
    const selectedOrganization = activeAssetOrganizations.find(
      (organization) => organization.id === organizationId,
    );
    if (!selectedOrganization) {
      setAssetLocationError("La organización seleccionada ya no está activa.");
      return;
    }
    if (!selectedAsset || selectedAsset.organization_id !== organizationId) {
      setAssetLocationError("Selecciona un activo del inventario municipal.");
      return;
    }
    if (selectedAsset.status === "archived") {
      setAssetLocationError("No se puede ubicar un activo archivado.");
      return;
    }

    const draft = assetLocationDraft;
    const asset = selectedAsset;
    const label = draft.label.trim() || asset.name;
    const address = draft.address.trim();
    const wasAlreadyLocated = asset.location_id !== null;
    setIsLocatingAsset(true);
    setAssetLocationError("");
    setMessage("");

    try {
      const mapItem = await createEntityLocation(getStoredToken(), {
        entity_type: "asset",
        entity_id: asset.id,
        role: "primary",
        location: {
          organization_id: organizationId,
          label,
          latitude: draft.latitude,
          longitude: draft.longitude,
          address_text: address || null,
        },
      });
      if (mapItem.entity_type !== "asset") {
        throw new Error("La API no devolvió el activo municipal esperado.");
      }

      mapAbortControllerRef.current?.abort();
      mapAbortControllerRef.current = null;
      mapRequestSequenceRef.current += 1;
      setIsLoading(false);
      setItems((currentItems) => [
        mapItem,
        ...currentItems.filter(
          (item) => getItemKey(item) !== getItemKey(mapItem),
        ),
      ]);
      setSelectedItem(mapItem);
      setManualFocusLocation({
        latitude: draft.latitude,
        longitude: draft.longitude,
        label,
      });
      setManualFocusZoom(16);
      setAssetLocationDraft(null);
      setMessage(
        wasAlreadyLocated
          ? "Activo municipal recolocado en el mapa."
          : "Activo municipal ubicado en el mapa.",
      );
    } catch (assetLocationErrorCaught) {
      if (
        assetLocationErrorCaught instanceof ApiRequestError &&
        assetLocationErrorCaught.status === 403
      ) {
        setAssetLocationError(
          "No puedes ubicar activos en esta organización. Selecciona otra organización o solicita permisos de mapa e inventario.",
        );
      } else {
        handleRequestError(
          assetLocationErrorCaught,
          setAssetLocationError,
          "No se pudo ubicar el activo municipal.",
        );
      }
    } finally {
      setIsLocatingAsset(false);
    }
  }

  const selectedFocusLocation = selectedItem
    ? (() => {
        const latitude = selectedItem.location.latitude;
        const longitude = selectedItem.location.longitude;
        if (latitude === null || longitude === null) {
          return null;
        }
        return {
          latitude,
          longitude,
          label: selectedItem.location.label,
        };
      })()
    : null;

  function handleViewMunicipalityOnMap(location: MunicipalityMapFocus) {
    setManualFocusLocation(location);
    setManualFocusZoom(MUNICIPAL_CAPITAL_ZOOM);
    setSelectedItem(null);
    setActiveView("territory");
  }

  if (!canViewMap && !canViewMunicipalities) {
    return (
      <section className="panel map-panel">
        <p className="eyebrow">Territorio</p>
        <h1>Mapa municipal</h1>
        <p className="muted">No tienes permisos para ver el mapa municipal.</p>
      </section>
    );
  }

  return (
    <section className="panel map-panel">
      <div className="panel-header map-panel-header">
        <div>
          <p className="eyebrow">Territorio</p>
          <h1>Mapa municipal</h1>
          <p className="muted map-panel-helper">
            Explora el trabajo geolocalizado, el inventario y el contexto de
            otros municipios desde una única vista.
          </p>
        </div>
        {activeView === "territory" ? (
          <span className="map-count" aria-live="polite">
            {displayedItems.length}
            {displayedItems.length !== items.length ? ` de ${items.length}` : ""}{" "}
            {items.length === 1 ? "elemento" : "elementos"}
          </span>
        ) : null}
      </div>

      <div className="map-view-tabs" role="tablist" aria-label="Vistas del mapa">
        {canViewMap ? (
          <button
            aria-selected={activeView === "territory"}
            className={activeView === "territory" ? "is-active" : ""}
            onClick={() => setActiveView("territory")}
            role="tab"
            type="button"
          >
            Territorio municipal
          </button>
        ) : null}
        {canViewMunicipalities ? (
          <button
            aria-selected={activeView === "municipalities"}
            className={activeView === "municipalities" ? "is-active" : ""}
            onClick={() => setActiveView("municipalities")}
            role="tab"
            type="button"
          >
            Municipios y normativa
          </button>
        ) : null}
      </div>

      {activeView === "municipalities" ? (
        <MunicipalityMapDirectory
          user={user}
          onViewOnMap={canViewMap ? handleViewMunicipalityOnMap : undefined}
        />
      ) : (
        <>

      <div className="map-command-bar" aria-label="Herramientas del mapa municipal">
        <div className="map-global-search">
          <label htmlFor="map-global-search">Buscar en el mapa</label>
          <div className="map-search-input-row">
            <span aria-hidden="true">⌕</span>
            <input
              autoComplete="off"
              id="map-global-search"
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Nombre, calle, estado, responsable…"
              type="search"
              value={searchQuery}
            />
            {searchQuery ? (
              <button
                aria-label="Limpiar búsqueda"
                onClick={() => setSearchQuery("")}
                type="button"
              >
                ×
              </button>
            ) : null}
          </div>
          {normalizedSearchQuery ? (
            <div className="map-search-results" aria-label="Resultados de búsqueda">
              {searchResults.length > 0 ? (
                searchResults.map((item) => (
                  <button
                    key={getItemKey(item)}
                    onClick={() => {
                      handleSelectItem(item);
                      setSearchQuery("");
                    }}
                    type="button"
                  >
                    <span
                      aria-hidden="true"
                      className="map-search-result-dot"
                      style={{ backgroundColor: mapItemLayerColor(item) }}
                    />
                    <span>
                      <strong>{item.title}</strong>
                      <small>
                        {mapItemLayerLabel(item)} · {item.location.label}
                      </small>
                    </span>
                    <small>{formatStatus(mapItemState(item))}</small>
                  </button>
                ))
              ) : (
                <p>No hay elementos que coincidan con la búsqueda.</p>
              )}
            </div>
          ) : null}
        </div>
        <label className="map-base-layer-control">
          Mapa base
          <select
            onChange={(event) =>
              setBaseLayer(event.target.value as MapBaseLayer)
            }
            value={baseLayer}
          >
            <option value="street">Calles</option>
            <option value="topographic">Topográfico</option>
          </select>
        </label>
        <label className="checkbox-row map-archive-filter">
          <input
            checked={includeArchived}
            type="checkbox"
            onChange={(event) => setIncludeArchived(event.target.checked)}
          />
          Incluir archivados
        </label>
        <div className="map-command-actions">
          <button
            className="secondary-button"
            onClick={() => setFitRequest((current) => current + 1)}
            type="button"
          >
            Ver todo
          </button>
          <button
            className="secondary-button"
            onClick={() => setLocateRequest((current) => current + 1)}
            type="button"
          >
            Mi ubicación
          </button>
        </div>
      </div>

      {message ? (
        <p className="map-message" role="status">
          {message}
        </p>
      ) : null}

      <div
        className={`map-content-grid${layerPanelOpen ? "" : " map-content-grid--layers-collapsed"}`}
      >
        <aside className="map-layer-panel" aria-label="Capas del mapa">
          <div className="map-layer-panel-heading">
            {layerPanelOpen ? <strong>Capas</strong> : null}
            <button
              aria-label={
                layerPanelOpen
                  ? "Plegar panel de capas"
                  : "Desplegar panel de capas"
              }
              onClick={() => setLayerPanelOpen((current) => !current)}
              title={layerPanelOpen ? "Plegar capas" : "Desplegar capas"}
              type="button"
            >
              {layerPanelOpen ? "‹" : "›"}
            </button>
          </div>

          {layerPanelOpen ? (
            <div className="map-layer-groups">
              {(["work", "assets"] as const).map((group) => {
                const groupLayers = orderedLayers.filter(
                  (layer) => layer.group === group,
                );
                if (groupLayers.length === 0) {
                  return null;
                }
                return (
                  <section key={group}>
                    <h2>
                      {group === "work"
                        ? "Trabajo municipal"
                        : "Patrimonio e instalaciones"}
                    </h2>
                    <div>
                      {groupLayers.map((layer, layerIndex) => {
                        const isVisible = visibleLayerKeys[layer.key] !== false;
                        return (
                          <div
                            className="map-layer-row"
                            key={layer.key}
                            onDragOver={(event) => event.preventDefault()}
                            onDrop={(event) =>
                              handleLayerDrop(event, layer.key)
                            }
                          >
                            <button
                              aria-label={`Reordenar ${layer.label}`}
                              className="map-layer-drag"
                              draggable
                              onDragEnd={() => {
                                layerDragKeyRef.current = null;
                              }}
                              onDragStart={(event) =>
                                handleLayerDragStart(event, layer.key)
                              }
                              title="Arrastrar para reordenar"
                              type="button"
                            >
                              ⠿
                            </button>
                            <div className="map-layer-order-buttons">
                              <button
                                aria-label={`Subir ${layer.label}`}
                                disabled={layerIndex === 0}
                                onClick={() =>
                                  swapLayerPositions(
                                    layer.key,
                                    groupLayers[layerIndex - 1]?.key,
                                  )
                                }
                                type="button"
                              >
                                ↑
                              </button>
                              <button
                                aria-label={`Bajar ${layer.label}`}
                                disabled={layerIndex === groupLayers.length - 1}
                                onClick={() =>
                                  swapLayerPositions(
                                    layer.key,
                                    groupLayers[layerIndex + 1]?.key,
                                  )
                                }
                                type="button"
                              >
                                ↓
                              </button>
                            </div>
                            <label>
                              <input
                                checked={isVisible}
                                onChange={() => toggleLayer(layer.key)}
                                type="checkbox"
                              />
                              <span
                                aria-hidden="true"
                                className="map-layer-swatch"
                                style={{ backgroundColor: layer.color }}
                              />
                              <span title={layer.label}>{layer.label}</span>
                              <small>{layer.count}</small>
                            </label>
                          </div>
                        );
                      })}
                    </div>
                  </section>
                );
              })}
              <SiurLayerTree
                catalog={siurCatalog}
                error={siurCatalogError}
                isLoading={isLoadingSiurCatalog}
                onControlChange={handleSiurControlChange}
                onMove={handleSiurMove}
                preferences={siurPreferences}
                structuralWarnings={siurTree?.warnings ?? []}
                tree={siurTree?.roots ?? []}
              />
              {canViewAssets ? (
                <Link className="map-layer-manage-link" href="/inventario">
                  Gestionar categorías y activos
                </Link>
              ) : null}
            </div>
          ) : (
            <div className="map-layer-rail">
              {orderedLayers.map((layer) => {
                const isVisible = visibleLayerKeys[layer.key] !== false;
                return (
                  <button
                    aria-pressed={isVisible}
                    key={layer.key}
                    onClick={() => toggleLayer(layer.key)}
                    style={{
                      borderColor: isVisible ? layer.color : undefined,
                      color: isVisible ? layer.color : undefined,
                    }}
                    title={`${layer.label} · ${layer.count}`}
                    type="button"
                  >
                    <span
                      aria-hidden="true"
                      style={{ backgroundColor: layer.color }}
                    />
                  </button>
                );
              })}
            </div>
          )}
        </aside>

        <div className="map-main-column">
          <div className="map-layer-chips" aria-label="Acceso rápido a capas">
            {orderedLayers.map((layer) => {
              const isVisible = visibleLayerKeys[layer.key] !== false;
              return (
                <button
                  aria-pressed={isVisible}
                  className={isVisible ? "is-active" : ""}
                  key={layer.key}
                  onClick={() => toggleLayer(layer.key)}
                  style={{ borderColor: isVisible ? layer.color : undefined }}
                  type="button"
                >
                  <span
                    aria-hidden="true"
                    style={{ backgroundColor: layer.color }}
                  />
                  {layer.label}
                </button>
              );
            })}
          </div>
          <MunicipalMap
            areaBounds={areaBounds}
            areaSelectionEnabled={areaSelectionEnabled}
            baseLayer={baseLayer}
            fitRequest={fitRequest}
            focusLocation={
              manualFocusLocation ??
              explicitFocusLocation ??
              selectedFocusLocation
            }
            initialZoom={manualFocusZoom ?? focusedZoom}
            items={displayedItems}
            locateRequest={locateRequest}
            markerColors={markerColors}
            onAreaSelectionChange={setAreaBounds}
            onLocationError={setMessage}
            onMapContextMenu={handleMapContextMenu}
            onSelectItem={handleSelectItem}
            onSiurIdentify={handleSiurIdentify}
            selectedItemId={selectedItem ? getItemKey(selectedItem) : null}
            siurLayers={siurMapLayers}
          />

          <div className="map-overlay-tools">
            <button
              aria-pressed={areaSelectionEnabled}
              className={areaSelectionEnabled ? "is-active" : ""}
              onClick={toggleAreaSelection}
              type="button"
            >
              Seleccionar área
            </button>
            <button
              aria-pressed={stateLayerEnabled}
              className={stateLayerEnabled ? "is-active" : ""}
              onClick={() => setStateLayerEnabled((current) => !current)}
              type="button"
            >
              Capa de estado
            </button>
          </div>

          {stateLayerEnabled ? (
            <div className="map-state-legend" aria-label="Leyenda de estados">
              {availableStatuses.map((itemStatus) => {
                const isVisible = visibleStatuses[itemStatus] !== false;
                const color = statusColor(itemStatus);
                return (
                  <button
                    aria-pressed={isVisible}
                    className={isVisible ? "is-active" : ""}
                    key={itemStatus}
                    onClick={() =>
                      setVisibleStatuses((current) => ({
                        ...current,
                        [itemStatus]: current[itemStatus] === false,
                      }))
                    }
                    type="button"
                  >
                    <span
                      aria-hidden="true"
                      style={{ backgroundColor: isVisible ? color : undefined }}
                    />
                    {formatStatus(itemStatus)}
                  </button>
                );
              })}
            </div>
          ) : null}

          {areaSelectionEnabled || areaBounds ? (
            <div className="map-area-export" role="status">
              <span>
                {areaBounds
                  ? `${areaItems.length} ${areaItems.length === 1 ? "elemento" : "elementos"} en el área`
                  : "Arrastra sobre el mapa para delimitar un área"}
              </span>
              <button
                disabled={!areaBounds}
                onClick={() => exportAreaItems("geojson")}
                type="button"
              >
                GeoJSON
              </button>
              <button
                disabled={!areaBounds}
                onClick={() => exportAreaItems("csv")}
                type="button"
              >
                CSV
              </button>
              <button
                disabled={!areaBounds}
                onClick={() => window.print()}
                type="button"
              >
                Imprimir
              </button>
              <button onClick={clearAreaSelection} type="button">
                Cerrar
              </button>
            </div>
          ) : null}
        </div>

        <aside
          className="map-detail-card"
          aria-label="Detalle del mapa"
        >
          {selectedItem ? (
            <>
              <div className="map-detail-heading">
                <span className="map-item-type">
                  {entityTypeLabel(selectedItem.entity_type)}
                </span>
                <button
                  aria-label="Cerrar detalle"
                  onClick={clearSelectedItem}
                  type="button"
                >
                  ×
                </button>
              </div>
              <h2>{selectedItem.title}</h2>
              {selectedItem.subtitle ? <p>{selectedItem.subtitle}</p> : null}
              <dl>
                <div>
                  <dt>Capa</dt>
                  <dd>{mapItemLayerLabel(selectedItem)}</dd>
                </div>
                <div>
                  <dt>Estado</dt>
                  <dd>{formatStatus(selectedItem.status)}</dd>
                </div>
                {selectedItem.condition_status ? (
                  <div>
                    <dt>Conservación</dt>
                    <dd>{formatStatus(selectedItem.condition_status)}</dd>
                  </div>
                ) : null}
                {selectedItem.priority ? (
                  <div>
                    <dt>Prioridad</dt>
                    <dd>{formatStatus(selectedItem.priority)}</dd>
                  </div>
                ) : null}
                <div>
                  <dt>Organización</dt>
                  <dd>{selectedItem.organization_name}</dd>
                </div>
                <div>
                  <dt>Ubicación</dt>
                  <dd>{selectedItem.location.label}</dd>
                </div>
                <div>
                  <dt>Rol geográfico</dt>
                  <dd>{locationRoleLabel(selectedItem.role)}</dd>
                </div>
              </dl>
              <Link
                className="secondary-button map-detail-link"
                href={normalizeDetailPath(selectedItem)}
              >
                {selectedItem.entity_type === "asset"
                  ? "Abrir contexto municipal"
                  : `Abrir ${entityDetailLabel(selectedItem.entity_type)}`}
              </Link>
              {selectedItem.entity_type === "asset" ? (
                <AssetMaintenancePanel
                  assetId={selectedItem.entity_id}
                  assetName={selectedItem.title}
                  assetStatus={normalizeAssetStatus(selectedItem.status)}
                  key={`${selectedItem.organization_id}-${selectedItem.entity_id}`}
                  organizationId={selectedItem.organization_id}
                  organizationStatus={
                    user.organizations?.find(
                      (organization) =>
                        organization.id === selectedItem.organization_id,
                    )?.status ?? "archived"
                  }
                  user={user}
                />
              ) : null}
            </>
          ) : siurIdentify ? (
            <div className="siur-identify-detail">
              <div className="map-detail-heading">
                <span className="map-item-type">Consulta SIUR</span>
                <button
                  aria-label="Cerrar consulta SIUR"
                  onClick={() => {
                    siurIdentifyAbortControllerRef.current?.abort();
                    siurIdentifyAbortControllerRef.current = null;
                    setSiurIdentify(null);
                  }}
                  type="button"
                >
                  ×
                </button>
              </div>
              <h2>{siurIdentify.layerTitle}</h2>
              {siurIdentify.isLoading ? (
                <p role="status">Consultando la capa superior visible…</p>
              ) : null}
              {siurIdentify.error ? (
                <p className="siur-layer-error" role="alert">
                  {siurIdentify.error}
                </p>
              ) : null}
              {!siurIdentify.isLoading &&
              !siurIdentify.error &&
              siurIdentify.features.length === 0 ? (
                <p>No hay elementos de esta capa en el punto seleccionado.</p>
              ) : null}
              {siurIdentify.features.map((feature, featureIndex) => {
                const properties = Object.entries(feature.properties);
                return (
                  <section
                    className="siur-identify-feature"
                    key={`${siurIdentify.layerId}-${featureIndex}`}
                  >
                    <h3>Resultado {featureIndex + 1}</h3>
                    <dl>
                      {properties.slice(0, 20).map(([key, value]) => (
                        <div key={key}>
                          <dt>{key.slice(0, 120)}</dt>
                          <dd>{formatIdentifyProperty(value)}</dd>
                        </div>
                      ))}
                    </dl>
                    {properties.length > 20 ? (
                      <p className="small-muted">
                        Se muestran 20 de {properties.length} atributos.
                      </p>
                    ) : null}
                  </section>
                );
              })}
            </div>
          ) : (
            <div className="map-detail-placeholder">
              <h2>Registra trabajo sobre el territorio</h2>
              <p>
                Haz clic derecho en cualquier punto para crear una necesidad, un
                proyecto o ubicar un activo existente. Pulsa un marcador para
                consultar su detalle en el mapa.
              </p>
              {displayedItems.length === 0 ? (
                <p className="small-muted">
                  {isLoading
                    ? "Cargando ubicaciones…"
                    : items.length > 0
                      ? "No hay elementos que cumplan los filtros y capas activos."
                      : "Aún no hay elementos ubicados, pero puedes registrar o ubicar el primero desde el mapa."}
                </p>
              ) : null}
            </div>
          )}
        </aside>
      </div>
        </>
      )}

      {mapContextMenu ? (
        <div
          className="map-context-menu"
          role="group"
          aria-label="Herramientas del mapa"
          style={{ left: mapContextMenu.x, top: mapContextMenu.y }}
        >
          <div className="map-context-menu-head">
            <strong>Herramientas en este punto</strong>
            <span>
              {formatCoordinates(
                mapContextMenu.latitude,
                mapContextMenu.longitude,
              )}
            </span>
          </div>
          {canCreateRequirements ? (
            <button
              type="button"
              onClick={() => openRegistrationDraft("requirement")}
            >
              Registrar necesidad aquí
              <span>Crear una necesidad municipal con esta ubicación</span>
            </button>
          ) : null}
          {canCreateProjects ? (
            <button
              type="button"
              onClick={() => openRegistrationDraft("project")}
            >
              Registrar proyecto aquí
              <span>Crear un proyecto y guardarlo en este punto</span>
            </button>
          ) : null}
          {canLocateAssets ? (
            <button
              type="button"
              onClick={openAssetLocationDraft}
            >
              Ubicar activo existente aquí
              <span>Elegir un activo del inventario municipal</span>
            </button>
          ) : null}
          {!canCreateRequirements && !canCreateProjects && !canLocateAssets ? (
            <p className="map-context-menu-note">
              No tienes permisos para registrar ni ubicar elementos desde el mapa.
            </p>
          ) : null}
          <button
            type="button"
            onClick={focusContextPoint}
          >
            Solo centrar el mapa aquí
          </button>
          {mapContextMenu.nearestItem && mapContextMenu.nearestDistanceMeters !== null ? (
            <button type="button" onClick={selectNearestItem}>
              Ver elemento cercano
              <span>
                {mapContextMenu.nearestItem.title} ·{" "}
                {formatDistance(mapContextMenu.nearestDistanceMeters)}
              </span>
            </button>
          ) : null}
          <button
            type="button"
            className="map-context-menu-close"
            onClick={() => setMapContextMenu(null)}
          >
            Cerrar
          </button>
        </div>
      ) : null}

      {assetLocationDraft ? (
        <div className="map-registration-backdrop" role="presentation">
          <form
            aria-labelledby="map-asset-dialog-title"
            aria-modal="true"
            className="map-registration-dialog map-asset-dialog"
            onSubmit={handleLocateExistingAsset}
            role="dialog"
          >
            <div className="map-registration-head">
              <div>
                <p className="eyebrow">Inventario municipal</p>
                <h2 id="map-asset-dialog-title">Ubicar activo existente</h2>
              </div>
              <button
                type="button"
                className="secondary-button"
                onClick={() => setAssetLocationDraft(null)}
                disabled={isLocatingAsset}
              >
                Cerrar
              </button>
            </div>

            <p className="small-muted">
              Punto seleccionado: {formatCoordinates(
                assetLocationDraft.latitude,
                assetLocationDraft.longitude,
              )}
            </p>

            <div className="map-asset-selection">
              <label>
                Organización activa
                <select
                  value={assetLocationDraft.organizationId}
                  onChange={(event) => {
                    setAssetLocationDraft({
                      ...assetLocationDraft,
                      organizationId: event.target.value,
                      assetId: "",
                      label: "",
                      address: "",
                    });
                    setAssetSearch("");
                    setAssetLocationError("");
                  }}
                  disabled={isLocatingAsset}
                >
                  {activeAssetOrganizations.map((organization) => (
                    <option key={organization.id} value={organization.id}>
                      {organization.name}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                Buscar en el inventario
                <input
                  autoComplete="off"
                  maxLength={200}
                  placeholder="Nombre, código, descripción o material"
                  ref={assetSearchRef}
                  type="search"
                  value={assetSearch}
                  onChange={(event) => {
                    assetAbortControllerRef.current?.abort();
                    assetRequestSequenceRef.current += 1;
                    setAssetOptions([]);
                    setAssetTotal(0);
                    setIsLoadingAssets(true);
                    setAssetSearch(event.target.value);
                    setAssetLocationDraft({
                      ...assetLocationDraft,
                      assetId: "",
                    });
                    setAssetLoadError("");
                    setAssetLocationError("");
                  }}
                  disabled={isLocatingAsset}
                />
              </label>

              <label className="map-asset-picker">
                Activo municipal
                <select
                  aria-busy={isLoadingAssets}
                  aria-describedby={
                    assetLoadError ? undefined : "map-asset-results"
                  }
                  value={assetLocationDraft.assetId}
                  onChange={(event) => {
                    setAssetLocationDraft({
                      ...assetLocationDraft,
                      assetId: event.target.value,
                    });
                    setAssetLocationError("");
                  }}
                  disabled={isLoadingAssets || isLocatingAsset}
                  required
                >
                  <option value="">
                    {isLoadingAssets
                      ? "Cargando inventario…"
                      : "Selecciona un activo"}
                  </option>
                  {assetOptions.map((asset) => (
                    <option key={asset.id} value={asset.id}>
                      {asset.name} · {asset.asset_type.name} ·{" "}
                      {asset.location_id !== null
                        ? "Ya ubicado"
                        : "Sin ubicación"}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {!assetLoadError ? (
              <p className="small-muted" id="map-asset-results" role="status">
                {isLoadingAssets
                  ? "Buscando activos visibles…"
                  : assetTotal > assetOptions.length
                    ? `Mostrando ${assetOptions.length} de ${assetTotal} coincidencias. Acota la búsqueda para encontrar el activo que falta.`
                    : `${assetTotal} ${assetTotal === 1 ? "activo disponible" : "activos disponibles"}.`}
              </p>
            ) : null}
            {!isLoadingAssets && !assetLoadError && assetOptions.length === 0 ? (
              <p className="map-asset-empty">
                {assetSearch.trim()
                  ? "No hay activos que coincidan con esta búsqueda."
                  : "Esta organización no tiene activos no archivados disponibles."}
              </p>
            ) : null}
            {assetLoadError ? (
              <div className="map-asset-load-error">
                <p className="form-error" role="alert">
                  {assetLoadError}
                </p>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => {
                    setAssetLoadError("");
                    setAssetReloadVersion((current) => current + 1);
                  }}
                >
                  Reintentar carga
                </button>
              </div>
            ) : null}

            {selectedAsset ? (
              <div className="map-asset-summary">
                <div>
                  <strong>{selectedAsset.name}</strong>
                  <span>
                    {selectedAsset.asset_type.category.name} ·{" "}
                    {selectedAsset.asset_type.name}
                  </span>
                </div>
                <dl>
                  <div>
                    <dt>Estado</dt>
                    <dd>{formatStatus(selectedAsset.status)}</dd>
                  </div>
                  <div>
                    <dt>Ubicación actual</dt>
                    <dd>{selectedAsset.location?.label ?? "Sin ubicación"}</dd>
                  </div>
                </dl>
                {selectedAsset.location_id !== null ? (
                  <p className="map-asset-location-state">
                    Al guardar, este activo se recolocará en el punto
                    seleccionado.
                  </p>
                ) : null}
              </div>
            ) : null}

            <label>
              Etiqueta del punto <span className="small-muted">(opcional)</span>
              <input
                value={assetLocationDraft.label}
                onChange={(event) =>
                  setAssetLocationDraft({
                    ...assetLocationDraft,
                    label: event.target.value,
                  })
                }
                placeholder={
                  selectedAsset?.name ?? "Se usará el nombre del activo"
                }
                disabled={isLocatingAsset}
                maxLength={255}
              />
            </label>

            <label>
              Dirección <span className="small-muted">(opcional)</span>
              <input
                value={assetLocationDraft.address}
                onChange={(event) =>
                  setAssetLocationDraft({
                    ...assetLocationDraft,
                    address: event.target.value,
                  })
                }
                placeholder="Ej. Plaza Mayor, junto a la fuente"
                disabled={isLocatingAsset}
                maxLength={500}
              />
            </label>

            {assetLocationError ? (
              <p className="form-error" role="alert">
                {assetLocationError}
              </p>
            ) : null}

            <div className="button-row map-registration-actions">
              <button
                type="submit"
                disabled={
                  isLoadingAssets ||
                  isLocatingAsset ||
                  selectedAsset === null
                }
              >
                {isLocatingAsset
                  ? "Guardando ubicación…"
                  : selectedAsset !== null && selectedAsset.location_id !== null
                    ? "Recolocar activo"
                    : "Ubicar activo"}
              </button>
              <button
                type="button"
                className="secondary-button"
                onClick={() => setAssetLocationDraft(null)}
                disabled={isLocatingAsset}
              >
                Cancelar
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {registrationDraft ? (
        <div className="map-registration-backdrop" role="presentation">
          <form
            aria-labelledby="map-registration-dialog-title"
            aria-modal="true"
            className="map-registration-dialog"
            onSubmit={handleCreateMapRegistration}
            role="dialog"
          >
            <div className="map-registration-head">
              <div>
                <p className="eyebrow">Nuevo registro geolocalizado</p>
                <h2 id="map-registration-dialog-title">
                  {registrationDraft.kind === "requirement"
                    ? "Registrar necesidad aquí"
                    : "Registrar proyecto aquí"}
                </h2>
              </div>
              <button
                type="button"
                className="secondary-button"
                onClick={() => setRegistrationDraft(null)}
                disabled={isRegistering}
              >
                Cerrar
              </button>
            </div>

            <p className="small-muted">
              Punto seleccionado: {formatCoordinates(registrationDraft.latitude, registrationDraft.longitude)}
            </p>

            <label>
              Organización
              <select
                value={registrationDraft.organizationId}
                onChange={(event) =>
                  setRegistrationDraft({
                    ...registrationDraft,
                    organizationId: event.target.value,
                  })
                }
              >
                <option value="">Selecciona organización</option>
                {(user.organizations ?? []).map((organization) => (
                  <option key={organization.id} value={organization.id}>
                    {organization.name}
                  </option>
                ))}
              </select>
            </label>

            <label>
              {registrationDraft.kind === "requirement"
                ? "Título de la necesidad"
                : "Nombre del proyecto"}
              <input
                value={registrationDraft.title}
                onChange={(event) =>
                  setRegistrationDraft({
                    ...registrationDraft,
                    title: event.target.value,
                  })
                }
                placeholder={
                  registrationDraft.kind === "requirement"
                    ? "Ej. Reparar el acceso al consultorio"
                    : "Ej. Mejora del parque municipal"
                }
              />
            </label>

            <label>
              Descripción breve
              <textarea
                value={registrationDraft.description}
                onChange={(event) =>
                  setRegistrationDraft({
                    ...registrationDraft,
                    description: event.target.value,
                  })
                }
                placeholder="Añade el contexto principal para que quede claro qué ocurre en este punto."
                rows={4}
              />
            </label>

            {registrationError ? (
              <p className="form-error" role="alert">
                {registrationError}
              </p>
            ) : null}

            <div className="button-row map-registration-actions">
              <button type="submit" disabled={isRegistering}>
                {isRegistering ? "Registrando…" : "Registrar en el mapa"}
              </button>
              <button
                type="button"
                className="secondary-button"
                onClick={() => setRegistrationDraft(null)}
                disabled={isRegistering}
              >
                Cancelar
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </section>
  );
}
