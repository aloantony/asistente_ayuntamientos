"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { adminRequest } from "../lib/api";
import { createEntityLocation, fetchGeoMapItems } from "../lib/geo";
import { useSession } from "../lib/session";
import { MunicipalMap } from "./MunicipalMap";
import type { GeoEntityType, GeoMapItem, Project, Requirement, User } from "./types";
import { userHasPermission } from "./types";

type MapPanelProps = {
  user: User;
  embeddedContext?: {
    organizationId?: number;
    entityType?: GeoEntityType;
    entityId?: number;
  };
};

type EntityTypeFilter = "all" | GeoEntityType;

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
  kind: GeoEntityType;
  latitude: number;
  longitude: number;
  organizationId: string;
  title: string;
  description: string;
};

function getItemKey(item: GeoMapItem) {
  return `${item.entity_type}-${item.entity_id}`;
}

function entityTypeLabel(type: GeoEntityType) {
  return type === "requirement" ? "Necesidad" : "Proyecto";
}

function formatStatus(value: string) {
  return value.replace(/_/g, " ");
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
  return item.detail_path || "/proyectos";
}

function parseNumberParam(value: string | null) {
  if (value === null || value.trim() === "") {
    return null;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseEntityTypeParam(value: string | null): GeoEntityType | null {
  return value === "requirement" || value === "project" ? value : null;
}

function defaultOrganizationId(user: User) {
  return user.organizations?.[0]?.id ? String(user.organizations[0].id) : "";
}

export function MapPanel({ user, embeddedContext }: MapPanelProps) {
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

  const canViewMap =
    userHasPermission(user, "map.view") || userHasPermission(user, "map.manage");
  const canEditMap =
    userHasPermission(user, "map.edit") || userHasPermission(user, "map.manage");
  const canCreateRequirements =
    canEditMap && userHasPermission(user, "requirements.create");
  const canCreateProjects = canEditMap && userHasPermission(user, "projects.create");
  const focusedEntityType =
    embeddedContext?.entityType ??
    parseEntityTypeParam(searchParams.get("entity_type"));
  const focusedEntityId =
    embeddedContext?.entityId ?? parseNumberParam(searchParams.get("entity_id"));
  const focusedLatitude = parseNumberParam(searchParams.get("lat"));
  const focusedLongitude = parseNumberParam(searchParams.get("lng"));
  const focusedZoom = parseNumberParam(searchParams.get("zoom"));
  const focusedItemKey =
    focusedEntityType && focusedEntityId !== null
      ? `${focusedEntityType}-${focusedEntityId}`
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
    if (entityType !== "all") {
      params.set("entity_type", entityType);
    }
    if (focusedEntityType && focusedEntityId !== null) {
      params.set("entity_type", focusedEntityType);
      params.set("entity_id", String(focusedEntityId));
    }
    if (embeddedContext?.organizationId) {
      params.set("organization_id", String(embeddedContext.organizationId));
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
    embeddedContext?.organizationId,
    entityType,
    focusedEntityId,
    focusedEntityType,
    includeArchived,
    status,
  ]);

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
          if (focusedItemKey) {
            return mapItems.find((item) => getItemKey(item) === focusedItemKey) ?? null;
          }
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
  }, [canViewMap, focusedItemKey, handleRequestError, queryParams]);

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

  const handleSelectItem = useCallback((item: GeoMapItem) => {
    setSelectedItem(item);
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
        x: Math.min(payload.x, window.innerWidth - 320),
        y: Math.min(payload.y, window.innerHeight - 280),
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

  function openRegistrationDraft(kind: GeoEntityType) {
    if (!mapContextMenu) {
      return;
    }
    setRegistrationDraft({
      kind,
      latitude: mapContextMenu.latitude,
      longitude: mapContextMenu.longitude,
      organizationId: embeddedContext?.organizationId
        ? String(embeddedContext.organizationId)
        : defaultOrganizationId(user),
      title: "",
      description: "",
    });
    setRegistrationError("");
    setMapContextMenu(null);
  }

  function selectNearestItem() {
    if (!mapContextMenu?.nearestItem) {
      return;
    }
    setSelectedItem(mapContextMenu.nearestItem);
    setManualFocusLocation(null);
    setManualFocusZoom(null);
    setMapContextMenu(null);
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
    if (
      embeddedContext?.organizationId !== undefined &&
      organizationId !== embeddedContext.organizationId
    ) {
      setRegistrationError(
        "La organización debe coincidir con la vista del mapa abierta.",
      );
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

      const belongsToEmbeddedContext =
        embeddedContext?.organizationId === undefined ||
        mapItem.location.organization_id === embeddedContext.organizationId;
      if (belongsToEmbeddedContext) {
        setItems((currentItems) => [
          mapItem,
          ...currentItems.filter(
            (item) => getItemKey(item) !== getItemKey(mapItem),
          ),
        ]);
        setSelectedItem(mapItem);
      }
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
          <MunicipalMap
            focusLocation={
              explicitFocusLocation ?? manualFocusLocation ?? selectedFocusLocation
            }
            initialZoom={focusedZoom ?? manualFocusZoom}
            items={items}
            onMapContextMenu={handleMapContextMenu}
            onSelectItem={handleSelectItem}
            selectedItemId={selectedItem ? getItemKey(selectedItem) : null}
          />
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
              <Link
                className="secondary-button map-detail-link"
                href={normalizeDetailPath(selectedItem)}
                rel={embeddedContext ? "noreferrer" : undefined}
                target={embeddedContext ? "_blank" : undefined}
              >
                Abrir {selectedItem.entity_type === "requirement" ? "necesidad" : "proyectos"}
              </Link>
            </>
          ) : (
            <div className="map-detail-placeholder">
              <h2>Registra trabajo sobre el territorio</h2>
              <p>
                Haz click derecho en cualquier punto del mapa para crear una necesidad o un proyecto ya ubicado. Si hay marcadores, púlsalos para abrir su ficha.
              </p>
              {items.length === 0 ? (
                <p className="small-muted">
                  {isLoading
                    ? "Cargando ubicaciones…"
                    : "Aún no hay elementos ubicados, pero puedes registrar el primero desde el mapa."}
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
            <strong>Registrar en este punto</strong>
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
          {!canCreateRequirements && !canCreateProjects ? (
            <p className="map-context-menu-note">
              No tienes permisos para crear registros desde el mapa.
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
                {mapContextMenu.nearestItem.title} · {formatDistance(mapContextMenu.nearestDistanceMeters)}
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

      {registrationDraft ? (
        <div className="map-registration-backdrop" role="presentation">
          <form
            className="map-registration-dialog"
            onSubmit={handleCreateMapRegistration}
          >
            <div className="map-registration-head">
              <div>
                <p className="eyebrow">Nuevo registro geolocalizado</p>
                <h2>
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
                disabled={embeddedContext?.organizationId !== undefined}
                onChange={(event) =>
                  setRegistrationDraft({
                    ...registrationDraft,
                    organizationId: event.target.value,
                  })
                }
              >
                <option value="">Selecciona organización</option>
                {embeddedContext?.organizationId !== undefined &&
                !(user.organizations ?? []).some(
                  (organization) =>
                    organization.id === embeddedContext.organizationId,
                ) ? (
                  <option value={embeddedContext.organizationId}>
                    Organización {embeddedContext.organizationId}
                  </option>
                ) : null}
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
              <p className="form-error">{registrationError}</p>
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
