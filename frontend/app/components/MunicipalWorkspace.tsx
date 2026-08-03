"use client";

import {
  BookOpen,
  Building2,
  CircleAlert,
  ClipboardList,
  Landmark,
  RefreshCw,
  ShieldCheck,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { fetchMunicipality } from "../lib/fetchers";
import {
  fetchMunicipalAssetSummary,
  fetchMunicipalMaintenanceSummary,
  fetchMunicipalOrdinances,
  fetchMunicipalOrganization,
  type MunicipalCollection,
} from "../lib/municipalWorkspace";
import { canViewMunicipalHub } from "../lib/permissions";
import { useSession } from "../lib/session";
import styles from "./MunicipalWorkspace.module.css";
import { HojaDeRuta } from "./ayuntamiento/HojaDeRuta";
import { InformacionMunicipio } from "./ayuntamiento/InformacionMunicipio";
import { Normativa } from "./ayuntamiento/Normativa";
import { Personal } from "./ayuntamiento/Personal";
import { ServiciosMunicipales } from "./ayuntamiento/ServiciosMunicipales";
import {
  EMPTY_RESOURCE_ERRORS,
  ORDINANCE_MANAGEMENT_PERMISSIONS,
  ResourceState,
  getInitials,
  getMunicipalContexts,
} from "./ayuntamiento/shared";
import type { ResourceErrors, WorkspaceTab } from "./ayuntamiento/types";
import {
  userHasPermission,
  type MaintenanceOrder,
  type Municipality,
  type MunicipalAsset,
  type Organization,
  type Ordinance,
  type User,
} from "./types";

type TabDefinition = {
  id: WorkspaceTab;
  label: string;
  icon: LucideIcon;
};

// Rótulos tomados de la navegación municipal de referencia. Los identificadores
// no cambian: se usan en enlaces `?tab=` repartidos por el producto.
const TAB_DEFINITIONS: TabDefinition[] = [
  { id: "summary", label: "Información", icon: Landmark },
  { id: "ordinances", label: "Normativa", icon: BookOpen },
  { id: "facilities", label: "Servicios municipales", icon: Wrench },
  { id: "people", label: "Personal", icon: Users },
  { id: "roadmap", label: "Hoja de ruta", icon: ClipboardList },
];

function RestrictedState() {
  return (
    <section className={`panel ${styles.pageState}`}>
      <ShieldCheck aria-hidden="true" size={28} strokeWidth={1.6} />
      <p className="eyebrow">Ayuntamiento</p>
      <h1>Acceso restringido</h1>
      <p className="muted">Esta sección no está disponible para esta cuenta.</p>
    </section>
  );
}

function EmptyState({ user }: { user: User }) {
  const organizations = user.organizations ?? [];
  const hasEligibleOrganizations = organizations.some(
    (organization) => organization.status !== "archived",
  );
  const canManageOrganizations = userHasPermission(user, "organizations.manage");

  let message = "Tu cuenta todavía no pertenece a ninguna organización municipal.";
  if (organizations.length > 0 && !hasEligibleOrganizations) {
    message = "Las organizaciones afiliadas a tu cuenta están archivadas.";
  } else if (hasEligibleOrganizations) {
    message =
      "Tus organizaciones activas o pausadas no tienen un municipio vinculado.";
  }

  return (
    <section className={`panel ${styles.pageState}`}>
      <Building2 aria-hidden="true" size={28} strokeWidth={1.6} />
      <p className="eyebrow">Ayuntamiento</p>
      <h1>Sin municipio afiliado</h1>
      <p className="muted">{message}</p>
      {canManageOrganizations ? (
        <Link className={styles.primaryAction} href="/admin/organizaciones">
          Revisar organizaciones
        </Link>
      ) : null}
    </section>
  );
}

export function MunicipalWorkspace() {
  const { user, handleRequestError } = useSession();
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("summary");
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<
    number | null
  >(null);
  const [municipality, setMunicipality] = useState<Municipality | null>(null);
  const [organization, setOrganization] = useState<Organization | null>(null);
  const [ordinances, setOrdinances] = useState<
    MunicipalCollection<Ordinance> | null
  >(null);
  const [assets, setAssets] = useState<
    MunicipalCollection<MunicipalAsset> | null
  >(null);
  const [maintenance, setMaintenance] = useState<
    MunicipalCollection<MaintenanceOrder> | null
  >(null);
  const [resourceErrors, setResourceErrors] = useState<ResourceErrors>(
    EMPTY_RESOURCE_ERRORS,
  );
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const requestSequenceRef = useRef(0);
  const tabButtonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const contexts = user ? getMunicipalContexts(user) : [];
  const selectedContext =
    contexts.find(
      ({ organization: item }) => item.id === selectedOrganizationId,
    ) ??
    contexts[0] ??
    null;
  const permissionSignature = (user?.permissions ?? []).slice().sort().join(",");
  const canViewOrdinances = Boolean(
    user &&
      (userHasPermission(user, "ordinances.view") ||
        userHasPermission(user, "ordinances.manage")),
  );
  const canViewAssets = Boolean(
    user &&
      (userHasPermission(user, "assets.view") ||
        userHasPermission(user, "assets.manage")),
  );
  const canViewMaintenance = Boolean(
    user &&
      canViewAssets &&
      (userHasPermission(user, "maintenance.view") ||
        userHasPermission(user, "maintenance.manage")),
  );
  const canViewMap = Boolean(
    user &&
      (userHasPermission(user, "map.view") ||
        userHasPermission(user, "map.manage")),
  );
  const canManageOrdinances = Boolean(
    user &&
      ORDINANCE_MANAGEMENT_PERMISSIONS.some((permission) =>
        userHasPermission(user, permission),
      ),
  );

  useEffect(() => {
    const requestedTab = new URLSearchParams(window.location.search).get("tab");
    if (TAB_DEFINITIONS.some(({ id }) => id === requestedTab)) {
      setActiveTab(requestedTab as WorkspaceTab);
    }
  }, []);

  useEffect(() => {
    if (!user || !canViewMunicipalHub(user) || !selectedContext) {
      requestSequenceRef.current += 1;
      setMunicipality(null);
      setOrganization(null);
      setOrdinances(null);
      setAssets(null);
      setMaintenance(null);
      setError("");
      setResourceErrors(EMPTY_RESOURCE_ERRORS);
      setIsLoading(false);
      return;
    }

    const controller = new AbortController();
    const requestSequence = ++requestSequenceRef.current;
    const organizationId = selectedContext.organization.id;
    const municipalityId = selectedContext.municipality.id;

    setMunicipality(null);
    setOrganization(null);
    setOrdinances(null);
    setAssets(null);
    setMaintenance(null);
    setError("");
    setResourceErrors(EMPTY_RESOURCE_ERRORS);
    setIsLoading(true);

    async function loadWorkspace() {
      const [
        municipalityResult,
        organizationResult,
        ordinanceResult,
        assetResult,
        maintenanceResult,
      ] = await Promise.allSettled([
        fetchMunicipality(municipalityId, controller.signal),
        fetchMunicipalOrganization(organizationId, controller.signal),
        canViewOrdinances
          ? fetchMunicipalOrdinances(municipalityId, controller.signal)
          : Promise.resolve(null),
        canViewAssets
          ? fetchMunicipalAssetSummary(organizationId, controller.signal)
          : Promise.resolve(null),
        canViewMaintenance
          ? fetchMunicipalMaintenanceSummary(organizationId, controller.signal)
          : Promise.resolve(null),
      ] as const);

      if (
        controller.signal.aborted ||
        requestSequence !== requestSequenceRef.current
      ) {
        return;
      }

      if (municipalityResult.status === "fulfilled") {
        setMunicipality(municipalityResult.value);
      }
      if (organizationResult.status === "fulfilled") {
        setOrganization(organizationResult.value);
      }

      const essentialFailure =
        municipalityResult.status === "rejected"
          ? municipalityResult.reason
          : organizationResult.status === "rejected"
            ? organizationResult.reason
            : null;
      if (essentialFailure) {
        handleRequestError(
          essentialFailure,
          setError,
          "No se pudo cargar el espacio municipal.",
        );
      }

      if (ordinanceResult.status === "fulfilled") {
        setOrdinances(ordinanceResult.value);
      } else {
        handleRequestError(
          ordinanceResult.reason,
          (message) =>
            setResourceErrors((current) => ({
              ...current,
              ordinances: message,
            })),
          "No se pudo cargar la normativa municipal.",
        );
      }

      if (assetResult.status === "fulfilled") {
        setAssets(assetResult.value);
      } else {
        handleRequestError(
          assetResult.reason,
          (message) =>
            setResourceErrors((current) => ({ ...current, assets: message })),
          "No se pudo cargar el inventario municipal.",
        );
      }

      if (maintenanceResult.status === "fulfilled") {
        setMaintenance(maintenanceResult.value);
      } else {
        handleRequestError(
          maintenanceResult.reason,
          (message) =>
            setResourceErrors((current) => ({
              ...current,
              maintenance: message,
            })),
          "No se pudo cargar el mantenimiento municipal.",
        );
      }
    }

    void loadWorkspace().finally(() => {
      if (
        !controller.signal.aborted &&
        requestSequence === requestSequenceRef.current
      ) {
        setIsLoading(false);
      }
    });

    return () => controller.abort();
    // Las IDs y la firma de permisos delimitan la carga. El controlador y el
    // contador impiden que una respuesta antigua sustituya el municipio activo.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    user?.id,
    user?.is_superuser,
    selectedContext?.organization.id,
    selectedContext?.municipality.id,
    permissionSignature,
    loadAttempt,
  ]);

  if (!user) {
    return null;
  }
  if (!canViewMunicipalHub(user)) {
    return <RestrictedState />;
  }
  if (!selectedContext) {
    return <EmptyState user={user} />;
  }

  const isPaused = selectedContext.organization.status === "paused";
  const activeTabDefinition =
    TAB_DEFINITIONS.find((tab) => tab.id === activeTab) ?? TAB_DEFINITIONS[0];

  function handleTabKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) {
    let nextIndex: number | null = null;

    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % TAB_DEFINITIONS.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex =
        (currentIndex - 1 + TAB_DEFINITIONS.length) % TAB_DEFINITIONS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = TAB_DEFINITIONS.length - 1;
    }

    if (nextIndex === null) {
      return;
    }

    event.preventDefault();
    selectWorkspaceTab(TAB_DEFINITIONS[nextIndex].id);
    tabButtonRefs.current[nextIndex]?.focus();
  }

  function selectWorkspaceTab(tab: WorkspaceTab, moveFocus = false) {
    setActiveTab(tab);
    const url = new URL(window.location.href);
    if (tab === "summary") {
      url.searchParams.delete("tab");
    } else {
      url.searchParams.set("tab", tab);
    }
    window.history.replaceState(window.history.state, "", url);

    if (moveFocus) {
      const tabIndex = TAB_DEFINITIONS.findIndex(({ id }) => id === tab);
      window.requestAnimationFrame(() => {
        tabButtonRefs.current[tabIndex]?.focus();
      });
    }
  }

  function changeOrganization(organizationId: number) {
    requestSequenceRef.current += 1;
    setSelectedOrganizationId(organizationId);
    setMunicipality(null);
    setOrganization(null);
    setOrdinances(null);
    setAssets(null);
    setMaintenance(null);
    setResourceErrors(EMPTY_RESOURCE_ERRORS);
    setError("");
    setIsLoading(true);
    selectWorkspaceTab("summary");
  }

  function retryWorkspace() {
    setLoadAttempt((value) => value + 1);
  }

  return (
    <section className={styles.workspace}>
      <header className={styles.masthead}>
        <div className={styles.identity}>
          <span className={styles.municipalityMark} aria-hidden="true">
            {getInitials(selectedContext.municipality.name)}
          </span>
          <div>
            <p>Espacio municipal</p>
            <h1>{selectedContext.municipality.name}</h1>
            <span>
              {selectedContext.municipality.province} ·{" "}
              {selectedContext.municipality.autonomous_community}
            </span>
          </div>
        </div>

        <div className={styles.contextPanel}>
          <span>Organización activa</span>
          {contexts.length > 1 ? (
            <select
              aria-label="Organización y municipio"
              onChange={(event) =>
                changeOrganization(Number(event.target.value))
              }
              value={selectedContext.organization.id}
            >
              {contexts.map(({ organization: item, municipality: summary }) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {summary.name}
                  {item.status === "paused" ? " (pausada)" : ""}
                </option>
              ))}
            </select>
          ) : (
            <strong>{selectedContext.organization.name}</strong>
          )}
          <small>
            {isPaused ? "Modo de consulta · organización pausada" : "Datos en producción"}
          </small>
        </div>
      </header>

      {isPaused ? (
        <p className={styles.warning} role="status">
          La organización está pausada. La información permanece disponible en
          modo de consulta.
        </p>
      ) : null}

      <nav aria-label="Áreas del ayuntamiento" className={styles.tabs} role="tablist">
        {TAB_DEFINITIONS.map(({ id, label, icon: Icon }, index) => (
          <button
            aria-controls={`municipal-panel-${id}`}
            aria-selected={activeTab === id}
            id={`municipal-tab-${id}`}
            key={id}
            onClick={() => selectWorkspaceTab(id)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
            ref={(element) => {
              tabButtonRefs.current[index] = element;
            }}
            role="tab"
            tabIndex={activeTab === id ? 0 : -1}
            type="button"
          >
            <Icon aria-hidden="true" size={17} strokeWidth={1.6} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <div
        aria-labelledby={`municipal-tab-${activeTabDefinition.id}`}
        id={`municipal-panel-${activeTabDefinition.id}`}
        role="tabpanel"
      >
        {isLoading ? (
          <div
            aria-busy="true"
            aria-live="polite"
            className={styles.loadingState}
            role="status"
          >
            <RefreshCw aria-hidden="true" size={22} />
            <div>
              <strong>Cargando el espacio municipal</strong>
              <span>Consultando fuentes autorizadas…</span>
            </div>
          </div>
        ) : error || !municipality || !organization ? (
          <ResourceState
            action={
              <button
                className={styles.primaryAction}
                onClick={retryWorkspace}
                type="button"
              >
                Reintentar
              </button>
            }
            description={error || "La ficha municipal está incompleta."}
            icon={CircleAlert}
            title="No se pudo abrir el espacio municipal"
            tone="error"
          />
        ) : activeTab === "summary" ? (
          <InformacionMunicipio
            assets={assets}
            canViewAssets={canViewAssets}
            canViewMaintenance={canViewMaintenance}
            canViewOrdinances={canViewOrdinances}
            errors={resourceErrors}
            maintenance={maintenance}
            municipality={municipality}
            onTabChange={(tab) => selectWorkspaceTab(tab, true)}
            ordinances={ordinances}
            organization={organization}
          />
        ) : activeTab === "ordinances" ? (
          <Normativa
            canManage={canManageOrdinances}
            canView={canViewOrdinances}
            error={resourceErrors.ordinances}
            municipality={municipality}
            onRetry={retryWorkspace}
            ordinances={ordinances}
          />
        ) : activeTab === "facilities" ? (
          <ServiciosMunicipales
            assets={assets}
            canViewAssets={canViewAssets}
            canViewMaintenance={canViewMaintenance}
            canViewMap={canViewMap}
            errors={resourceErrors}
            maintenance={maintenance}
            onRetry={retryWorkspace}
            organizationId={selectedContext.organization.id}
          />
        ) : activeTab === "people" ? (
          <Personal organization={organization} />
        ) : (
          <HojaDeRuta
            assets={assets}
            canViewMap={canViewMap}
            maintenance={maintenance}
            ordinances={ordinances}
            organizationId={selectedContext.organization.id}
            user={user}
          />
        )}
      </div>
    </section>
  );
}
