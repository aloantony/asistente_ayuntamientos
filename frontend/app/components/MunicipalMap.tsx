"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  Circle,
  CircleMarker,
  ErrorEvent as LeafletLocationErrorEvent,
  LatLng,
  LatLngBounds,
  LeafletMouseEvent,
  LocationEvent,
  Map as LeafletMap,
  Marker,
  Rectangle,
  TileLayer,
} from "leaflet";
import {
  selectLocalBaseMapLayer,
  selectTopIdentifyLayer,
  tileCoordinatesForProjectedPoint,
  type SiurIdentifyPoint,
  type SiurMapLayer,
} from "../lib/referenceLayers";
import type { GeoMapItem } from "./types";

export type MapBounds = {
  north: number;
  south: number;
  east: number;
  west: number;
};

type MunicipalMapProps = {
  items: GeoMapItem[];
  focusLocation?: {
    latitude: number;
    longitude: number;
    label?: string;
  } | null;
  initialZoom?: number | null;
  selectedItemId?: string | null;
  markerColors?: Record<string, string>;
  baseLayerId?: number | null;
  fitRequest?: number;
  locateRequest?: number;
  areaSelectionEnabled?: boolean;
  areaBounds?: MapBounds | null;
  siurLayers?: SiurMapLayer[];
  onAreaSelectionChange?: (bounds: MapBounds | null) => void;
  onLocationError?: (message: string) => void;
  onSiurIdentify?: (point: SiurIdentifyPoint) => void;
  onSiurTileError?: (layerId: number, layerTitle: string) => void;
  onSiurTileLoad?: (layerId: number) => void;
  onSelectItem: (item: GeoMapItem) => void;
  onMapContextMenu?: (payload: {
    latitude: number;
    longitude: number;
    zoom: number;
    x: number;
    y: number;
  }) => void;
};

type LeafletModule = typeof import("leaflet");

type MarkerRecord = {
  item: GeoMapItem;
  marker: Marker;
};

type SiurTileLayerRecord = {
  layer: TileLayer;
  signature: string;
  url: string;
  errorNotified: boolean;
};

type AreaDragState = {
  startLatLng: LatLng;
  startX: number;
  startY: number;
  draftBounds: MapBounds;
  previousBounds: MapBounds | null;
};

// Where the map opens before anything is geolocated, which is how every new
// deployment starts. It used to be the provincial capital, eighty kilometres
// from the municipality being served and outside the cartography the local
// mirror holds, so a new town hall opened its map on a blank grid. Serving the
// municipality is the point of the product, so that is where it opens.
const FALLBACK_CENTER: [number, number] = [41.633, -3.583];
const FALLBACK_ZOOM = 14;
const SINGLE_ITEM_ZOOM = 16;
const MAX_MAP_ZOOM = 24;
const AREA_DRAG_THRESHOLD = 4;

function getItemKey(item: GeoMapItem) {
  return `${item.entity_type}-${item.entity_id}-${item.role}`;
}

function entityTypeLabel(item: GeoMapItem) {
  if (item.entity_type === "requirement") {
    return "Necesidad";
  }
  if (item.entity_type === "project") {
    return "Proyecto";
  }
  if (item.entity_type === "asset") {
    return "Activo municipal";
  }
  return "Elemento municipal";
}

function locationRoleLabel(item: GeoMapItem) {
  if (item.role === "primary") {
    return "Principal";
  }
  if (item.role === "affected_area") {
    return "Área afectada";
  }
  return "Referencia";
}

function isValidCoordinate(latitude: number, longitude: number) {
  return (
    Number.isFinite(latitude) &&
    Number.isFinite(longitude) &&
    latitude >= -90 &&
    latitude <= 90 &&
    longitude >= -180 &&
    longitude <= 180
  );
}

function isPointItem(item: GeoMapItem) {
  const { latitude, longitude } = item.location;
  return (
    item.location.geometry_type === "point" &&
    typeof latitude === "number" &&
    typeof longitude === "number" &&
    isValidCoordinate(latitude, longitude)
  );
}

