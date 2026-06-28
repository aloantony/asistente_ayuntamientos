"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchGeoMapItems } from "../lib/geo";
import { useSession } from "../lib/session";
import { MunicipalMap } from "./MunicipalMap";
import type { GeoEntityType, GeoMapItem, User } from "./types";
import { userHasPermission } from "./types";

type MapPanelProps = {
  user: User;
};

type EntityTypeFilter = "all" | GeoEntityType;

function getItemKey(item: GeoMapItem) {
  return `${item.entity_type}-${item.entity_id}`;
}

function entityTypeLabel(type: GeoEntityType) {
  return type === "requirement" ? "Necesidad" : "Proyecto";
}

function formatStatus(value: string) {
  return value.replace(/_/g, " ");
}

function normalizeDetailPath(item: GeoMapItem) {
  if (item.entity_type === "requirement") {
    return `/requisitos?id=${item.entity_id}`;
  }
  return item.detail_path || "/proyectos";
}

export function MapPanel({ user }: MapPanelProps) {
  const { handleRequestError } = useSession();
  const [entityType, setEntityType] = useState<EntityTypeFilter>("all");
  const [status, setStatus] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [items, setItems] = useState<GeoMapItem[]>([]);
  const [selectedItem, setSelectedItem] = useState<GeoMapItem | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [message, setMessage] = useState("");

  const canViewMap =
    userHasPermission(user, "map.view") || userHasPermission(user, "map.manage");

  const queryParams = useMemo(() => {
    const params = new URLSearchParams();
    if (entityType !== "all") {
      params.set("entity_type", entityType);
    }
    if (status.trim()) {
      params.set("status", status.trim());
    }
    if (includeArchived) {
      params.set("include_archived", "true");
    }
    params.set("limit", "500");
    return params;
  }, [entityType, includeArchived, status]);

  useEffect(() => {
    if (!canViewMap) {
      return;
    }

    let isActive = true;
    setIsLoading(true);
    setMessage("");

    fetchGeoMapItems(queryParams)
      .then((mapItems) => {
        if (!isActive) {
          return;
        }
        setItems(mapItems);
        setSelectedItem((current) => {
          if (!current) {
            return null;
          }
          return mapItems.find((item) => getItemKey(item) === getItemKey(current)) ?? null;
        });
      })
      .catch((error) => {
        if (isActive) {
          handleRequestError(
            error,
            setMessage,
            "No se pudieron cargar los elementos del mapa.",
          );
        }
      })
      .finally(() => {
        if (isActive) {
          setIsLoading(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, [canViewMap, handleRequestError, queryParams]);

  const handleSelectItem = useCallback((item: GeoMapItem) => {
    setSelectedItem(item);
  }, []);

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
            El mapa mostrará el trabajo municipal con ubicación revisada o propuesta.
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
            value={entityType}
            onChange={(event) => setEntityType(event.target.value as EntityTypeFilter)}
          >
            <option value="all">Todos</option>
            <option value="requirement">Necesidades</option>
            <option value="project">Proyectos</option>
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

      {message ? <p className="form-error">{message}</p> : null}

      <div className="map-content-grid">
        <div className="map-main-column">
          {items.length > 0 ? (
            <MunicipalMap
              items={items}
              onSelectItem={handleSelectItem}
              selectedItemId={selectedItem ? getItemKey(selectedItem) : null}
            />
          ) : (
            <div className="map-empty-state">
              <p>Todavía no hay necesidades o proyectos con ubicación.</p>
              {isLoading ? (
                <span>Cargando ubicaciones…</span>
              ) : (
                <span>Cuando existan ubicaciones visibles aparecerán aquí.</span>
              )}
            </div>
          )}
        </div>

        <aside className="map-detail-card" aria-label="Detalle del marcador seleccionado">
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
              </dl>
              <Link className="secondary-button map-detail-link" href={normalizeDetailPath(selectedItem)}>
                Abrir {selectedItem.entity_type === "requirement" ? "necesidad" : "proyectos"}
              </Link>
            </>
          ) : (
            <div className="map-detail-placeholder">
              <h2>Selecciona un marcador</h2>
              <p>
                Pulsa sobre una ubicación del mapa para ver su necesidad o proyecto y abrir su ficha.
              </p>
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}
