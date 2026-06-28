"use client";

import { useEffect, useRef } from "react";
import type { GeoMapItem } from "./types";

type MunicipalMapProps = {
  items: GeoMapItem[];
  focusLocation?: {
    latitude: number;
    longitude: number;
    label?: string;
  } | null;
  initialZoom?: number | null;
  selectedItemId?: string | null;
  onSelectItem: (item: GeoMapItem) => void;
};

const FALLBACK_CENTER: [number, number] = [42.3439, -3.6969];
const FALLBACK_ZOOM = 12;

function getItemKey(item: GeoMapItem) {
  return `${item.entity_type}-${item.entity_id}`;
}

function isPointItem(item: GeoMapItem) {
  return (
    item.location.geometry_type === "point" &&
    typeof item.location.latitude === "number" &&
    typeof item.location.longitude === "number"
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

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function MunicipalMap({
  focusLocation,
  initialZoom,
  items,
  selectedItemId,
  onSelectItem,
}: MunicipalMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }

    let isActive = true;
    let map: import("leaflet").Map | null = null;

    async function renderMap() {
      const L = await import("leaflet");
      if (!isActive || !containerRef.current) {
        return;
      }

      const pointItems = items.filter(isPointItem);
      map = L.map(containerRef.current, {
        center: FALLBACK_CENTER,
        preferCanvas: true,
        scrollWheelZoom: true,
        zoom: FALLBACK_ZOOM,
        zoomControl: false,
      });

      L.control.zoom({ position: "bottomleft" }).addTo(map);

      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution:
          '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19,
      }).addTo(map);

      const bounds = L.latLngBounds([]);

      pointItems.forEach((item) => {
        const latitude = item.location.latitude;
        const longitude = item.location.longitude;
        if (typeof latitude !== "number" || typeof longitude !== "number") {
          return;
        }

        const coordinates: [number, number] = [latitude, longitude];
        const marker = L.marker(coordinates, {
          icon: L.divIcon({
            className: markerClassName(item, selectedItemId),
            html: `<span aria-hidden="true"></span>`,
            iconAnchor: [14, 14],
            iconSize: [28, 28],
            popupAnchor: [0, -14],
          }),
          title: item.title,
        }).addTo(map!);

        marker.bindPopup(
          `<strong>${escapeHtml(item.title)}</strong><br><span>${escapeHtml(
            item.entity_type === "requirement" ? "Necesidad" : "Proyecto",
          )}</span>`,
        );
        marker.on("click", () => onSelectItem(item));
        bounds.extend(coordinates);
      });

      if (focusLocation) {
        const focusCoordinates: [number, number] = [
          focusLocation.latitude,
          focusLocation.longitude,
        ];
        L.circleMarker(focusCoordinates, {
          className: "municipal-map-focus-ring",
          color: "transparent",
          fillColor: "transparent",
          radius: 22,
          weight: 0,
        })
          .addTo(map)
          .bindPopup(escapeHtml(focusLocation.label || "Ubicación indicada"));
        bounds.extend(focusCoordinates);
        map.setView(focusCoordinates, initialZoom ?? 16);
      } else if (pointItems.length === 1 && bounds.isValid()) {
        map.setView(bounds.getCenter(), initialZoom ?? 16);
      } else if (pointItems.length > 0 && bounds.isValid()) {
        map.fitBounds(bounds.pad(0.18), { maxZoom: initialZoom ?? 15 });
      }
    }

    void renderMap();

    return () => {
      isActive = false;
      map?.remove();
    };
  }, [focusLocation, initialZoom, items, onSelectItem, selectedItemId]);

  return (
    <div className="municipal-map-shell">
      <div
        aria-label="Mapa municipal con ubicaciones de necesidades y proyectos"
        className="municipal-map-canvas"
        ref={containerRef}
        role="region"
      />
    </div>
  );
}
