"use client";

import { useEffect, useRef, useState } from "react";
import type {
  Circle,
  LayerGroup,
  CircleMarker,
  ErrorEvent as LeafletLocationErrorEvent,
  LatLng,
  LatLngBounds,
  LeafletMouseEvent,
  LocationEvent,
  Map as LeafletMap,
  Marker,
  Rectangle,
} from "leaflet";
import {
  limitesDe,
  mascaraExterior,
  nivelDetalle,
  vialVisible,
  type CartografiaPueblo,
  type NivelDetalle,
} from "../lib/pueblo";
import type { GeoMapItem } from "./types";
import styles from "./MunicipalMap.module.css";

export type MapBounds = {
  north: number;
  south: number;
  east: number;
  west: number;
};

export type DrawnGeometry = { type: "LineString" | "Polygon"; coordinates: number[][] | number[][][] };

type MunicipalMapProps = {
  onDrawPoint?: (longitude: number, latitude: number) => void;
  drawnGeometry?: DrawnGeometry | null;
  /** Plano del municipio servido: es el fondo, y el único que hay. */
  cartografia: CartografiaPueblo;
  items: GeoMapItem[];
  focusLocation?: {
    latitude: number;
    longitude: number;
    label?: string;
  } | null;
  initialZoom?: number | null;
  selectedItemId?: string | null;
  markerColors?: Record<string, string>;
  fitRequest?: number;
  locateRequest?: number;
  areaSelectionEnabled?: boolean;
  areaBounds?: MapBounds | null;
  onAreaSelectionChange?: (bounds: MapBounds | null) => void;
  onLocationError?: (message: string) => void;
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

type AreaDragState = {
  startLatLng: LatLng;
  startX: number;
  startY: number;
  draftBounds: MapBounds;
  previousBounds: MapBounds | null;
};

// El mapa abre sobre el término municipal, que es su propio fondo. Estas
// constantes sólo sirven para el instante entre crear el mapa y encajarlo.
const FALLBACK_CENTER: [number, number] = [41.633, -3.583];
const FALLBACK_ZOOM = 14;
const SINGLE_ITEM_ZOOM = 16;
// Hasta dónde se deja ampliar. Veinticuatro venían de cuando el fondo eran
// teselas y sobraban: pasado el zoom nativo del archivo sólo se agrandaba la
// misma imagen hasta que el nombre del pueblo ocupaba la pantalla (ADR-061).
// El plano es vectorial y no se emborrona, pero por encima de 19 un edificio ya
// llena el panel y no queda nada nuevo que enseñar.
const MAX_MAP_ZOOM = 19;
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
  fallbackBounds?: LatLngBounds | null,
) {
  if (!bounds.isValid()) {
    // Sin nada situado todavía, lo que hay que enseñar es el pueblo.
    if (fallbackBounds?.isValid()) {
      map.fitBounds(fallbackBounds, { padding: [16, 16] });
      return;
    }
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

/**
 * Dibuja el municipio: el término como lienzo, sus usos del suelo, aguas,
 * viales y edificios, y encima una máscara que tapa cuanto sobresale del
 * límite.
 *
 * Los datos de OSM llegan con las geometrías enteras —una carretera no se
 * corta en el mojón— y recortarlas contra un límite cóncavo es trabajo fino y
 * frágil. Pintar por encima un rectángulo con el término como agujero da el
 * borde exacto sin tocar los datos; Leaflet rellena con `evenodd`, así que los
 * anillos interiores son agujeros sin depender del sentido de giro.
 *
 * El color no está aquí: cada capa lleva su clase y la hoja de estilos decide,
 * de modo que el modo oscuro sale de los tokens de la aplicación.
 */
function dibujarPueblo(
  L: LeafletModule,
  map: LeafletMap,
  cartografia: CartografiaPueblo,
  alCambiarNivel: (nivel: NivelDetalle) => void,
) {
  const datos = (valor: unknown) => valor as unknown as GeoJSON.GeoJsonObject;
  const pane = (nombre: string, orden: number) => {
    map.createPane(nombre).style.zIndex = String(orden);
    return nombre;
  };

  L.geoJSON(datos(cartografia.limite), {
    interactive: false,
    pane: pane("pueblo-termino", 180),
    style: { className: styles.termino },
  }).addTo(map);

  L.geoJSON(datos(cartografia.verde), {
    interactive: false,
    pane: pane("pueblo-suelo", 190),
    style: (feature) => ({
      className: [
        styles.suelo,
        styles[`suelo-${String(feature?.properties?.tipo)}`] ?? "",
      ]
        .filter(Boolean)
        .join(" "),
    }),
  }).addTo(map);

  L.geoJSON(datos(cartografia.agua), {
    interactive: false,
    pane: pane("pueblo-agua", 200),
    style: { className: styles.agua },
  }).addTo(map);

  // Qué viales se dibujan depende del zoom: de lejos la red principal, de
  // cerca hasta los caminos. El filtro de Leaflet sólo actúa al añadir datos,
  // así que la capa se rehace cuando se cruza un umbral.
  let nivel = nivelDetalle(map.getZoom());
  const viales = L.geoJSON(datos(cartografia.viales), {
    interactive: false,
    pane: pane("pueblo-viales", 210),
    style: (feature) => ({
      className: [
        styles.vial,
        styles[`vial-${String(feature?.properties?.clase)}`] ?? "",
      ]
        .filter(Boolean)
        .join(" "),
    }),
    filter: (feature) => vialVisible(feature?.properties?.clase, nivel),
  }).addTo(map);

  L.geoJSON(datos(cartografia.edificios), {
    interactive: false,
    pane: pane("pueblo-edificios", 220),
    style: { className: styles.edificio },
  }).addTo(map);

  L.geoJSON(datos(mascaraExterior(cartografia.limite)), {
    interactive: false,
    pane: pane("pueblo-mascara", 230),
    style: { className: styles.mascara },
  }).addTo(map);

  L.geoJSON(datos(cartografia.limite), {
    interactive: false,
    pane: pane("pueblo-borde", 240),
    style: { className: styles.borde },
  }).addTo(map);

  alCambiarNivel(nivel);
  map.on("zoomend", () => {
    const siguiente = nivelDetalle(map.getZoom());
    if (siguiente === nivel) {
      return;
    }
    nivel = siguiente;
    alCambiarNivel(siguiente);
    viales.clearLayers();
    viales.addData(datos(cartografia.viales));
  });
}

export function MunicipalMap({
  areaBounds,
  areaSelectionEnabled = false,
  cartografia,
  fitRequest,
  focusLocation,
  initialZoom,
  items,
  locateRequest,
  markerColors,
  selectedItemId,
  onAreaSelectionChange,
  onLocationError,
  onMapContextMenu,
  onDrawPoint,
  drawnGeometry,
  onSelectItem,
}: MunicipalMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<LeafletMap | null>(null);
  const leafletRef = useRef<LeafletModule | null>(null);
  const terminoBoundsRef = useRef<LatLngBounds | null>(null);
  const markerRecordsRef = useRef<Map<string, MarkerRecord>>(new Map());
  const shapeLayerRef = useRef<LayerGroup | null>(null);
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
  const [nivel, setNivel] = useState<NivelDetalle>("pueblo");

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

    async function initializeMap() {
      const L = await import("leaflet");
      const mapContainer = containerRef.current;
      if (!isActive || !mapContainer) {
        return;
      }

      const map = L.map(mapContainer, {
        center: FALLBACK_CENTER,
        maxZoom: MAX_MAP_ZOOM,
        // El dibujo del pueblo se pinta como SVG y no como lienzo: así cada
        // capa lleva su clase y es la hoja de estilos la que decide el color,
        // con el modo oscuro heredado de los tokens de la aplicación.
        preferCanvas: false,
        scrollWheelZoom: true,
        zoom: FALLBACK_ZOOM,
        zoomControl: false,
      });
      leafletRef.current = L;
      mapRef.current = map;
      markerBoundsRef.current = L.latLngBounds([]);

      dibujarPueblo(L, map, cartografia, setNivel);
      terminoBoundsRef.current = L.latLngBounds(limitesDe(cartografia.limite));
      map.setMaxBounds(terminoBoundsRef.current.pad(0.25));
      map.options.maxBoundsViscosity = 0.9;

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
      terminoBoundsRef.current = null;
      markerRecords.clear();
      markerBoundsRef.current = null;
      focusMarkerRef.current = null;
      locationMarkerRef.current = null;
      locationAccuracyRef.current = null;
      areaRectangleRef.current = null;
    };
  }, [cartografia]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map || !onDrawPoint) return;
    const draw = (event: LeafletMouseEvent) => onDrawPoint(event.latlng.lng, event.latlng.lat);
    map.on("click", draw);
    return () => { map.off("click", draw); };
  }, [mapReady, onDrawPoint]);

  useEffect(() => {
    const map = mapRef.current;
    const L = leafletRef.current;
    if (!mapReady || !map || !L) return;
    shapeLayerRef.current?.remove();
    const layer = L.layerGroup().addTo(map);
    shapeLayerRef.current = layer;
    for (const item of items.filter((entry) => entry.location.geometry_type !== "point")) {
      try {
        const geometry = JSON.parse(item.location.geometry_json);
        const shape = L.geoJSON(geometry, { style: {
          color: markerColors?.[getItemKey(item)] || item.layer_color || "#2f74d0",
          weight: selectedItemId === getItemKey(item) ? 5 : 3, fillOpacity: 0.2,
        }}).addTo(layer);
        shape.bindPopup(createItemPopup(item));
        shape.on("click", () => onSelectItemRef.current(item));
      } catch { /* A malformed legacy geometry does not hide valid records. */ }
    }
    if (drawnGeometry) {
      L.geoJSON(drawnGeometry as GeoJSON.Geometry, { style: { color: "#c0603a", dashArray: "6 4", weight: 3 } }).addTo(layer);
    }
    return () => { layer.remove(); };
  }, [items, selectedItemId, markerColors, mapReady, drawnGeometry]);

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

    for (const item of items.filter((entry) => entry.location.geometry_type !== "point")) {
      try {
        const shapeBounds = L.geoJSON(JSON.parse(item.location.geometry_json)).getBounds();
        if (shapeBounds.isValid()) bounds.extend(shapeBounds);
      } catch { /* Ignore malformed legacy shapes when framing valid records. */ }
    }
    markerBoundsRef.current = bounds;
    const currentFocus = focusLocationRef.current;
    if (
      !currentFocus ||
      !isValidCoordinate(currentFocus.latitude, currentFocus.longitude)
    ) {
      fitMapToBounds(
        map,
        bounds,
        initialZoomRef.current,
        terminoBoundsRef.current,
      );
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
      const selectedShape = items.find((item) => getItemKey(item) === selectedItemId && item.location.geometry_type !== "point");
      if (selectedShape) {
        try {
          const shapeBounds = L.geoJSON(JSON.parse(selectedShape.location.geometry_json)).getBounds();
          if (shapeBounds.isValid()) {
            map.fitBounds(shapeBounds, { paddingTopLeft: [24, 64], paddingBottomRight: [24, 150], maxZoom: 18 });
            return;
          }
        } catch { /* Fall back to the recorded anchor for legacy geometry. */ }
      }
      map.setView(coordinates, normalizeZoom(initialZoom, SINGLE_ITEM_ZOOM));
      return;
    }

    if (markerBoundsRef.current) {
      fitMapToBounds(
        map,
        markerBoundsRef.current,
        initialZoom,
        terminoBoundsRef.current,
      );
    }
  }, [
    items,
    selectedItemId,
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
    fitMapToBounds(
      map,
      allBounds,
      initialZoomRef.current,
      terminoBoundsRef.current,
    );
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
        aria-label={`Mapa de ${cartografia.municipio} con las ubicaciones de necesidades, proyectos y activos`}
        className={`municipal-map-canvas ${styles.mapa}`}
        data-detalle={nivel}
        ref={containerRef}
        role="region"
      />
    </div>
  );
}
