"use client";

import Link from "next/link";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { ApiRequestError } from "../lib/api";
import { fetchMunicipalAssets } from "../lib/geo";
import {
  fetchMaintenanceOrders,
  type MaintenanceOrderListFilters,
} from "../lib/maintenance";
import { useSession } from "../lib/session";
import styles from "./MaintenanceDashboard.module.css";
import type {
  MaintenanceOrder,
  MaintenanceOrderPriority,
  MaintenanceType,
  MunicipalAsset,
  User,
} from "./types";
import { userHasPermission } from "./types";

const PAGE_SIZE = 25;
const UPCOMING_DAYS = 30;

type MaintenanceView =
  | "open"
  | "overdue"
  | "upcoming"
  | "completed"
  | "all";

type DashboardMetrics = {
  total: number;
  open: number;
  overdue: number;
  upcoming: number;
  completed: number;
};

type FilterState = {
  query: string;
  assetId: string;
  priority: "" | MaintenanceOrderPriority;
  maintenanceType: "" | MaintenanceType;
};

const EMPTY_FILTERS: FilterState = {
  query: "",
  assetId: "",
  priority: "",
  maintenanceType: "",
};

const STATUS_LABELS: Record<MaintenanceOrder["status"], string> = {
  planned: "Planificada",
  scheduled: "Programada",
  in_progress: "En curso",
  completed: "Completada",
  cancelled: "Cancelada",
};

const PRIORITY_LABELS: Record<MaintenanceOrderPriority, string> = {
  low: "Baja",
  normal: "Normal",
  high: "Alta",
  urgent: "Urgente",
};

const TYPE_LABELS: Record<MaintenanceType, string> = {
  preventive: "Preventivo",
  corrective: "Correctivo",
  inspection: "Inspección",
  cleaning: "Limpieza",
  other: "Otro",
};

const VIEW_LABELS: Record<MaintenanceView, string> = {
  open: "Abiertas",
  overdue: "Vencidas",
  upcoming: "Próximas",
  completed: "Completadas",
  all: "Todas",
};

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

function localDateKey(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dateKeyWithOffset(days: number) {
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() + days);
  return localDateKey(date);
}

