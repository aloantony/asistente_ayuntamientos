"use client";

import { CircleAlert, Layers, MapPin, RefreshCw, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { fetchAllGeoMapItems } from "../../lib/geo";
import {
  defaultLocalBaseMapSelection,
  fetchReferenceCatalog,
  type SiurMapLayer,
} from "../../lib/referenceLayers";
import { MunicipalMap } from "../MunicipalMap";
import workspaceStyles from "../MunicipalWorkspace.module.css";
import type { GeoMapItem } from "../types";
import styles from "./MapaGeneral.module.css";
import { ResourceState } from "./shared";

/**
 * Árbol de capas construido desde los propios elementos.
 *
 * El backend ya agrupa cada elemento en su capa (`layer_key`, `layer_label`,
 * `layer_color`), así que la pantalla no necesita un catálogo aparte que
 * mantener sincronizado: si una capa no tiene nada dentro, no existe en el
 * árbol, que es lo que un mapa debería enseñar.
 */
export type MapLayer = {
  key: string;
  label: string;
  color: string;
  count: number;
};

export function buildLayers(items: GeoMapItem[]): MapLayer[] {
  const layers = new Map<string, MapLayer>();
  for (const item of items) {
    const existing = layers.get(item.layer_key);
    if (existing) {
      existing.count += 1;
      continue;
    }
    layers.set(item.layer_key, {
      key: item.layer_key,
      label: item.layer_label,
      color: item.layer_color,
      count: 1,
    });
  }
  return [...layers.values()].sort((left, right) =>
    left.label.localeCompare(right.label, "es"),
  );
}

/** Clave estable de un elemento: el par entidad/id, no el índice de la lista. */
export function itemKey(item: GeoMapItem) {
  return `${item.entity_type}:${item.entity_id}`;
}

export function matchesSearch(item: GeoMapItem, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) {
    return true;
  }
  return (
    item.title.toLowerCase().includes(needle) ||
    (item.subtitle ?? "").toLowerCase().includes(needle) ||
    item.layer_label.toLowerCase().includes(needle)
  );
}

