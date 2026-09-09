"use client";

import {
  CircleAlert,
  Focus,
  Layers,
  Maximize2,
  Minimize2,
  MapPin,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { fetchAllGeoMapItems } from "../../lib/geo";
import {
  buscarPueblo,
  cargarCartografia,
  type CartografiaPueblo,
  type MunicipioIdentificable,
} from "../../lib/pueblo";
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
  municipality,
  canViewMap,
}: {
  organizationId: number;
  municipality: MunicipioIdentificable | null;
  canViewMap: boolean;
}) {
  const [items, setItems] = useState<GeoMapItem[]>([]);
  const [cartografia, setCartografia] = useState<CartografiaPueblo | null>(
    null,
  );
  const [hiddenLayers, setHiddenLayers] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [fitRequest, setFitRequest] = useState(0);
  const [areLayersOpen, setAreLayersOpen] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const layersPanelId = useId();
  const layersButtonRef = useRef<HTMLButtonElement | null>(null);
  const layersCloseRef = useRef<HTMLButtonElement | null>(null);
  const mapWorkspaceRef = useRef<HTMLDivElement | null>(null);

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

  // Cartografía del pueblo. Es un fichero estático que viaja con la
  // aplicación, así que se pide aparte del inventario: si el municipio todavía
  // no tiene mapa preparado, el resto de la pantalla sigue funcionando.
  const pueblo = useMemo(() => buscarPueblo(municipality), [municipality]);

  useEffect(() => {
    if (!pueblo) {
      setCartografia(null);
      return;
    }

    let cancelado = false;
    cargarCartografia(pueblo)
      .then((cargada) => {
        if (!cancelado) {
          setCartografia(cargada);
        }
      })
      .catch(() => {
        if (!cancelado) {
          setCartografia(null);
        }
      });

    return () => {
      cancelado = true;
    };
  }, [pueblo, loadAttempt]);

  useEffect(() => {
    function handleFullscreenChange() {
      setIsFullscreen(document.fullscreenElement === mapWorkspaceRef.current);
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () =>
      document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  useEffect(() => {
    if (!areLayersOpen) {
      return;
    }

    const focusFrame = window.requestAnimationFrame(() =>
      layersCloseRef.current?.focus(),
    );
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setAreLayersOpen(false);
        window.requestAnimationFrame(() => layersButtonRef.current?.focus());
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [areLayersOpen]);

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

  function closeLayers() {
    setAreLayersOpen(false);
    window.requestAnimationFrame(() => layersButtonRef.current?.focus());
  }

  function clearFilters() {
    setSearch("");
    setHiddenLayers(new Set());
  }

  async function toggleFullscreen() {
    if (document.fullscreenElement === mapWorkspaceRef.current) {
      await document.exitFullscreen();
      return;
    }
    await mapWorkspaceRef.current?.requestFullscreen();
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
        <div className={styles.mapWorkspace} ref={mapWorkspaceRef}>
          <header className={styles.header}>
            <div>
              <p className="eyebrow">Mapa municipal</p>
              <h2>{municipality?.name ?? "Municipio seleccionado"}</h2>
              <p>
                Consulta el plano local y los elementos situados por el
                ayuntamiento.
              </p>
            </div>
            <div className={styles.actions}>
              <button
                disabled={!cartografia}
                onClick={() => setFitRequest((value) => value + 1)}
                type="button"
              >
                <Focus aria-hidden="true" size={16} />
                Centrar municipio
              </button>
              <button onClick={() => void toggleFullscreen()} type="button">
                {isFullscreen ? (
                  <Minimize2 aria-hidden="true" size={16} />
                ) : (
                  <Maximize2 aria-hidden="true" size={16} />
                )}
                {isFullscreen ? "Salir de pantalla completa" : "Pantalla completa"}
              </button>
              <button
                aria-controls={layersPanelId}
                aria-expanded={areLayersOpen}
                className={styles.layersToggle}
                onClick={() => setAreLayersOpen((open) => !open)}
                ref={layersButtonRef}
                type="button"
              >
                <SlidersHorizontal aria-hidden="true" size={16} />
                Capas y filtros
              </button>
            </div>
          </header>

          <div className={styles.layout}>
            {areLayersOpen ? (
              <button
                aria-label="Cerrar capas y filtros"
                className={styles.scrim}
                onClick={closeLayers}
                type="button"
              />
            ) : null}
            <aside
              aria-label="Capas y filtros del mapa"
              className={[
                styles.sidebar,
                areLayersOpen ? styles.sidebarOpen : "",
              ]
                .filter(Boolean)
                .join(" ")}
              id={layersPanelId}
            >
              <div className={styles.sidebarHeader}>
                <strong>Capas y filtros</strong>
                <button
                  aria-label="Cerrar capas y filtros"
                  onClick={closeLayers}
                  ref={layersCloseRef}
                  type="button"
                >
                  <X aria-hidden="true" size={18} />
                </button>
              </div>
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
              {cartografia ? (
                <MunicipalMap
                  cartografia={cartografia}
                  fitRequest={fitRequest}
                  items={visibleItems}
                  markerColors={markerColors}
                  onSelectItem={(item) => setSelectedId(itemKey(item))}
                  selectedItemId={selectedId}
                />
              ) : (
                <ResourceState
                  description={
                    pueblo
                      ? "Preparando el plano del municipio…"
                      : "Este municipio todavía no tiene plano preparado. Se añade al contratar el servicio."
                  }
                  icon={MapPin}
                  title={pueblo ? "Cargando el plano" : "Sin plano del municipio"}
                  tone={pueblo ? "empty" : "restricted"}
                />
              )}
              <p className={styles.counter}>
                {visibleItems.length} de {items.length} elementos
                {hiddenLayers.size > 0 || search.trim() ? " (filtrados)" : ""}
              </p>
              {items.length > 0 && visibleItems.length === 0 ? (
                <div className={styles.filterEmpty} role="status">
                  <span>Sin elementos con estos filtros.</span>
                  <button onClick={clearFilters} type="button">
                    Limpiar filtros
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
