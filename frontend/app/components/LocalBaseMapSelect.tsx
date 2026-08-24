"use client";

import {
  listLocalBaseMapLayers,
  type SiurMapLayer,
} from "../lib/referenceLayers";

type LocalBaseMapSelectProps = {
  layers: SiurMapLayer[];
  selectedLayerId: number | null;
  onSelect: (layerId: number | null) => void;
};

export function LocalBaseMapSelect({
  layers,
  onSelect,
  selectedLayerId,
}: LocalBaseMapSelectProps) {
  const baseLayers = listLocalBaseMapLayers(layers);
  const selectedValue =
    selectedLayerId !== null &&
    baseLayers.some((layer) => layer.layerId === selectedLayerId)
      ? String(selectedLayerId)
      : "";

  return (
    <label className="map-base-layer-control">
      Mapa base
      <select
        aria-label="Mapa base"
        onChange={(event) => {
          const value = event.target.value;
          if (!value) {
            onSelect(null);
            return;
          }
          const layerId = Number(value);
          onSelect(
            baseLayers.some((layer) => layer.layerId === layerId)
              ? layerId
              : null,
          );
        }}
        value={selectedValue}
      >
        <option value="">Sin fondo</option>
        {baseLayers.map((layer) => (
          <option key={layer.layerId} value={layer.layerId}>
            {layer.title}
          </option>
        ))}
      </select>
    </label>
  );
}