function markerClassName(item: GeoMapItem, selectedItemId?: string | null) {
  const isSelected = selectedItemId === getItemKey(item);
  return [
    "municipal-map-marker",
    `municipal-map-marker--${item.entity_type}`,
    isSelected ? "municipal-map-marker--selected" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function normalizeZoom(zoom: number | null | undefined, fallback: number) {
  if (typeof zoom !== "number" || !Number.isFinite(zoom)) {
    return fallback;
  }
  return Math.min(MAX_MAP_ZOOM, Math.max(1, zoom));
}

function normalizeMapBounds(bounds: MapBounds | null | undefined) {
  if (!bounds) {
    return null;
  }

  const { east, north, south, west } = bounds;
  if (
    !isValidCoordinate(north, east) ||
    !isValidCoordinate(south, west) ||
    north < south ||
    east < west
  ) {
    return null;
  }

  return { east, north, south, west };
}

function boundsFromCorners(start: LatLng, end: LatLng): MapBounds {
  return {
    north: Math.max(start.lat, end.lat),
    south: Math.min(start.lat, end.lat),
    east: Math.max(start.lng, end.lng),
    west: Math.min(start.lng, end.lng),
  };
}

function safeMarkerColor(value: string | undefined) {
  if (!value) {
    return null;
  }

  const color = value.trim();
  if (color.length === 0 || color.length > 64 || /[;{}]/.test(color)) {
    return null;
  }

  const isHex = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(
    color,
  );
  const isNamedColor = /^[a-z]+$/i.test(color);
  const isSafeFunction =
    /^(?:rgb|rgba|hsl|hsla)\(\s*[-+.\d,%\s/]+\)$/i.test(color);
  if (!isHex && !isNamedColor && !isSafeFunction) {
    return null;
  }

  if (typeof CSS !== "undefined" && !CSS.supports("color", color)) {
    return null;
  }
  return color;
}

function resolveMarkerColor(
  item: GeoMapItem,
  markerColors: Record<string, string> | undefined,
) {
  if (!markerColors) {
    return null;
  }

  const candidate =
    markerColors[getItemKey(item)] ??
    markerColors[`${item.entity_type}:${item.status}`] ??
    markerColors[item.status] ??
    markerColors[item.entity_type];
  return safeMarkerColor(candidate);
}

function createMarkerIcon(
  L: LeafletModule,
  item: GeoMapItem,
  selectedItemId?: string | null,
) {
  return L.divIcon({
    className: markerClassName(item, selectedItemId),
    html: '<span aria-hidden="true"></span>',
    iconAnchor: [14, 14],
    iconSize: [28, 28],
    popupAnchor: [0, -14],
  });
}

function applyMarkerAppearance(
  L: LeafletModule,
  record: MarkerRecord,
  selectedItemId: string | null | undefined,
  markerColors: Record<string, string> | undefined,
) {
  record.marker.setIcon(createMarkerIcon(L, record.item, selectedItemId));
  const element = record.marker.getElement();
  if (element) {
    element.style.backgroundColor =
      resolveMarkerColor(record.item, markerColors) ?? "";
  }
}

function createItemPopup(item: GeoMapItem) {
  const content = document.createElement("div");
  const title = document.createElement("strong");
  const details = document.createElement("span");
  title.textContent = item.title;
  details.textContent = `${entityTypeLabel(item)} · ${locationRoleLabel(item)}`;
  content.append(title, document.createElement("br"), details);
  return content;
}

function createFocusPopup(label: string) {
  const content = document.createElement("span");
  content.textContent = label;
  return content;
}

function renderAreaRectangle(
  L: LeafletModule,
  map: LeafletMap,
  rectangle: Rectangle | null,
  bounds: MapBounds | null,
) {
  const normalizedBounds = normalizeMapBounds(bounds);
  if (!normalizedBounds) {
    rectangle?.remove();
    return null;
  }

  const leafletBounds = L.latLngBounds(
    [normalizedBounds.south, normalizedBounds.west],
    [normalizedBounds.north, normalizedBounds.east],
  );
  if (rectangle) {
    rectangle.setBounds(leafletBounds);
    return rectangle;
  }

  return L.rectangle(leafletBounds, {
    className: "municipal-map-area-selection",
    color: "#386641",
    dashArray: "7 5",
    fillColor: "#6a994e",
    fillOpacity: 0.14,
    interactive: false,
    weight: 2,
  }).addTo(map);
}

function copyBounds(bounds: LatLngBounds, L: LeafletModule) {
  if (!bounds.isValid()) {
    return L.latLngBounds([]);
  }
  return L.latLngBounds(bounds.getSouthWest(), bounds.getNorthEast());
}

function extendWithFocus(
  bounds: LatLngBounds,
  focusLocation:
    | { latitude: number; longitude: number; label?: string }
    | null
    | undefined,
) {
  if (
    focusLocation &&
    isValidCoordinate(focusLocation.latitude, focusLocation.longitude)
  ) {
    bounds.extend([focusLocation.latitude, focusLocation.longitude]);
  }
  return bounds;
}

function fitMapToBounds(
  map: LeafletMap,
  bounds: LatLngBounds,
  initialZoom: number | null | undefined,
) {
  if (!bounds.isValid()) {
    map.setView(FALLBACK_CENTER, FALLBACK_ZOOM);
    return;
  }

  const northEast = bounds.getNorthEast();
  const southWest = bounds.getSouthWest();
  const isSinglePoint =
    Math.abs(northEast.lat - southWest.lat) < 1e-9 &&
    Math.abs(northEast.lng - southWest.lng) < 1e-9;
  if (isSinglePoint) {
    map.setView(
      bounds.getCenter(),
      normalizeZoom(initialZoom, SINGLE_ITEM_ZOOM),
    );
    return;
  }

  map.fitBounds(bounds.pad(0.18), {
    animate: true,
    maxZoom: normalizeZoom(initialZoom, 15),
  });
}

function locationErrorMessage(error: LeafletLocationErrorEvent) {
  if (error.code === 1) {
    return "No se ha concedido permiso para acceder a tu ubicación.";
  }
  if (error.code === 2) {
    return "El dispositivo no pudo determinar tu ubicación.";
  }
  if (error.code === 3) {
    return "La búsqueda de tu ubicación ha tardado demasiado.";
  }
  return "No se pudo obtener tu ubicación.";
}

export function MunicipalMap({
  areaBounds,
  areaSelectionEnabled = false,
  baseLayerId = null,
  fitRequest,
  focusLocation,
  initialZoom,
  items,
  locateRequest,
  markerColors,
  selectedItemId,
  siurLayers = [],
  onAreaSelectionChange,
  onLocationError,
  onMapContextMenu,
  onSelectItem,
  onSiurIdentify,
  onSiurTileError,
  onSiurTileLoad,
}: MunicipalMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<LeafletMap | null>(null);
  const leafletRef = useRef<LeafletModule | null>(null);
  const tileLayerRef = useRef<TileLayer | null>(null);
  const baseTileLayerRecordRef = useRef<{
    layerId: number;
    signature: string;
    url: string;
    errorNotified: boolean;
  } | null>(null);
  const siurTileLayersRef = useRef<Map<number, SiurTileLayerRecord>>(new Map());
  const markerRecordsRef = useRef<Map<string, MarkerRecord>>(new Map());
  const markerBoundsRef = useRef<LatLngBounds | null>(null);
  const focusMarkerRef = useRef<CircleMarker | null>(null);
  const locationMarkerRef = useRef<CircleMarker | null>(null);
  const locationAccuracyRef = useRef<Circle | null>(null);
  const areaRectangleRef = useRef<Rectangle | null>(null);
  const areaDragStateRef = useRef<AreaDragState | null>(null);
  const cancelAreaDragRef = useRef<(() => void) | null>(null);
  const locateCleanupRef = useRef<(() => void) | null>(null);
  const areaSyncFrameRef = useRef<number | null>(null);
  const lastFitRequestRef = useRef<number | undefined>(fitRequest);
  const lastLocateRequestRef = useRef<number | undefined>(locateRequest);
  const [mapReady, setMapReady] = useState(false);
  const selectedBaseMap = useMemo(
    () => selectLocalBaseMapLayer(siurLayers, baseLayerId),
    [baseLayerId, siurLayers],
  );

  const focusLocationRef = useRef(focusLocation);
  const initialZoomRef = useRef(initialZoom);
  const markerColorsRef = useRef(markerColors);
  const selectedItemIdRef = useRef(selectedItemId);
  const areaSelectionEnabledRef = useRef(areaSelectionEnabled);
  const areaBoundsRef = useRef<MapBounds | null>(normalizeMapBounds(areaBounds));
  const onAreaSelectionChangeRef = useRef(onAreaSelectionChange);
  const onLocationErrorRef = useRef(onLocationError);
  const onMapContextMenuRef = useRef(onMapContextMenu);
  const onSelectItemRef = useRef(onSelectItem);
  const onSiurIdentifyRef = useRef(onSiurIdentify);
  const onSiurTileErrorRef = useRef(onSiurTileError);
  const onSiurTileLoadRef = useRef(onSiurTileLoad);
  const siurLayersRef = useRef(siurLayers);

  focusLocationRef.current = focusLocation;
  initialZoomRef.current = initialZoom;
  markerColorsRef.current = markerColors;
  selectedItemIdRef.current = selectedItemId;
  areaSelectionEnabledRef.current = areaSelectionEnabled;
  areaBoundsRef.current = normalizeMapBounds(areaBounds);
  onAreaSelectionChangeRef.current = onAreaSelectionChange;
  onLocationErrorRef.current = onLocationError;
  onMapContextMenuRef.current = onMapContextMenu;
  onSelectItemRef.current = onSelectItem;
  onSiurIdentifyRef.current = onSiurIdentify;
  onSiurTileErrorRef.current = onSiurTileError;
  onSiurTileLoadRef.current = onSiurTileLoad;
  siurLayersRef.current = siurLayers;

  const focusLatitude = focusLocation?.latitude;
  const focusLongitude = focusLocation?.longitude;
  const focusLabel = focusLocation?.label;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }

    let isActive = true;
    let resizeObserver: ResizeObserver | null = null;
    let resizeFrame: number | null = null;
    const markerRecords = markerRecordsRef.current;
    const siurTileLayers = siurTileLayersRef.current;

    async function initializeMap() {
      const L = await import("leaflet");
      const mapContainer = containerRef.current;
      if (!isActive || !mapContainer) {
        return;
      }

      const map = L.map(mapContainer, {
        center: FALLBACK_CENTER,
        maxZoom: MAX_MAP_ZOOM,
        preferCanvas: true,
        scrollWheelZoom: true,
        zoom: FALLBACK_ZOOM,
        zoomControl: false,
      });
      leafletRef.current = L;
      mapRef.current = map;
      markerBoundsRef.current = L.latLngBounds([]);

      const siurPane = map.createPane("siurPane");
      siurPane.style.zIndex = "250";
      siurPane.style.pointerEvents = "none";

      L.control.zoom({ position: "bottomleft" }).addTo(map);

      const handleContextMenu = (event: LeafletMouseEvent) => {
        const originalEvent = event.originalEvent as MouseEvent;
        onMapContextMenuRef.current?.({
          latitude: event.latlng.lat,
          longitude: event.latlng.lng,
          zoom: map.getZoom(),
          x: originalEvent.clientX,
          y: originalEvent.clientY,
        });
      };

      const handleMapClick = (event: LeafletMouseEvent) => {
        const originalTarget = (event.originalEvent as MouseEvent).target;
        if (
          areaSelectionEnabledRef.current ||
          areaDragStateRef.current ||
          (originalTarget instanceof Element &&
            originalTarget.closest(
              ".leaflet-control, .leaflet-marker-icon, .leaflet-popup",
            ))
        ) {
          return;
        }
        const zoom = map.getZoom();
        const latlng = map.wrapLatLng(event.latlng);
        const layer = selectTopIdentifyLayer(
          siurLayersRef.current,
          zoom,
          latlng.lat,
          latlng.lng,
        );
        if (!layer) {
          return;
        }
        const projected = map.project(latlng, zoom);
        const coordinates = tileCoordinatesForProjectedPoint(
          projected.x,
          projected.y,
          zoom,
        );
        onSiurIdentifyRef.current?.({
          layer,
          z: zoom,
          x: coordinates.x,
          y: coordinates.y,
          pixelX: coordinates.pixelX,
          pixelY: coordinates.pixelY,
        });
      };

      const removeAreaDocumentListeners = (
        handleMouseMove: (event: MouseEvent) => void,
        handleMouseUp: (event: MouseEvent) => void,
        handleWindowBlur: () => void,
      ) => {
        document.removeEventListener("mousemove", handleMouseMove);
        document.removeEventListener("mouseup", handleMouseUp);
        window.removeEventListener("blur", handleWindowBlur);
      };

      const latLngFromMouseEvent = (event: MouseEvent) => {
        const bounds = mapContainer.getBoundingClientRect();
        const x = Math.min(
          Math.max(event.clientX - bounds.left, 0),
          bounds.width,
        );
        const y = Math.min(
          Math.max(event.clientY - bounds.top, 0),
          bounds.height,
        );
        return map.containerPointToLatLng(L.point(x, y));
      };

      const finishAreaDrag = (commit: boolean, event?: MouseEvent) => {
        const dragState = areaDragStateRef.current;
        removeAreaDocumentListeners(
          handleMouseMove,
          handleMouseUp,
          handleWindowBlur,
        );
        areaDragStateRef.current = null;
        if (!dragState) {
          return;
        }

        let finalBounds = dragState.draftBounds;
        let movedDistance = 0;
        if (event) {
          const endLatLng = latLngFromMouseEvent(event);
          finalBounds = boundsFromCorners(dragState.startLatLng, endLatLng);
          movedDistance = Math.hypot(
            event.clientX - dragState.startX,
            event.clientY - dragState.startY,
          );
        }

        const shouldCommit = commit && movedDistance >= AREA_DRAG_THRESHOLD;
        areaRectangleRef.current = renderAreaRectangle(
          L,
          map,
          areaRectangleRef.current,
          shouldCommit ? finalBounds : dragState.previousBounds,
        );
        if (!shouldCommit) {
          return;
        }

        onAreaSelectionChangeRef.current?.(finalBounds);
        if (areaSyncFrameRef.current !== null) {
          cancelAnimationFrame(areaSyncFrameRef.current);
        }
        areaSyncFrameRef.current = requestAnimationFrame(() => {
          areaSyncFrameRef.current = null;
          if (!areaDragStateRef.current && mapRef.current === map) {
            areaRectangleRef.current = renderAreaRectangle(
              L,
              map,
              areaRectangleRef.current,
              areaBoundsRef.current,
            );
          }
        });
      };

      function handleMouseMove(event: MouseEvent) {
        const dragState = areaDragStateRef.current;
        if (!dragState) {
          return;
        }
        const endLatLng = latLngFromMouseEvent(event);
        dragState.draftBounds = boundsFromCorners(
          dragState.startLatLng,
          endLatLng,
        );
        areaRectangleRef.current = renderAreaRectangle(
          L,
          map,
          areaRectangleRef.current,
          dragState.draftBounds,
        );
      }

      function handleMouseUp(event: MouseEvent) {
        finishAreaDrag(true, event);
      }

      function handleWindowBlur() {
        finishAreaDrag(false);
      }

      const handleAreaMouseDown = (event: LeafletMouseEvent) => {
        const originalEvent = event.originalEvent as MouseEvent;
        if (!areaSelectionEnabledRef.current || originalEvent.button !== 0) {
          return;
        }
        if (
          originalEvent.target instanceof Element &&
          originalEvent.target.closest(".leaflet-control")
        ) {
          return;
        }

        originalEvent.preventDefault();
        originalEvent.stopPropagation();
        finishAreaDrag(false);
        map.closePopup();
        map.dragging.disable();
        const draftBounds = boundsFromCorners(event.latlng, event.latlng);
        areaDragStateRef.current = {
          draftBounds,
          previousBounds: areaBoundsRef.current,
          startLatLng: event.latlng,
          startX: originalEvent.clientX,
          startY: originalEvent.clientY,
        };
        areaRectangleRef.current = renderAreaRectangle(
          L,
          map,
          areaRectangleRef.current,
          draftBounds,
        );
        document.addEventListener("mousemove", handleMouseMove);
        document.addEventListener("mouseup", handleMouseUp);
        window.addEventListener("blur", handleWindowBlur);
      };

      cancelAreaDragRef.current = () => finishAreaDrag(false);
      map.on("click", handleMapClick);
      map.on("contextmenu", handleContextMenu);
      map.on("mousedown", handleAreaMouseDown);

      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(() => {
          if (resizeFrame !== null) {
            cancelAnimationFrame(resizeFrame);
          }
          resizeFrame = requestAnimationFrame(() => {
            resizeFrame = null;
            map.invalidateSize({ pan: false });
          });
        });
        resizeObserver.observe(mapContainer);
      }

      setMapReady(true);
    }

    void initializeMap();

    return () => {
      isActive = false;
      cancelAreaDragRef.current?.();
      cancelAreaDragRef.current = null;
      locateCleanupRef.current?.();
      locateCleanupRef.current = null;
      if (areaSyncFrameRef.current !== null) {
        cancelAnimationFrame(areaSyncFrameRef.current);
        areaSyncFrameRef.current = null;
      }
      if (resizeFrame !== null) {
        cancelAnimationFrame(resizeFrame);
      }
      resizeObserver?.disconnect();
      mapRef.current?.stopLocate();
      mapRef.current?.remove();
      mapRef.current = null;
      leafletRef.current = null;
      tileLayerRef.current = null;
      baseTileLayerRecordRef.current = null;
      siurTileLayers.clear();
      markerRecords.clear();
      markerBoundsRef.current = null;
      focusMarkerRef.current = null;
      locationMarkerRef.current = null;
      locationAccuracyRef.current = null;
      areaRectangleRef.current = null;
    };
  }, []);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !L || !map) {
      return;
    }

    if (!selectedBaseMap) {
      tileLayerRef.current?.remove();
      tileLayerRef.current = null;
      baseTileLayerRecordRef.current = null;
      return;
    }
    const signature = JSON.stringify([
      selectedBaseMap.layerId,
      selectedBaseMap.attribution,
      selectedBaseMap.minZoom,
      selectedBaseMap.maxZoom,
      selectedBaseMap.bounds,
    ]);
    const existing = baseTileLayerRecordRef.current;
    if (
      existing &&
      tileLayerRef.current &&
      existing.layerId === selectedBaseMap.layerId &&
      existing.signature === signature
    ) {
      if (existing.url !== selectedBaseMap.tileUrl) {
        tileLayerRef.current.setUrl(selectedBaseMap.tileUrl);
        existing.url = selectedBaseMap.tileUrl;
        existing.errorNotified = false;
      }
      return;
    }
    tileLayerRef.current?.remove();
    tileLayerRef.current = null;
    baseTileLayerRecordRef.current = null;
    const bounds = selectedBaseMap.bounds
      ? L.latLngBounds(
          [selectedBaseMap.bounds.south, selectedBaseMap.bounds.west],
          [selectedBaseMap.bounds.north, selectedBaseMap.bounds.east],
        )
      : undefined;
    const tileLayer = L.tileLayer(selectedBaseMap.tileUrl, {
      attribution: selectedBaseMap.attribution ?? undefined,
      bounds,
      maxZoom: selectedBaseMap.maxZoom ?? MAX_MAP_ZOOM,
      minZoom: selectedBaseMap.minZoom ?? 0,
    });
    const record = {
      layerId: selectedBaseMap.layerId,
      signature,
      url: selectedBaseMap.tileUrl,
      errorNotified: false,
    };
    tileLayer.on("tileerror", () => {
      if (!record.errorNotified) {
        record.errorNotified = true;
        onSiurTileErrorRef.current?.(
          selectedBaseMap.layerId,
          selectedBaseMap.title,
        );
      }
    });
    tileLayer.on("tileload", () => {
      record.errorNotified = false;
      onSiurTileLoadRef.current?.(selectedBaseMap.layerId);
    });
    tileLayerRef.current = tileLayer.addTo(map);
    baseTileLayerRecordRef.current = record;
    tileLayer.setZIndex(0);
  }, [mapReady, selectedBaseMap]);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !L || !map) {
      return;
    }

    const overlayLayers = siurLayers.filter((layer) => layer.role !== "base");
    const desiredIds = new Set(overlayLayers.map((layer) => layer.layerId));
    for (const [layerId, record] of siurTileLayersRef.current) {
      if (!desiredIds.has(layerId)) {
        record.layer.remove();
        siurTileLayersRef.current.delete(layerId);
      }
    }

    for (const descriptor of overlayLayers) {
      const signature = JSON.stringify([
        descriptor.attribution,
        descriptor.minZoom,
        descriptor.maxZoom,
        descriptor.bounds,
      ]);
      let record = siurTileLayersRef.current.get(descriptor.layerId);
      if (record && record.signature !== signature) {
        record.layer.remove();
        siurTileLayersRef.current.delete(descriptor.layerId);
        record = undefined;
      }
      if (!record) {
        const bounds = descriptor.bounds
          ? L.latLngBounds(
              [descriptor.bounds.south, descriptor.bounds.west],
              [descriptor.bounds.north, descriptor.bounds.east],
            )
          : undefined;
        const tileLayer = L.tileLayer(descriptor.tileUrl, {
          attribution: descriptor.attribution ?? undefined,
          bounds,
          maxZoom: descriptor.maxZoom ?? MAX_MAP_ZOOM,
          minZoom: descriptor.minZoom ?? 0,
          opacity: descriptor.opacity,
          pane: "siurPane",
        });
        record = {
          layer: tileLayer,
          signature,
          url: descriptor.tileUrl,
          errorNotified: false,
        };
        const tileRecord = record;
        tileLayer.on("tileerror", () => {
          if (!tileRecord.errorNotified) {
            tileRecord.errorNotified = true;
            onSiurTileErrorRef.current?.(
              descriptor.layerId,
              descriptor.title,
            );
          }
        });
        tileLayer.on("tileload", () => {
          tileRecord.errorNotified = false;
          onSiurTileLoadRef.current?.(descriptor.layerId);
        });
        siurTileLayersRef.current.set(descriptor.layerId, record);
      } else if (record.url !== descriptor.tileUrl) {
        record.layer.setUrl(descriptor.tileUrl);
        record.url = descriptor.tileUrl;
        record.errorNotified = false;
      }

      record.layer.setOpacity(descriptor.opacity);
      record.layer.setZIndex(descriptor.zIndex);
      if (descriptor.visible) {
        if (!map.hasLayer(record.layer)) {
          record.layer.addTo(map);
        }
      } else if (map.hasLayer(record.layer)) {
        record.layer.remove();
      }
    }
  }, [mapReady, siurLayers]);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !L || !map) {
      return;
    }

    markerRecordsRef.current.forEach(({ marker }) => marker.remove());
    markerRecordsRef.current.clear();
    const bounds = L.latLngBounds([]);

    items.filter(isPointItem).forEach((item) => {
      const latitude = item.location.latitude;
      const longitude = item.location.longitude;
      if (typeof latitude !== "number" || typeof longitude !== "number") {
        return;
      }

      const marker = L.marker([latitude, longitude], {
        alt: `${entityTypeLabel(item)}: ${item.title}`,
        icon: createMarkerIcon(L, item, selectedItemIdRef.current),
        keyboard: true,
        title: item.title,
      }).addTo(map);
      const record = { item, marker };
      marker.bindPopup(createItemPopup(item));
      marker.on("click", () => onSelectItemRef.current(item));
      markerRecordsRef.current.set(getItemKey(item), record);
      applyMarkerAppearance(
        L,
        record,
        selectedItemIdRef.current,
        markerColorsRef.current,
      );
      bounds.extend([latitude, longitude]);
    });

    markerBoundsRef.current = bounds;
    const currentFocus = focusLocationRef.current;
    if (
      !currentFocus ||
      !isValidCoordinate(currentFocus.latitude, currentFocus.longitude)
    ) {
      fitMapToBounds(map, bounds, initialZoomRef.current);
    }
  }, [items, mapReady]);

  useEffect(() => {
    const L = leafletRef.current;
    if (!mapReady || !L) {
      return;
    }
    markerRecordsRef.current.forEach((record) =>
      applyMarkerAppearance(L, record, selectedItemId, markerColorsRef.current),
    );
  }, [mapReady, selectedItemId]);

  useEffect(() => {
    const L = leafletRef.current;
    if (!mapReady || !L) {
      return;
    }
    markerRecordsRef.current.forEach((record) =>
      applyMarkerAppearance(L, record, selectedItemIdRef.current, markerColors),
    );
  }, [mapReady, markerColors]);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !L || !map) {
      return;
    }

    focusMarkerRef.current?.remove();
    focusMarkerRef.current = null;
    if (
      typeof focusLatitude === "number" &&
      typeof focusLongitude === "number" &&
      isValidCoordinate(focusLatitude, focusLongitude)
    ) {
      const coordinates: [number, number] = [focusLatitude, focusLongitude];
      focusMarkerRef.current = L.circleMarker(coordinates, {
        className: "municipal-map-focus-ring",
        color: "#3caf8c",
        fillColor: "#3caf8c",
        fillOpacity: 0.16,
        opacity: 0.92,
        radius: 22,
        weight: 4,
      })
        .addTo(map)
        .bindPopup(
          createFocusPopup(focusLabel || "Ubicación indicada"),
        );
      map.setView(coordinates, normalizeZoom(initialZoom, SINGLE_ITEM_ZOOM));
      return;
    }

    if (markerBoundsRef.current) {
      fitMapToBounds(map, markerBoundsRef.current, initialZoom);
    }
  }, [
    focusLabel,
    focusLatitude,
    focusLongitude,
    initialZoom,
    mapReady,
  ]);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !L || !map || fitRequest === lastFitRequestRef.current) {
      return;
    }
    lastFitRequestRef.current = fitRequest;
    if (fitRequest === undefined) {
      return;
    }

    const markerBounds = markerBoundsRef.current ?? L.latLngBounds([]);
    const allBounds = extendWithFocus(
      copyBounds(markerBounds, L),
      focusLocationRef.current,
    );
    fitMapToBounds(map, allBounds, initialZoomRef.current);
  }, [fitRequest, mapReady]);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (
      !mapReady ||
      !L ||
      !map ||
      locateRequest === lastLocateRequestRef.current
    ) {
      return;
    }
    lastLocateRequestRef.current = locateRequest;
    if (locateRequest === undefined) {
      return;
    }
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      onLocationErrorRef.current?.(
        "Este navegador no permite consultar la ubicación del dispositivo.",
      );
      return;
    }

    locateCleanupRef.current?.();
    let isCurrentRequest = true;
    const cleanup = () => {
      if (!isCurrentRequest) {
        return;
      }
      isCurrentRequest = false;
      map.off("locationfound", handleLocationFound);
      map.off("locationerror", handleLocationError);
      map.stopLocate();
      if (locateCleanupRef.current === cleanup) {
        locateCleanupRef.current = null;
      }
    };
    const handleLocationFound = (event: LocationEvent) => {
      if (!isCurrentRequest) {
        return;
      }
      const { lat, lng } = event.latlng;
      cleanup();
      if (!isValidCoordinate(lat, lng)) {
        onLocationErrorRef.current?.(
          "El dispositivo devolvió una ubicación no válida.",
        );
        return;
      }

      locationMarkerRef.current?.remove();
      locationAccuracyRef.current?.remove();
      locationAccuracyRef.current = L.circle(event.latlng, {
        color: "#2563eb",
        fillColor: "#60a5fa",
        fillOpacity: 0.08,
        interactive: false,
        radius: Math.max(event.accuracy, 1),
        weight: 1,
      }).addTo(map);
      locationMarkerRef.current = L.circleMarker(event.latlng, {
        color: "#ffffff",
        fillColor: "#2563eb",
        fillOpacity: 1,
        interactive: false,
        radius: 7,
        weight: 3,
      }).addTo(map);
      map.setView(event.latlng, Math.max(map.getZoom(), 16));
    };
    const handleLocationError = (error: LeafletLocationErrorEvent) => {
      if (!isCurrentRequest) {
        return;
      }
      cleanup();
      onLocationErrorRef.current?.(locationErrorMessage(error));
    };

    locateCleanupRef.current = cleanup;
    map.on("locationfound", handleLocationFound);
    map.on("locationerror", handleLocationError);
    map.locate({
      enableHighAccuracy: true,
      maximumAge: 30_000,
      setView: false,
      timeout: 12_000,
      watch: false,
    });

    return cleanup;
  }, [locateRequest, mapReady]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map) {
      return;
    }

    const container = map.getContainer();
    if (areaSelectionEnabled) {
      map.dragging.disable();
      map.boxZoom.disable();
      map.touchZoom.disable();
      container.style.cursor = "crosshair";
    } else {
      cancelAreaDragRef.current?.();
      map.dragging.enable();
      map.boxZoom.enable();
      map.touchZoom.enable();
      container.style.cursor = "";
    }

    return () => {
      cancelAreaDragRef.current?.();
      if (mapRef.current === map) {
        map.dragging.enable();
        map.boxZoom.enable();
        map.touchZoom.enable();
        container.style.cursor = "";
      }
    };
  }, [areaSelectionEnabled, mapReady]);

  useEffect(() => {
    const L = leafletRef.current;
    const map = mapRef.current;
    if (!mapReady || !L || !map || areaDragStateRef.current) {
      return;
    }

    areaRectangleRef.current = renderAreaRectangle(
      L,
      map,
      areaRectangleRef.current,
      areaBoundsRef.current,
    );
  }, [
    areaBounds?.east,
    areaBounds?.north,
    areaBounds?.south,
    areaBounds?.west,
    mapReady,
  ]);

  return (
    <div className="municipal-map-shell">
      <div
        aria-label="Mapa municipal con ubicaciones de necesidades, proyectos y activos"
        className="municipal-map-canvas"
        ref={containerRef}
        role="region"
      />
      {!selectedBaseMap ? (
        <p className="municipal-map-local-base-note" role="status">
          Fondo cartográfico local pendiente de sincronización.
        </p>
      ) : null}
    </div>
  );
}