function formatDate(value: string | null) {
  if (!value) {
    return "Sin fecha prevista";
  }

  const date = new Date(`${value}T12:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("es-ES", {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(date);
}

function formatUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("es-ES", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(minutes: number | null) {
  if (minutes === null) {
    return "Sin estimación";
  }
  if (minutes < 60) {
    return `${minutes} min`;
  }

  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder > 0 ? `${hours} h ${remainder} min` : `${hours} h`;
}

function getEligibleOrganizations(user: User) {
  return (user.organizations ?? [])
    .filter(
      (organization) =>
        organization.status !== "archived" &&
        (typeof organization.municipality_id === "number" ||
          typeof organization.municipality?.id === "number"),
    )
    .sort((left, right) => {
      if (left.status !== right.status) {
        return left.status === "active" ? -1 : 1;
      }
      return (
        left.name.localeCompare(right.name, "es") || left.id - right.id
      );
    });
}

function getViewFilters(
  organizationId: number,
  view: MaintenanceView,
  filters: FilterState,
  offset: number,
): MaintenanceOrderListFilters {
  const today = localDateKey();
  const base: MaintenanceOrderListFilters = {
    organizationId,
    assetId: filters.assetId ? Number(filters.assetId) : undefined,
    priority: filters.priority || undefined,
    maintenanceType: filters.maintenanceType || undefined,
    query: filters.query || undefined,
    limit: PAGE_SIZE,
    offset,
  };

  if (view === "open") {
    return { ...base, includeClosed: false };
  }
  if (view === "overdue") {
    return {
      ...base,
      includeClosed: false,
      scheduledTo: dateKeyWithOffset(-1),
    };
  }
  if (view === "upcoming") {
    return {
      ...base,
      includeClosed: false,
      scheduledFrom: today,
      scheduledTo: dateKeyWithOffset(UPCOMING_DAYS),
    };
  }
  if (view === "completed") {
    return { ...base, includeClosed: true, status: "completed" };
  }
  return { ...base, includeClosed: true };
}

function orderCategory(order: MaintenanceOrder) {
  if (order.status === "completed") {
    return { key: "completed", label: "Completada" } as const;
  }
  if (order.status === "cancelled") {
    return { key: "cancelled", label: "Cancelada" } as const;
  }
  if (order.scheduled_for && order.scheduled_for < localDateKey()) {
    return { key: "overdue", label: "Vencida" } as const;
  }
  if (
    order.scheduled_for &&
    order.scheduled_for <= dateKeyWithOffset(UPCOMING_DAYS)
  ) {
    return { key: "upcoming", label: "Próxima" } as const;
  }
  return { key: "open", label: "Abierta" } as const;
}

function RestrictedState() {
  return (
    <section className={styles.statePanel}>
      <p className={styles.eyebrow}>Mantenimiento municipal</p>
      <h1>Acceso restringido</h1>
      <p>
        Para consultar este panel necesitas acceso simultáneo al inventario y
        al mantenimiento de la organización.
      </p>
    </section>
  );
}

function NoOrganizationState() {
  return (
    <section className={styles.statePanel}>
      <p className={styles.eyebrow}>Mantenimiento municipal</p>
      <h1>Sin organización disponible</h1>
      <p>
        Tu cuenta no está vinculada a ninguna organización municipal activa o
        pausada.
      </p>
    </section>
  );
}

export function MaintenanceDashboard({
  initialOrganizationId,
}: {
  initialOrganizationId?: number | null;
}) {
  const { user, handleRequestError } = useSession();
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<
    number | null
  >(initialOrganizationId ?? null);
  const [assets, setAssets] = useState<MunicipalAsset[]>([]);
  const [assetsTotal, setAssetsTotal] = useState<number | null>(null);
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [isLoadingSummary, setIsLoadingSummary] = useState(false);
  const [summaryError, setSummaryError] = useState("");
  const [orders, setOrders] = useState<MaintenanceOrder[]>([]);
  const [ordersTotal, setOrdersTotal] = useState(0);
  const [isLoadingOrders, setIsLoadingOrders] = useState(false);
  const [ordersError, setOrdersError] = useState("");
  const [view, setView] = useState<MaintenanceView>("open");
  const [filterDraft, setFilterDraft] =
    useState<FilterState>(EMPTY_FILTERS);
  const [appliedFilters, setAppliedFilters] =
    useState<FilterState>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [reloadVersion, setReloadVersion] = useState(0);
  const handleRequestErrorRef = useRef(handleRequestError);

  useEffect(() => {
    handleRequestErrorRef.current = handleRequestError;
  }, [handleRequestError]);

  const organizations = useMemo(
    () => (user ? getEligibleOrganizations(user) : []),
    [user],
  );
  const selectedOrganization =
    organizations.find(
      (organization) => organization.id === selectedOrganizationId,
    ) ??
    organizations[0] ??
    null;

  const canViewAssets = Boolean(
    user &&
      (userHasPermission(user, "assets.view") ||
        userHasPermission(user, "assets.manage")),
  );
  const canViewMaintenance = Boolean(
    user &&
      (userHasPermission(user, "maintenance.view") ||
        userHasPermission(user, "maintenance.manage")),
  );
  const canView = canViewAssets && canViewMaintenance;
  const canCreateAssets = Boolean(
    user &&
      selectedOrganization?.status === "active" &&
      (userHasPermission(user, "assets.create") ||
        userHasPermission(user, "assets.manage")),
  );
  const canOperateMaintenance = Boolean(
    user &&
      selectedOrganization?.status === "active" &&
      [
        "maintenance.create",
        "maintenance.edit",
        "maintenance.complete",
        "maintenance.manage",
      ].some((permission) => userHasPermission(user, permission)),
  );
  const canViewMap = Boolean(
    user &&
      (userHasPermission(user, "map.view") ||
        userHasPermission(user, "map.manage")),
  );

  useEffect(() => {
    if (!canView || !selectedOrganization) {
      setAssets([]);
      setAssetsTotal(null);
      setMetrics(null);
      setSummaryError("");
      setIsLoadingSummary(false);
      return;
    }

    const controller = new AbortController();
    let isActive = true;
    const organizationId = selectedOrganization.id;
    const today = localDateKey();
    setIsLoadingSummary(true);
    setSummaryError("");
    setAssetsTotal(null);
    setMetrics(null);

    async function loadSummary() {
      try {
        const [assetResponse, total, open, overdue, upcoming, completed] =
          await Promise.all([
            fetchMunicipalAssets(organizationId, "", controller.signal),
            fetchMaintenanceOrders(
              {
                organizationId,
                includeClosed: true,
                limit: 1,
              },
              controller.signal,
            ),
            fetchMaintenanceOrders(
              {
                organizationId,
                includeClosed: false,
                limit: 1,
              },
              controller.signal,
            ),
            fetchMaintenanceOrders(
              {
                organizationId,
                includeClosed: false,
                scheduledTo: dateKeyWithOffset(-1),
                limit: 1,
              },
              controller.signal,
            ),
            fetchMaintenanceOrders(
              {
                organizationId,
                includeClosed: false,
                scheduledFrom: today,
                scheduledTo: dateKeyWithOffset(UPCOMING_DAYS),
                limit: 1,
              },
              controller.signal,
            ),
            fetchMaintenanceOrders(
              {
                organizationId,
                includeClosed: true,
                status: "completed",
                limit: 1,
              },
              controller.signal,
            ),
          ]);

        if (!isActive || controller.signal.aborted) {
          return;
        }

        setAssets(assetResponse.items);
        setAssetsTotal(assetResponse.total);
        setMetrics({
          total: total.total,
          open: open.total,
          overdue: overdue.total,
          upcoming: upcoming.total,
          completed: completed.total,
        });
      } catch (error) {
        if (!isActive || controller.signal.aborted || isAbortError(error)) {
          return;
        }
        if (error instanceof ApiRequestError && error.status === 403) {
          setSummaryError(
            "No puedes consultar el inventario y el mantenimiento de esta organización.",
          );
        } else {
          handleRequestErrorRef.current(
            error,
            setSummaryError,
            "No se pudo cargar el resumen de mantenimiento.",
          );
        }
      } finally {
        if (isActive && !controller.signal.aborted) {
          setIsLoadingSummary(false);
        }
      }
    }

    void loadSummary();
    return () => {
      isActive = false;
      controller.abort();
    };
  }, [canView, reloadVersion, selectedOrganization]);

  useEffect(() => {
    if (!canView || !selectedOrganization || summaryError) {
      setOrders([]);
      setOrdersTotal(0);
      setOrdersError("");
      setIsLoadingOrders(false);
      return;
    }

    const controller = new AbortController();
    let isActive = true;
    setIsLoadingOrders(true);
    setOrdersError("");

    void fetchMaintenanceOrders(
      getViewFilters(
        selectedOrganization.id,
        view,
        appliedFilters,
        offset,
      ),
      controller.signal,
    )
      .then((response) => {
        if (!isActive || controller.signal.aborted) {
          return;
        }
        setOrders(response.items);
        setOrdersTotal(response.total);
      })
      .catch((error) => {
        if (!isActive || controller.signal.aborted || isAbortError(error)) {
          return;
        }
        if (error instanceof ApiRequestError && error.status === 403) {
          setOrdersError(
            "No tienes permiso para consultar estas órdenes en la organización seleccionada.",
          );
        } else {
          handleRequestErrorRef.current(
            error,
            setOrdersError,
            "No se pudieron cargar las órdenes de mantenimiento.",
          );
        }
      })
      .finally(() => {
        if (isActive && !controller.signal.aborted) {
          setIsLoadingOrders(false);
        }
      });

    return () => {
      isActive = false;
      controller.abort();
    };
  }, [
    appliedFilters,
    canView,
    offset,
    reloadVersion,
    selectedOrganization,
    summaryError,
    view,
  ]);

  const assetFilterIsComplete =
    assetsTotal !== null && assetsTotal <= assets.length;
  const activeFilterCount = [
    appliedFilters.query,
    appliedFilters.assetId,
    appliedFilters.priority,
    appliedFilters.maintenanceType,
  ].filter(Boolean).length;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(ordersTotal / PAGE_SIZE));

  if (!user) {
    return null;
  }
  if (!canView) {
    return <RestrictedState />;
  }
  if (!selectedOrganization) {
    return <NoOrganizationState />;
  }

  function changeOrganization(organizationId: number) {
    setSelectedOrganizationId(organizationId);
    setAssets([]);
    setAssetsTotal(null);
    setMetrics(null);
    setOrders([]);
    setOrdersTotal(0);
    setSummaryError("");
    setOrdersError("");
    setIsLoadingSummary(true);
    setIsLoadingOrders(true);
    setFilterDraft(EMPTY_FILTERS);
    setAppliedFilters(EMPTY_FILTERS);
    setView("open");
    setOffset(0);
  }

  function selectView(nextView: MaintenanceView) {
    setView(nextView);
    setOffset(0);
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAppliedFilters({
      ...filterDraft,
      query: filterDraft.query.trim(),
    });
    setOffset(0);
  }

  function clearFilters() {
    setFilterDraft(EMPTY_FILTERS);
    setAppliedFilters(EMPTY_FILTERS);
    setOffset(0);
  }

  const organizationLabel =
    selectedOrganization.municipality?.name ?? selectedOrganization.name;
  const inventoryHref = `/inventario?organization_id=${selectedOrganization.id}`;
  const mapHref = `/mapa?organization_id=${selectedOrganization.id}`;

  return (
    <section className={styles.dashboard}>
      <header className={styles.hero}>
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>Operaciones municipales</p>
          <h1>Mantenimiento</h1>
          <p>
            Prioriza las intervenciones del inventario con fechas, estados y
            responsables registrados en el sistema.
          </p>
        </div>

        <div className={styles.contextArea}>
          {organizations.length > 1 ? (
            <label className={styles.organizationSelect}>
              <span>Organización</span>
              <select
                onChange={(event) =>
                  changeOrganization(Number(event.target.value))
                }
                value={selectedOrganization.id}
              >
                {organizations.map((organization) => (
                  <option key={organization.id} value={organization.id}>
                    {organization.municipality?.name ?? organization.name}
                    {organization.status === "paused" ? " · pausada" : ""}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <div className={styles.organizationBadge}>
              <span>Organización</span>
              <strong>{organizationLabel}</strong>
            </div>
          )}

          <div className={styles.heroActions}>
            <Link className={styles.secondaryLink} href={inventoryHref}>
              Abrir inventario
            </Link>
            {canViewMap ? (
              <Link className={styles.secondaryLink} href={mapHref}>
                Abrir mapa
              </Link>
            ) : null}
          </div>
        </div>
      </header>

      {selectedOrganization.status === "paused" ? (
        <p className={styles.contextWarning} role="status">
          La organización está pausada. Puedes consultar su historial, pero el
          backend bloqueará cualquier modificación.
        </p>
      ) : null}

      {summaryError ? (
        <div className={styles.errorPanel} role="alert">
          <div>
            <strong>No se pudo cargar el resumen</strong>
            <p>{summaryError}</p>
          </div>
          <button
            onClick={() => setReloadVersion((version) => version + 1)}
            type="button"
          >
            Reintentar
          </button>
        </div>
      ) : null}

      {!summaryError ? (
        <div aria-busy={isLoadingSummary} className={styles.metrics}>
          {(
            [
              ["open", "Abiertas", "Trabajo pendiente o en curso"],
              ["overdue", "Vencidas", "Con fecha anterior a hoy"],
              ["upcoming", "Próximos 30 días", "Desde hoy hasta el próximo mes"],
              ["completed", "Completadas", "Histórico finalizado"],
            ] as const
          ).map(([metricKey, label, hint]) => (
            <button
              aria-pressed={view === metricKey}
              className={`${styles.metricCard}${
                view === metricKey ? ` ${styles.metricCardActive}` : ""
              }`}
              key={metricKey}
              onClick={() => selectView(metricKey)}
              type="button"
            >
              <span>{label}</span>
              <strong>
                {isLoadingSummary || !metrics ? "—" : metrics[metricKey]}
              </strong>
              <small>{hint}</small>
            </button>
          ))}
        </div>
      ) : null}

      {!summaryError && !isLoadingSummary && assetsTotal === 0 && metrics?.total === 0 ? (
        <section className={styles.onboardingEmpty}>
          <div className={styles.emptyIllustration} aria-hidden="true">
            <svg
              fill="none"
              height="34"
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="1.6"
              viewBox="0 0 24 24"
              width="34"
            >
              <path d="M4 20h16M6 20V8l6-4 6 4v12M9 20v-5h6v5" />
              <path d="m16.5 5.5 2-2M18.5 5.5l-2-2" />
            </svg>
          </div>
          <div className={styles.emptyCopy}>
            <p className={styles.eyebrow}>Primer paso</p>
            <h2>Registra el inventario antes de planificar trabajos</h2>
            <p>
              Todavía no hay activos municipales ni órdenes en
              {` ${organizationLabel}`}. Cada orden debe pertenecer a un activo
              real para conservar su contexto y su historial auditable.
            </p>
            <ol>
              <li>Crea categorías y tipos de activo en el inventario.</li>
              <li>Registra los elementos que mantiene el ayuntamiento.</li>
              <li>Ubícalos en el mapa y programa sus intervenciones.</li>
            </ol>
            <div className={styles.emptyActions}>
              <Link className={styles.primaryLink} href={inventoryHref}>
                {canCreateAssets ? "Crear el primer activo" : "Abrir inventario"}
              </Link>
              {canViewMap ? (
                <Link className={styles.secondaryLink} href={mapHref}>
                  Revisar mapa municipal
                </Link>
              ) : null}
            </div>
          </div>
        </section>
      ) : null}

      {!summaryError &&
      !isLoadingSummary &&
      !(assetsTotal === 0 && metrics?.total === 0) ? (
        <>
          <form className={styles.filters} onSubmit={applyFilters}>
            <div className={styles.filterHeading}>
              <div>
                <p className={styles.eyebrow}>Órdenes</p>
                <h2>{VIEW_LABELS[view]}</h2>
              </div>
              <button
                className={styles.allViewButton}
                onClick={() => selectView("all")}
                type="button"
              >
                Ver todas
              </button>
            </div>

            <div className={styles.filterGrid}>
              <label className={styles.searchField}>
                <span>Buscar</span>
                <input
                  maxLength={200}
                  onChange={(event) =>
                    setFilterDraft((current) => ({
                      ...current,
                      query: event.target.value,
                    }))
                  }
                  placeholder="Orden, descripción, código o activo"
                  type="search"
                  value={filterDraft.query}
                />
              </label>

              <label>
                <span>Activo</span>
                <select
                  disabled={!assetFilterIsComplete || assets.length === 0}
                  onChange={(event) =>
                    setFilterDraft((current) => ({
                      ...current,
                      assetId: event.target.value,
                    }))
                  }
                  value={filterDraft.assetId}
                >
                  <option value="">
                    {assetFilterIsComplete
                      ? "Todos los activos"
                      : "Usa la búsqueda por nombre"}
                  </option>
                  {assets.map((asset) => (
                    <option key={asset.id} value={asset.id}>
                      {asset.code ? `${asset.code} · ` : ""}
                      {asset.name}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                <span>Prioridad</span>
                <select
                  onChange={(event) =>
                    setFilterDraft((current) => ({
                      ...current,
                      priority: event.target.value as FilterState["priority"],
                    }))
                  }
                  value={filterDraft.priority}
                >
                  <option value="">Todas</option>
                  {Object.entries(PRIORITY_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>

              <label>
                <span>Tipo</span>
                <select
                  onChange={(event) =>
                    setFilterDraft((current) => ({
                      ...current,
                      maintenanceType:
                        event.target.value as FilterState["maintenanceType"],
                    }))
                  }
                  value={filterDraft.maintenanceType}
                >
                  <option value="">Todos</option>
                  {Object.entries(TYPE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className={styles.filterActions}>
              <button className={styles.applyButton} type="submit">
                Aplicar filtros
              </button>
              <button
                disabled={activeFilterCount === 0}
                onClick={clearFilters}
                type="button"
              >
                Limpiar{activeFilterCount > 0 ? ` (${activeFilterCount})` : ""}
              </button>
              {!assetFilterIsComplete && assetsTotal ? (
                <p>
                  El inventario tiene {assetsTotal} activos. Para no cargar una
                  lista incompleta, filtra por nombre o código.
                </p>
              ) : null}
            </div>
          </form>

          <section className={styles.orderPanel} aria-live="polite">
            <div className={styles.orderPanelHeader}>
              <div>
                <strong>
                  {isLoadingOrders
                    ? "Cargando órdenes…"
                    : `${ordersTotal} ${
                        ordersTotal === 1 ? "orden" : "órdenes"
                      }`}
                </strong>
                <span>
                  {organizationLabel} · página {currentPage} de {totalPages}
                </span>
              </div>
              {canOperateMaintenance ? (
                <p>
                  Las acciones operativas se realizan desde la ficha del activo.
                </p>
              ) : null}
            </div>

            {ordersError ? (
              <div className={styles.inlineError} role="alert">
                <p>{ordersError}</p>
                <button
                  onClick={() => setReloadVersion((version) => version + 1)}
                  type="button"
                >
                  Reintentar
                </button>
              </div>
            ) : null}

            {!ordersError && isLoadingOrders ? (
              <div className={styles.orderSkeleton} aria-hidden="true">
                <span />
                <span />
                <span />
              </div>
            ) : null}

            {!ordersError && !isLoadingOrders && orders.length === 0 ? (
              <div className={styles.filteredEmpty}>
                <h3>
                  {metrics?.total === 0
                    ? "No hay órdenes de mantenimiento todavía"
                    : `No hay órdenes ${VIEW_LABELS[view].toLowerCase()} con estos filtros`}
                </h3>
                <p>
                  {metrics?.total === 0
                    ? "El inventario ya puede consultarse. Abre un activo para programar su primera intervención."
                    : "Prueba otra vista o elimina alguno de los filtros aplicados."}
                </p>
                <div className={styles.emptyActions}>
                  <Link className={styles.primaryLink} href={inventoryHref}>
                    Abrir inventario
                  </Link>
                  {canViewMap ? (
                    <Link className={styles.secondaryLink} href={mapHref}>
                      Abrir mapa
                    </Link>
                  ) : null}
                  {activeFilterCount > 0 ? (
                    <button onClick={clearFilters} type="button">
                      Limpiar filtros
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}

            {!ordersError && !isLoadingOrders && orders.length > 0 ? (
              <ol className={styles.orderList}>
                {orders.map((order) => {
                  const category = orderCategory(order);
                  const canOpenMap = canViewMap;

                  return (
                    <li className={styles.orderRow} key={order.id}>
                      <div className={styles.orderIdentity}>
                        <div className={styles.rowBadges}>
                          <span
                            className={`${styles.categoryBadge} ${
                              styles[`category_${category.key}`]
                            }`}
                          >
                            {category.label}
                          </span>
                          <span className={styles.orderId}>#{order.id}</span>
                        </div>
                        <h3>{order.title}</h3>
                        <p>
                          {order.asset.code ? `${order.asset.code} · ` : ""}
                          {order.asset.name}
                        </p>
                      </div>

                      <dl className={styles.orderFacts}>
                        <div>
                          <dt>Estado</dt>
                          <dd>{STATUS_LABELS[order.status]}</dd>
                        </div>
                        <div>
                          <dt>Fecha</dt>
                          <dd>{formatDate(order.scheduled_for)}</dd>
                        </div>
                        <div>
                          <dt>Prioridad</dt>
                          <dd>{PRIORITY_LABELS[order.priority]}</dd>
                        </div>
                        <div>
                          <dt>Responsable</dt>
                          <dd>{order.assigned_to?.full_name ?? "Sin asignar"}</dd>
                        </div>
                      </dl>

                      <details className={styles.orderDetail}>
                        <summary>Más información</summary>
                        <div>
                          <p>
                            {order.description ||
                              "Esta orden no tiene una descripción adicional."}
                          </p>
                          <dl>
                            <div>
                              <dt>Tipo</dt>
                              <dd>{TYPE_LABELS[order.maintenance_type]}</dd>
                            </div>
                            <div>
                              <dt>Duración</dt>
                              <dd>{formatDuration(order.estimated_minutes)}</dd>
                            </div>
                            <div>
                              <dt>Actualizada</dt>
                              <dd>{formatUpdatedAt(order.updated_at)}</dd>
                            </div>
                          </dl>
                        </div>
                      </details>

                      <div className={styles.rowAction}>
                        {canOpenMap ? (
                          <Link
                            href={`/mapa?organization_id=${selectedOrganization.id}&entity_type=asset&entity_id=${order.asset_id}`}
                          >
                            {canOperateMaintenance
                              ? "Gestionar en mapa"
                              : "Ver en mapa"}
                          </Link>
                        ) : (
                          <span>Mapa no disponible para esta cuenta</span>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ol>
            ) : null}

            {!ordersError && !isLoadingOrders && ordersTotal > PAGE_SIZE ? (
              <nav aria-label="Paginación de mantenimiento" className={styles.pagination}>
                <button
                  disabled={offset === 0}
                  onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
                  type="button"
                >
                  Anterior
                </button>
                <span>
                  Página {currentPage} de {totalPages}
                </span>
                <button
                  disabled={offset + PAGE_SIZE >= ordersTotal}
                  onClick={() => setOffset((current) => current + PAGE_SIZE)}
                  type="button"
                >
                  Siguiente
                </button>
              </nav>
            ) : null}
          </section>
        </>
      ) : null}
    </section>
  );
}
