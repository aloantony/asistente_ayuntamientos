"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { adminRequest, ApiRequestError } from "../lib/api";
import {
  createEntityLocation,
  fetchGeoMapItems,
  fetchMunicipalAssets,
} from "../lib/geo";
import { useSession } from "../lib/session";
import { MunicipalMap } from "./MunicipalMap";
import type {
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

type EntityTypeFilter = "all" | GeoEntityType;
type CreatableMapEntityType = "requirement" | "project";

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

export function MapPanel({ user }: MapPanelProps) {
  const searchParams = useSearchParams();
  const { getStoredToken, handleRequestError } = useSession();
  const [entityType, setEntityType] = useState<EntityTypeFilter>("all");
  const [status, setStatus] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [items, setItems] = useState<GeoMapItem[]>([]);
  const [selectedItem, setSelectedItem] = useState<GeoMapItem | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [message, setMessage] = useState("");
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
  const assetAbortControllerRef = useRef<AbortController | null>(null);
  const assetSearchRef = useRef<HTMLInputElement | null>(null);
  const handleRequestErrorRef = useRef(handleRequestError);

  const canViewMap =
    userHasPermission(user, "map.view") || userHasPermission(user, "map.manage");
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
    if (entityType !== "all" && (entityType !== "asset" || canViewAssets)) {
      params.set("entity_type", entityType);
    }
    if (focusedEntityType && focusedEntityId !== null) {
      params.set("entity_type", focusedEntityType);
      params.set("entity_id", String(focusedEntityId));
    }
    if (status.trim()) {
      params.set("status", status.trim());
    }
    if (includeArchived) {
      params.set("include_archived", "true");
    }
    params.set("limit", "500");
    return params;
  }, [
    canViewAssets,
    entityType,
    focusedEntityId,
    focusedEntityType,
    includeArchived,
    status,
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

    fetchGeoMapItems(queryParams, controller.signal)
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

  const handleSelectItem = useCallback((item: GeoMapItem) => {
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

  const handleMapContextMenu = useCallback(
    (payload: {
      latitude: number;
      longitude: number;
      zoom: number;
      x: number;
      y: number;
    }) => {
      const nearest = items.reduce<{
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
    [items],
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

  if (!canViewMap) {
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
            Consulta necesidades, proyectos y activos con una ubicación
            municipal.
          </p>
        </div>
        <span className="map-count" aria-live="polite">
          {items.length} {items.length === 1 ? "elemento" : "elementos"}
        </span>
      </div>

      <div className="map-filters" aria-label="Filtros del mapa municipal">
        <label>
          Tipo
          <select
            value={
              entityType === "asset" && !canViewAssets ? "all" : entityType
            }
            onChange={(event) =>
              setEntityType(event.target.value as EntityTypeFilter)
            }
          >
            <option value="all">Todos</option>
            <option value="requirement">Necesidades</option>
            <option value="project">Proyectos</option>
            {canViewAssets ? (
              <option value="asset">Activos municipales</option>
            ) : null}
          </select>
        </label>
        <label>
          Estado
          <input
            placeholder="Ej. accepted, active..."
            type="search"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          />
        </label>
        <label className="checkbox-row map-archive-filter">
          <input
            checked={includeArchived}
            type="checkbox"
            onChange={(event) => setIncludeArchived(event.target.checked)}
          />
          Incluir archivados
        </label>
      </div>

      {message ? (
        <p className="map-message" role="status">
          {message}
        </p>
      ) : null}

      <div className="map-content-grid">
        <div className="map-main-column">
          <MunicipalMap
            focusLocation={
              manualFocusLocation ??
              explicitFocusLocation ??
              selectedFocusLocation
            }
            initialZoom={manualFocusZoom ?? focusedZoom}
            items={items}
            onMapContextMenu={handleMapContextMenu}
            onSelectItem={handleSelectItem}
            selectedItemId={selectedItem ? getItemKey(selectedItem) : null}
          />
        </div>

        <aside
          className="map-detail-card"
          aria-label="Detalle del marcador seleccionado"
        >
          {selectedItem ? (
            <>
              <span className="map-item-type">
                {entityTypeLabel(selectedItem.entity_type)}
              </span>
              <h2>{selectedItem.title}</h2>
              {selectedItem.subtitle ? <p>{selectedItem.subtitle}</p> : null}
              <dl>
                <div>
                  <dt>Estado</dt>
                  <dd>{formatStatus(selectedItem.status)}</dd>
                </div>
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
            </>
          ) : (
            <div className="map-detail-placeholder">
              <h2>Registra trabajo sobre el territorio</h2>
              <p>
                Haz click derecho en cualquier punto para crear una necesidad, un
                proyecto o ubicar un activo existente. Pulsa un marcador para
                consultar su detalle en el mapa.
              </p>
              {items.length === 0 ? (
                <p className="small-muted">
                  {isLoading
                    ? "Cargando ubicaciones…"
                    : "Aún no hay elementos ubicados, pero puedes registrar o ubicar el primero desde el mapa."}
                </p>
              ) : null}
            </div>
          )}
        </aside>
      </div>

      {mapContextMenu ? (
        <div
          className="map-context-menu"
          role="menu"
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
              role="menuitem"
              onClick={() => openRegistrationDraft("requirement")}
            >
              Registrar necesidad aquí
              <span>Crear una necesidad municipal con esta ubicación</span>
            </button>
          ) : null}
          {canCreateProjects ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => openRegistrationDraft("project")}
            >
              Registrar proyecto aquí
              <span>Crear un proyecto y guardarlo en este punto</span>
            </button>
          ) : null}
          {canLocateAssets ? (
            <button
              type="button"
              role="menuitem"
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
            role="menuitem"
            onClick={focusContextPoint}
          >
            Solo centrar el mapa aquí
          </button>
          {mapContextMenu.nearestItem && mapContextMenu.nearestDistanceMeters !== null ? (
            <button type="button" role="menuitem" onClick={selectNearestItem}>
              Ver elemento cercano
              <span>
                {mapContextMenu.nearestItem.title} ·{" "}
                {formatDistance(mapContextMenu.nearestDistanceMeters)}
              </span>
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
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