export function MapaGeneral({
  organizationId,
  canViewMap,
}: {
  organizationId: number;
  canViewMap: boolean;
}) {
  const [items, setItems] = useState<GeoMapItem[]>([]);
  const [hiddenLayers, setHiddenLayers] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [baseMapLayers, setBaseMapLayers] = useState<SiurMapLayer[]>([]);
  const [baseLayerId, setBaseLayerId] = useState<number | null>(null);

  useEffect(() => {
    if (!canViewMap) {
      setItems([]);
      return;
    }

    const controller = new AbortController();
    setIsLoading(true);
    setError("");

    const params = new URLSearchParams({
      organization_id: String(organizationId),
    });
    fetchAllGeoMapItems(params, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted) {
          setItems(loaded);
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(
            reason instanceof Error
              ? reason.message
              : "No se pudo cargar el mapa municipal.",
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoading(false);
        }
      });

    return () => controller.abort();
  }, [organizationId, canViewMap, loadAttempt]);

  // El fondo del mapa lo sirve el espejo cartográfico, igual que en `/mapa`.
  // Esta pantalla no elige mapa base ni lo recuerda: le basta con el que el
  // catálogo trae por omisión. Si el catálogo no está disponible, el mapa se
  // queda sin fondo pero los elementos municipales se siguen viendo, así que el
  // fallo no tumba la pantalla; sólo se anuncia debajo.
  useEffect(() => {
    if (!canViewMap) {
      setBaseMapLayers([]);
      setBaseLayerId(null);
      return;
    }

    const controller = new AbortController();
    fetchReferenceCatalog(organizationId, "", controller.signal)
      .then((catalog) => {
        if (controller.signal.aborted) {
          return;
        }
        const selection = defaultLocalBaseMapSelection(catalog);
        setBaseMapLayers(selection.layers);
        setBaseLayerId(selection.baseLayerId);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setBaseMapLayers([]);
          setBaseLayerId(null);
        }
      });

    return () => controller.abort();
  }, [organizationId, canViewMap, loadAttempt]);

  const layers = useMemo(() => buildLayers(items), [items]);
  const markerColors = useMemo(
    () =>
      Object.fromEntries(layers.map((layer) => [layer.key, layer.color])),
    [layers],
  );
  const visibleItems = useMemo(
    () =>
      items.filter(
        (item) =>
          !hiddenLayers.has(item.layer_key) && matchesSearch(item, search),
      ),
    [items, hiddenLayers, search],
  );
  const selectedItem =
    visibleItems.find((item) => itemKey(item) === selectedId) ?? null;

  function toggleLayer(key: string) {
    setHiddenLayers((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  if (!canViewMap) {
    return (
      <div className={workspaceStyles.tabContent}>
        <ResourceState
          description="Tu cuenta no dispone del permiso map.view en esta organización."
          icon={ShieldCheck}
          title="Mapa no autorizado"
          tone="restricted"
        />
      </div>
    );
  }

  return (
    <div className={workspaceStyles.tabContent}>
      {error ? (
        <ResourceState
          action={
            <button
              className={workspaceStyles.secondaryAction}
              onClick={() => setLoadAttempt((value) => value + 1)}
              type="button"
            >
              Reintentar
            </button>
          }
          description={error}
          icon={CircleAlert}
          title="Mapa no disponible"
          tone="error"
        />
      ) : isLoading ? (
        <div
          aria-busy="true"
          aria-live="polite"
          className={workspaceStyles.loadingState}
          role="status"
        >
          <RefreshCw aria-hidden="true" size={22} />
          <div>
            <strong>Cargando el mapa municipal</strong>
            <span>Situando el inventario…</span>
          </div>
        </div>
      ) : (
        <div className={styles.layout}>
          <aside className={styles.sidebar}>
            <label className={styles.search}>
              <span className="sr-only">Buscar en el mapa</span>
              <input
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Buscar en el mapa…"
                type="search"
                value={search}
              />
            </label>

            <section className={styles.tree}>
              <h3>
                <Layers aria-hidden="true" size={15} strokeWidth={1.7} />
                Capas
              </h3>
              {layers.length === 0 ? (
                <p className={styles.empty}>
                  Todavía no hay nada situado en el mapa. Los elementos del
                  inventario aparecen aquí en cuanto tienen ubicación.
                </p>
              ) : (
                <ul>
                  {layers.map((layer) => (
                    <li key={layer.key}>
                      <label>
                        <input
                          checked={!hiddenLayers.has(layer.key)}
                          onChange={() => toggleLayer(layer.key)}
                          type="checkbox"
                        />
                        <span
                          aria-hidden="true"
                          className={styles.swatch}
                          style={{ background: layer.color }}
                        />
                        <span className={styles.layerLabel}>{layer.label}</span>
                        <span className={styles.layerCount}>{layer.count}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {selectedItem ? (
              <section className={styles.card}>
                <h3>
                  <MapPin aria-hidden="true" size={15} strokeWidth={1.7} />
                  {selectedItem.title}
                </h3>
                {selectedItem.subtitle ? (
                  <p>{selectedItem.subtitle}</p>
                ) : null}
                <dl>
                  <div>
                    <dt>Capa</dt>
                    <dd>{selectedItem.layer_label}</dd>
                  </div>
                  {selectedItem.item_type ? (
                    <div>
                      <dt>Tipo</dt>
                      <dd>{selectedItem.item_type}</dd>
                    </div>
                  ) : null}
                  <div>
                    <dt>Estado</dt>
                    <dd>{selectedItem.status}</dd>
                  </div>
                </dl>
              </section>
            ) : null}
          </aside>

          <div className={styles.map}>
            <MunicipalMap
              items={visibleItems}
              markerColors={markerColors}
              siurLayers={baseMapLayers}
              baseLayerId={baseLayerId}
              onSelectItem={(item) => setSelectedId(itemKey(item))}
              selectedItemId={selectedId}
            />
            <p className={styles.counter}>
              {visibleItems.length} de {items.length} elementos
              {hiddenLayers.size > 0 || search.trim()
                ? " (filtrados)"
                : ""}
            </p>
            {baseLayerId === null ? (
              <p className={styles.counter}>
                Sin fondo cartográfico disponible
              </p>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
